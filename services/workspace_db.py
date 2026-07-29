from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, cast

from utils.exclusion_rules import RuleTokens

_logger = logging.getLogger(__name__)

from models import Bubble, ParseWarningCollector, SchemaError
from utils.path_helpers import get_workspace_folder_paths
from utils.workspace_descriptor import read_json_file


# ── Global-DB KV loaders ────────────────────────────────────────────────────
# Each function accepts an already-opened sqlite3.Connection (row_factory must
# be set to sqlite3.Row by the caller, as open_global_db does) and returns
# a populated dict.  sqlite3.Error is caught internally so a missing or
# corrupt table cannot propagate to callers.


def safe_fetchall(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    """Run *query* on *conn*; return rows or ``[]`` on sqlite3.Error."""
    try:
        return cast(list[sqlite3.Row], conn.execute(query, params).fetchall())
    except sqlite3.Error:
        return []


def _parse_bubble_kv_row(
    row_key: str,
    row_value: str | bytes,
    *,
    parse_warnings: ParseWarningCollector | None = None,
) -> tuple[str, Bubble] | None:
    """Parse one ``bubbleId:…`` row; return ``(bubble_id, Bubble)`` or skip."""
    parts = row_key.split(":")
    if len(parts) < 3:
        return None
    bid = parts[2]
    try:
        parsed = json.loads(row_value)
        bubble = Bubble.from_dict(parsed, bubble_id=bid)
        return bid, bubble
    except SchemaError as exc:
        _logger.warning(
            "Schema drift in bubble %s: %s (%s)", bid, exc, type(exc).__name__
        )
        if parse_warnings is not None:
            parse_warnings.record_bubble_skipped()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if parse_warnings is not None:
            _logger.warning(
                "Failed to decode Bubble from %s: %s", row_key, exc
            )
            parse_warnings.record_bubble_skipped()
        else:
            _logger.debug("Skipping malformed bubbleId row %s: %s", row_key, exc)
    return None


def load_bubble_map(
    global_db: sqlite3.Connection,
    *,
    parse_warnings: ParseWarningCollector | None = None,
) -> dict[str, Bubble]:
    """Load all ``bubbleId:*`` KV entries into ``{bubble_id: Bubble}``.

    Uses the same :meth:`Bubble.from_dict` validation as search and tabs.
    When *parse_warnings* is set, skipped rows are recorded for the API.
    """
    bubble_map: dict[str, Bubble] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV"
            " WHERE key LIKE 'bubbleId:%' AND value IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return bubble_map
    for row in rows:
        parsed = _parse_bubble_kv_row(
            row["key"], row["value"], parse_warnings=parse_warnings
        )
        if parsed is not None:
            bid, bubble = parsed
            bubble_map[bid] = bubble
    return bubble_map


def _extract_root_paths_from_context(ctx: dict[str, Any]) -> list[str]:
    """Pull ``rootPath`` strings from a messageRequestContext JSON object."""
    paths: list[str] = []
    layouts = ctx.get("projectLayouts")
    if not isinstance(layouts, list):
        return paths
    for layout in layouts:
        try:
            o = json.loads(layout) if isinstance(layout, str) else layout
            if isinstance(o, dict) and o.get("rootPath"):
                paths.append(o["rootPath"])
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            continue
    return paths


def load_project_layouts_for_composer(
    global_db: sqlite3.Connection, composer_id: str,
) -> list[str]:
    """Scoped MRC load: ``messageRequestContext:{composer_id}:%`` only."""
    paths: list[str] = []
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"messageRequestContext:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return paths
    for row in rows:
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                paths.extend(_extract_root_paths_from_context(ctx))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed messageRequestContext row %s: %s",
                row["key"],
                e,
            )
    return paths


def load_project_layouts_map(global_db: sqlite3.Connection) -> dict[str, list[str]]:
    """Load ``projectLayouts`` from all ``messageRequestContext:*`` KV entries.

    Returns ``{composer_id: [root_path_str, ...]}``.  Prefer
    :func:`load_project_layouts_for_composer` on list paths when only a few
    composers need layout fallbacks.
    """
    layouts_map: dict[str, list[str]] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"
        ).fetchall()
    except sqlite3.Error:
        return layouts_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 2:
            continue
        cid = parts[1]
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                layouts_map.setdefault(cid, [])
                layouts_map[cid].extend(_extract_root_paths_from_context(ctx))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug("Skipping malformed messageRequestContext row %s: %s", row["key"], e)
    return layouts_map


