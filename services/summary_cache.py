"""Disk cache for derived workspace summaries (issue #84 Phase 3).

Caches project lists and per-workspace tab summaries keyed by storage mtimes
so repeat page loads avoid re-scanning Cursor's global KV index.

Bypass: set env ``CURSOR_CHAT_BROWSER_NOCACHE=1`` or pass ``?nocache=1`` on API
requests. Cache files live under ``~/.cache/cursor-chat-browser/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeVar

from utils.exclusion_rules import RuleTokens

_logger = logging.getLogger(__name__)

# Serialises fingerprint compute, cache read-compare, and cache write so threaded
# WSGI workers cannot lost-update a peer's fresh cache entry (see design guide).
_summary_cache_lock = threading.Lock()

CACHE_VERSION = 1
CACHE_DIR = Path.home() / ".cache" / "cursor-chat-browser"
PROJECTS_CACHE_FILE = CACHE_DIR / "projects.json"
COMPOSER_MAP_CACHE_FILE = CACHE_DIR / "composer-id-to-ws.json"
INVALID_WORKSPACE_ALIASES_CACHE_FILE = CACHE_DIR / "invalid-workspace-aliases.json"
TAB_SUMMARIES_PREFIX = "tab-summaries-"

T = TypeVar("T")


def nocache_enabled(*, request_nocache: bool = False) -> bool:
    """Return whether summary-cache reads should be bypassed.

    Args:
        request_nocache: True when the HTTP request included ``?nocache=1``.

    Returns:
        True when bypass is requested or ``CURSOR_CHAT_BROWSER_NOCACHE`` is set
        to ``"1"``, ``"true"``, or ``"yes"`` (case-insensitive).
    """
    if request_nocache:
        return True
    return os.environ.get("CURSOR_CHAT_BROWSER_NOCACHE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _rules_digest(rules: list[RuleTokens]) -> str:
    try:
        payload = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = repr(rules)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _file_mtime_ns(path: str | None) -> int | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def fingerprint_workspace_storage(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    *,
    global_db_path: str | None,
    rules: list[RuleTokens],
    cli_chats_path: str | None = None,
) -> dict[str, Any]:
    """Build a fingerprint dict for cache invalidation."""
    ws_mt: list[list[str | int]] = []

    def _entry_mtimes(entry: dict[str, Any]) -> list[list[str | int]]:
        rows: list[list[str | int]] = []
        name = entry.get("name")
        if not isinstance(name, str):
            return rows
        base = os.path.join(workspace_path, name)
        for rel in ("state.vscdb", "workspace.json"):
            p = os.path.join(base, rel)
            mtime = _file_mtime_ns(p)
            if mtime is not None:
                rows.append([f"{name}/{rel}", mtime])
        return rows

    if workspace_entries:
        max_workers = min(32, len(workspace_entries))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_entry_mtimes, entry) for entry in workspace_entries]
            for fut in as_completed(futures):
                ws_mt.extend(fut.result())
    ws_mt.sort(key=lambda row: row[0])

    return {
        "version": CACHE_VERSION,
        "workspace_path": os.path.normpath(workspace_path),
        "global_db_mtime_ns": _file_mtime_ns(global_db_path),
        "workspace_files": ws_mt,
        "rules_digest": _rules_digest(rules),
        "cli_chats_mtime_ns": _file_mtime_ns(cli_chats_path),
    }


def _workspace_storage_fingerprint(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
) -> dict[str, Any]:
    """Fingerprint workspace storage, resolving global-db and cli-chats paths."""
    from services.workspace_db import global_storage_db_path
    from utils.workspace_path import get_cli_chats_path

    gdb = global_storage_db_path(workspace_path)
    cli_path = get_cli_chats_path()
    return fingerprint_workspace_storage(
        workspace_path,
        workspace_entries,
        global_db_path=gdb if os.path.isfile(gdb) else None,
        rules=rules,
        cli_chats_path=cli_path if os.path.isdir(cli_path) else None,
    )


def _normalize_fingerprint(fp: dict[str, Any]) -> dict[str, Any]:
    """Normalize fingerprint for comparison (JSON round-trip uses lists, not tuples)."""
    normalized = dict(fp)
    wf = fp.get("workspace_files")
    if isinstance(wf, list):
        normalized["workspace_files"] = [
            [row[0], row[1]] if isinstance(row, (list, tuple)) and len(row) == 2 else row
            for row in wf
        ]
    return normalized


def _fingerprint_equal(a: object, b: dict[str, Any]) -> bool:
    if not isinstance(a, dict):
        return False
    return _normalize_fingerprint(a) == _normalize_fingerprint(b)


def _read_cache_file_unlocked(path: Path | str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError) as e:
        _logger.debug("Summary cache read failed for %s: %s", path, e)
        return None


def _write_cache_file_unlocked(path: Path | str, payload: dict[str, Any]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(p)
    except OSError as e:
        _logger.warning("Summary cache write failed for %s: %s", path, e)


def _write_cache_file(path: Path | str, payload: dict[str, Any]) -> None:
    with _summary_cache_lock:
        _write_cache_file_unlocked(path, payload)


def _get_cached_projects_unlocked(
    fingerprint: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    data = _read_cache_file_unlocked(PROJECTS_CACHE_FILE)
    if not data:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    projects = data.get("projects")
    warnings = data.get("warnings")
    if not isinstance(projects, list):
        return None
    if not isinstance(warnings, list):
        warnings = []
    return projects, warnings


def get_cached_projects(
    fingerprint: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Load cached workspace project list when the fingerprint matches.

    Args:
        fingerprint: Storage mtime/rules digest from
            :func:`fingerprint_workspace_storage`.

    Returns:
        ``(projects, warnings)`` on hit, else ``None``.
    """
    with _summary_cache_lock:
        return _get_cached_projects_unlocked(fingerprint)