def load_code_block_diff_map(global_db: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Load ``codeBlockDiff:*`` KV entries into ``{composer_id: [diff_dict]}``.

    Each diff dict contains all fields from the raw JSON value plus a
    ``diffId`` key taken from the third path component of the KV key.
    """
    diff_map: dict[str, list[dict[str, Any]]] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'codeBlockDiff:%'"
        ).fetchall()
    except sqlite3.Error:
        return diff_map
    for row in rows:
        parts = row["key"].split(":")
        cid = parts[1] if len(parts) > 1 else None
        if not cid:
            continue
        try:
            d = json.loads(row["value"])
            if isinstance(d, dict):
                diff_map.setdefault(cid, []).append({
                    **d,
                    "diffId": parts[2] if len(parts) > 2 else None,
                })
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug("Skipping malformed codeBlockDiff row %s: %s", row["key"], e)
    return diff_map


def load_bubbles_for_composer(
    global_db: sqlite3.Connection,
    composer_id: str,
    *,
    parse_warnings: ParseWarningCollector | None = None,
) -> dict[str, Bubble]:
    """Load ``bubbleId:{composer_id}:*`` KV entries into ``{bubble_id: Bubble}``.

    Scoped alternative to :func:`load_bubble_map` for single-conversation assembly;
    avoids a full global ``bubbleId:%`` scan.
    """
    bubble_map: dict[str, Bubble] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return bubble_map
    for row in rows:
        parsed = _parse_bubble_kv_row(
            row["key"], row["value"], parse_warnings=parse_warnings
        )
        if parsed is not None:
            bid, bubble = parsed
            bubble_map[bid] = bubble
    return bubble_map


def load_message_request_context_for_composer(
    global_db: sqlite3.Connection, composer_id: str,
) -> list[dict[str, Any]]:
    """Load ``messageRequestContext:{composer_id}:*`` KV entries.

    Returns a list of context dicts, each with an injected ``contextId`` key
    taken from the third path component of the KV key.  Scoped alternative to
    the global MRC pass inside :func:`load_project_layouts_map`.
    """
    contexts: list[dict[str, Any]] = []
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"messageRequestContext:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return contexts
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        context_id = parts[2]
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                contexts.append({**ctx, "contextId": context_id})
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed messageRequestContext row %s: %s",
                row["key"],
                e,
            )
    return contexts


def load_code_block_diffs_for_composer(
    global_db: sqlite3.Connection, composer_id: str,
) -> list[dict[str, Any]]:
    """Load ``codeBlockDiff:{composer_id}:*`` KV entries.

    Returns a list of diff dicts, each with an injected ``diffId`` key.
    Scoped alternative to :func:`load_code_block_diff_map` for single-conversation
    assembly.
    """
    diffs: list[dict[str, Any]] = []
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"codeBlockDiff:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return diffs
    for row in rows:
        parts = row["key"].split(":")
        try:
            d = json.loads(row["value"])
            if isinstance(d, dict):
                diffs.append({**d, "diffId": parts[2] if len(parts) > 2 else None})
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed codeBlockDiff row %s: %s", row["key"], e
            )
    return diffs


def collect_workspace_entries(workspace_path: str) -> list[dict[str, Any]]:
    """Scan workspace directory and return entries with workspace.json.

    Args:
        workspace_path: Cursor workspace storage root (parent of per-workspace folders).

    Returns:
        List of dicts with keys ``name`` (folder id) and ``workspaceJsonPath``.
        Returns an empty list if ``workspace_path`` is missing or unreadable.
    """
    entries: list[dict[str, Any]] = []
    try:
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if os.path.isdir(full):
                wj = os.path.join(full, "workspace.json")
                if os.path.isfile(wj):
                    entries.append({"name": name, "workspaceJsonPath": wj})
    except OSError:
        # workspace_path missing / not readable / not a directory — return what
        # we have so far. OSError covers FileNotFoundError, PermissionError,
        # and NotADirectoryError.
        pass
    return entries


def collect_invalid_workspace_ids(workspace_entries: list[dict[str, Any]]) -> set[str]:
    """Return workspace IDs whose descriptors have no resolvable folder paths.

    Args:
        workspace_entries: Output of :func:`collect_workspace_entries`.

    Returns:
        Set of workspace folder names that cannot be mapped to a folder path.
    """
    invalid: set[str] = set()
    for entry in workspace_entries:
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            folders = get_workspace_folder_paths(wd)
            if not folders:
                invalid.add(entry["name"])
        except (OSError, ValueError, KeyError, TypeError):
            # OSError: workspace.json unreadable. ValueError covers
            # json.JSONDecodeError. KeyError / TypeError: malformed entry
            # dict. Any of these mean we can't resolve folders → mark invalid,
            # matching the pre-narrowing behaviour.
            invalid.add(entry["name"])
    return invalid


# Composers that have at least one header entry (list/summary paths).
COMPOSER_ROWS_WITH_HEADERS_SQL = (
    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    " AND LENGTH(value) > 10"
    " AND value LIKE '%fullConversationHeadersOnly%'"
    " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
    " AND value NOT LIKE '%fullConversationHeadersOnly\": []%'"
)


def global_storage_db_path(workspace_path: str) -> str:
    """Resolved path to Cursor global ``state.vscdb`` for a workspace storage root."""
    return os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))


def _composer_ids_for_workspace_entry(
    workspace_path: str,
    entry: dict[str, Any],
) -> list[tuple[str, str]]:
    """Read ``composer.composerData`` from one workspace folder (parallel-safe)."""
    pairs: list[tuple[str, str]] = []
    db_path = os.path.join(workspace_path, entry["name"], "state.vscdb")
    if not os.path.isfile(db_path):
        return pairs
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(db_uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
            ).fetchone()
    except sqlite3.Error:
        return pairs
    if not (row and row[0]):
        return pairs
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, ValueError):
        return pairs
    all_composers = data.get("allComposers") if isinstance(data, dict) else None
    if not isinstance(all_composers, list):
        return pairs
    ws_name = entry["name"]
    for c in all_composers:
        if not isinstance(c, dict):
            continue
        cid = c.get("composerId")
        if cid:
            pairs.append((cid, ws_name))
    return pairs


def build_composer_id_to_workspace_id(
    workspace_path: str, workspace_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Build mapping from composer ID to workspace folder name.

    Reads ``composer.composerData`` from each workspace's ``state.vscdb``.
    Skips workspaces with missing databases or malformed JSON. Workspace
    folders are scanned in parallel (issue #95).

    Args:
        workspace_path: Cursor workspace storage root.
        workspace_entries: Output of :func:`collect_workspace_entries`.

    Returns:
        Dict mapping ``composerId`` strings to workspace folder names.
    """
    if not workspace_entries:
        return {}
    mapping: dict[str, str] = {}
    max_workers = min(32, len(workspace_entries))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_composer_ids_for_workspace_entry, workspace_path, entry)
            for entry in workspace_entries
        ]
        for _entry, fut in zip(workspace_entries, futures):
            for cid, ws_name in fut.result():
                if cid not in mapping:
                    mapping[cid] = ws_name
    return mapping


def build_composer_id_to_workspace_id_cached(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
) -> dict[str, str]:
    """Like :func:`build_composer_id_to_workspace_id` with optional disk cache."""
    from services.summary_cache import (
        get_or_build_cached_composer_id_to_ws,
        nocache_enabled,
    )

    if nocache_enabled(request_nocache=nocache):
        return build_composer_id_to_workspace_id(workspace_path, workspace_entries)

    def build() -> dict[str, str]:
        return build_composer_id_to_workspace_id(workspace_path, workspace_entries)

    return get_or_build_cached_composer_id_to_ws(
        workspace_path,
        workspace_entries,
        rules,
        build_fn=build,
    )


@contextmanager
def open_global_db(
    workspace_path: str,
) -> Iterator[tuple[sqlite3.Connection | None, str]]:
    """Open Cursor global storage SQLite database read-only.

    Args:
        workspace_path: Cursor workspace storage root.

    Yields:
        ``(conn, path)`` where ``conn`` is a :class:`sqlite3.Connection` with
        ``row_factory=sqlite3.Row``, or ``None`` if the database file is missing
        or cannot be opened. ``path`` is always the resolved global DB path.
    """
    global_db_path = global_storage_db_path(workspace_path)
    if not os.path.isfile(global_db_path):
        yield None, global_db_path
        return
    db_uri = Path(global_db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
    except sqlite3.Error:
        yield None, global_db_path
        return
    conn.row_factory = sqlite3.Row
    try:
        yield conn, global_db_path
    finally:
        conn.close()