def _set_cached_projects_unlocked(
    fingerprint: dict[str, Any],
    projects: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    _write_cache_file_unlocked(
        PROJECTS_CACHE_FILE,
        {
            "fingerprint": fingerprint,
            "projects": projects,
            "warnings": warnings,
        },
    )


def set_cached_projects(
    fingerprint: dict[str, Any],
    projects: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    """Write workspace project list and warnings to the disk cache.

    Args:
        fingerprint: Invalidation fingerprint paired with the payload.
        projects: Sidebar project dicts.
        warnings: Parse warnings emitted while building *projects*.
    """
    with _summary_cache_lock:
        _set_cached_projects_unlocked(fingerprint, projects, warnings)


def _get_or_build_cached(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
    *,
    build_fn: Callable[[], T],
    get_unlocked: Callable[[dict[str, Any]], T | None],
    set_unlocked: Callable[[dict[str, Any], T], None],
    should_cache: Callable[[T], bool] | None = None,
) -> T:
    with _summary_cache_lock:
        fingerprint = _workspace_storage_fingerprint(workspace_path, workspace_entries, rules)
        hit = get_unlocked(fingerprint)
        if hit is not None:
            return hit

    built = build_fn()

    with _summary_cache_lock:
        fingerprint = _workspace_storage_fingerprint(workspace_path, workspace_entries, rules)
        hit = get_unlocked(fingerprint)
        if hit is not None:
            return hit
        if should_cache is None or should_cache(built):
            set_unlocked(fingerprint, built)
    return built


def get_or_build_cached_projects(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
    *,
    build_fn: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return cached projects or build once under double-checked locking."""
    return _get_or_build_cached(
        workspace_path,
        workspace_entries,
        rules,
        build_fn=build_fn,
        get_unlocked=_get_cached_projects_unlocked,
        set_unlocked=lambda fp, built: _set_cached_projects_unlocked(fp, built[0], built[1]),
    )


def _get_cached_composer_id_to_ws_unlocked(
    fingerprint: dict[str, Any],
) -> dict[str, str] | None:
    data = _read_cache_file_unlocked(COMPOSER_MAP_CACHE_FILE)
    if not data:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    mapping = data.get("composer_id_to_ws")
    if not isinstance(mapping, dict):
        return None
    return {str(k): str(v) for k, v in mapping.items()}


def get_cached_composer_id_to_ws(
    fingerprint: dict[str, Any],
) -> dict[str, str] | None:
    """Load cached composer-id → workspace-id map when the fingerprint matches.

    Args:
        fingerprint: Storage mtime/rules digest.

    Returns:
        Mapping on hit, else ``None``.
    """
    with _summary_cache_lock:
        return _get_cached_composer_id_to_ws_unlocked(fingerprint)


def _set_cached_composer_id_to_ws_unlocked(
    fingerprint: dict[str, Any],
    mapping: dict[str, str],
) -> None:
    _write_cache_file_unlocked(
        COMPOSER_MAP_CACHE_FILE,
        {
            "fingerprint": fingerprint,
            "composer_id_to_ws": mapping,
        },
    )


def set_cached_composer_id_to_ws(
    fingerprint: dict[str, Any],
    mapping: dict[str, str],
) -> None:
    """Persist composer-id → workspace-id map under *fingerprint*.

    Args:
        fingerprint: Invalidation fingerprint paired with *mapping*.
        mapping: Composer UUID to workspace folder name.
    """
    with _summary_cache_lock:
        _set_cached_composer_id_to_ws_unlocked(fingerprint, mapping)


def get_or_build_cached_composer_id_to_ws(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
    *,
    build_fn: Callable[[], dict[str, str]],
) -> dict[str, str]:
    """Return cached composer map or build once under double-checked locking."""
    return _get_or_build_cached(
        workspace_path,
        workspace_entries,
        rules,
        build_fn=build_fn,
        get_unlocked=_get_cached_composer_id_to_ws_unlocked,
        set_unlocked=_set_cached_composer_id_to_ws_unlocked,
    )


def _get_cached_invalid_workspace_aliases_unlocked(
    fingerprint: dict[str, Any],
) -> dict[str, str] | None:
    data = _read_cache_file_unlocked(INVALID_WORKSPACE_ALIASES_CACHE_FILE)
    if not data:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    aliases = data.get("invalid_workspace_aliases")
    if not isinstance(aliases, dict):
        _logger.debug(
            "Invalid workspace aliases cache rejected: invalid_workspace_aliases is not a dict",
        )
        return None
    validated: dict[str, str] = {}
    for key, value in aliases.items():
        if not isinstance(key, str) or not isinstance(value, str):
            _logger.debug(
                "Invalid workspace aliases cache rejected: non-string entry (%r -> %r)",
                key,
                value,
            )
            return None
        validated[key] = value
    return validated


def get_cached_invalid_workspace_aliases(
    fingerprint: dict[str, Any],
) -> dict[str, str] | None:
    """Load cached invalid-workspace alias map when the fingerprint matches.

    Args:
        fingerprint: Storage mtime/rules digest.

    Returns:
        ``{invalid_id: replacement_id}`` on hit, else ``None``.
    """
    with _summary_cache_lock:
        return _get_cached_invalid_workspace_aliases_unlocked(fingerprint)


def _set_cached_invalid_workspace_aliases_unlocked(
    fingerprint: dict[str, Any],
    aliases: dict[str, str],
) -> None:
    _write_cache_file_unlocked(
        INVALID_WORKSPACE_ALIASES_CACHE_FILE,
        {
            "fingerprint": fingerprint,
            "invalid_workspace_aliases": aliases,
        },
    )


def set_cached_invalid_workspace_aliases(
    fingerprint: dict[str, Any],
    aliases: dict[str, str],
) -> None:
    """Persist invalid-workspace alias map under *fingerprint*.

    Args:
        fingerprint: Invalidation fingerprint paired with *aliases*.
        aliases: ``{invalid_id: replacement_id}`` from alias inference.
    """
    with _summary_cache_lock:
        _set_cached_invalid_workspace_aliases_unlocked(fingerprint, aliases)


def get_or_build_cached_invalid_workspace_aliases(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
    *,
    build_fn: Callable[[], dict[str, str]],
) -> dict[str, str]:
    """Return cached alias map or build once under double-checked locking."""
    return _get_or_build_cached(
        workspace_path,
        workspace_entries,
        rules,
        build_fn=build_fn,
        get_unlocked=_get_cached_invalid_workspace_aliases_unlocked,
        set_unlocked=_set_cached_invalid_workspace_aliases_unlocked,
    )


def _tab_summaries_path(workspace_id: str) -> Path:
    safe = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{TAB_SUMMARIES_PREFIX}{safe}.json"


def _get_cached_tab_summaries_unlocked(
    fingerprint: dict[str, Any],
    workspace_id: str,
) -> tuple[dict[str, Any], int] | None:
    data = _read_cache_file_unlocked(_tab_summaries_path(workspace_id))
    if not data:
        return None
    if data.get("workspace_id") != workspace_id:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    payload = data.get("payload")
    status = data.get("status", 200)
    if not isinstance(payload, dict) or not isinstance(status, int):
        return None
    return payload, status


def get_cached_tab_summaries(
    fingerprint: dict[str, Any],
    workspace_id: str,
) -> tuple[dict[str, Any], int] | None:
    """Load cached tab-summary response for one workspace when fingerprint matches.

    Args:
        fingerprint: Storage mtime/rules digest.
        workspace_id: Workspace folder name the payload belongs to.

    Returns:
        ``(payload, status)`` on hit, else ``None``.
    """
    with _summary_cache_lock:
        return _get_cached_tab_summaries_unlocked(fingerprint, workspace_id)


def _set_cached_tab_summaries_unlocked(
    fingerprint: dict[str, Any],
    workspace_id: str,
    payload: dict[str, Any],
    status: int,
) -> None:
    _write_cache_file_unlocked(
        _tab_summaries_path(workspace_id),
        {
            "workspace_id": workspace_id,
            "fingerprint": fingerprint,
            "payload": payload,
            "status": status,
        },
    )


def set_cached_tab_summaries(
    fingerprint: dict[str, Any],
    workspace_id: str,
    payload: dict[str, Any],
    status: int,
) -> None:
    """Persist tab-summary API payload for one workspace.

    Args:
        fingerprint: Invalidation fingerprint paired with the response.
        workspace_id: Workspace folder name.
        payload: JSON body returned to clients.
        status: HTTP status code paired with *payload*.
    """
    with _summary_cache_lock:
        _set_cached_tab_summaries_unlocked(fingerprint, workspace_id, payload, status)


def get_or_build_cached_tab_summaries(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
    workspace_id: str,
    *,
    build_fn: Callable[[], tuple[dict[str, Any], int]],
) -> tuple[dict[str, Any], int]:
    """Return cached tab summaries or build once under double-checked locking."""
    return _get_or_build_cached(
        workspace_path,
        workspace_entries,
        rules,
        build_fn=build_fn,
        get_unlocked=lambda fp: _get_cached_tab_summaries_unlocked(fp, workspace_id),
        set_unlocked=lambda fp, built: _set_cached_tab_summaries_unlocked(
            fp, workspace_id, built[0], built[1],
        ),
        should_cache=lambda built: built[1] == 200,
    )
