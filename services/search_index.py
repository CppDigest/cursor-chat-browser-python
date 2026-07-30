"""Local FTS search index over Cursor global composer + bubble storage.

Phase 2: derived ``search_index.sqlite`` under ``~/.cache/cursor-chat-browser/``.
Cursor's ``state.vscdb`` remains source of truth; this index is rebuilt when
storage mtimes change (same fingerprint as summary_cache).

Bypass: ``CURSOR_CHAT_BROWSER_NO_SEARCH_INDEX=1`` or ``CURSOR_CHAT_BROWSER_NOCACHE=1``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from services.search import (
    _composer_dict_timestamp_ms,
    _composer_row_raw_text,
    _quick_bubble_text,
)
from services.summary_cache import (
    CACHE_DIR,
    nocache_enabled,
    workspace_storage_fingerprint,
)
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    collect_workspace_entries,
    global_storage_db_path,
    open_global_db,
)
from utils.path_helpers import to_epoch_ms
from utils.exclusion_rules import RuleTokens

__all__ = [
    "SEARCH_INDEX_FILE",
    "SEARCH_INDEX_POINTER_FILE",
    "ensure_search_index",
    "index_is_usable",
    "index_search_enabled",
    "query_all_bubble_texts_for_composer_ids",
    "query_all_composer_bubble_texts",
    "query_composer_bubble_hits",
    "query_composer_title_hits",
    "query_composer_rows_in_window",
    "start_search_index_background",
]

_logger = logging.getLogger(__name__)

INDEX_VERSION = 1
SEARCH_INDEX_POINTER_FILE = CACHE_DIR / "search_index.active"
# Legacy single-file path (pre-pointer builds); still honored when no pointer exists.
SEARCH_INDEX_FILE = CACHE_DIR / "search_index.sqlite"

_index_lock = threading.Lock()
_index_build_lock = threading.Lock()
_background_started = False


def _resolve_active_index_db_path() -> Path | None:
    """Return the SQLite path for the current search index generation."""
    if SEARCH_INDEX_POINTER_FILE.is_file():
        try:
            name = SEARCH_INDEX_POINTER_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            name = ""
        if name:
            candidate = CACHE_DIR / name
            if candidate.is_file():
                return candidate
    if SEARCH_INDEX_FILE.is_file():
        return SEARCH_INDEX_FILE
    return None


def _publish_active_index(new_db_path: Path) -> None:
    """Point readers at *new_db_path* without replacing an open SQLite file (Windows)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pointer_tmp = SEARCH_INDEX_POINTER_FILE.with_suffix(".active.tmp")
    pointer_tmp.write_text(new_db_path.name, encoding="utf-8")
    try:
        pointer_tmp.replace(SEARCH_INDEX_POINTER_FILE)
    except OSError:
        SEARCH_INDEX_POINTER_FILE.write_text(new_db_path.name, encoding="utf-8")
        try:
            pointer_tmp.unlink()
        except OSError:
            pass
    _prune_stale_index_files(keep=new_db_path)


def _prune_stale_index_files(*, keep: Path) -> None:
    """Best-effort removal of superseded index files (may stay locked on Windows)."""
    for pattern in ("search_index.*.sqlite", "search_index.sqlite"):
        for path in CACHE_DIR.glob(pattern):
            if path.resolve() == keep.resolve():
                continue
            try:
                path.unlink()
            except OSError:
                pass
    for suffix in (".sqlite.tmp", ".active.tmp"):
        for path in CACHE_DIR.glob(f"search_index*{suffix}"):
            try:
                path.unlink()
            except OSError:
                pass


def index_search_enabled() -> bool:
    if nocache_enabled():
        return False
    return os.environ.get("CURSOR_CHAT_BROWSER_NO_SEARCH_INDEX", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )


def _storage_fingerprint(workspace_path: str, rules: list[RuleTokens]) -> dict[str, Any]:
    entries = collect_workspace_entries(workspace_path)
    return workspace_storage_fingerprint(workspace_path, entries, rules)


def _open_index_db(*, readonly: bool = True) -> sqlite3.Connection | None:
    db_path = _resolve_active_index_db_path()
    if db_path is None:
        return None
    uri = db_path.resolve().as_uri()
    if readonly:
        uri += "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        _logger.debug("Failed to open search index: %s", exc)
        return None


@contextmanager
def _index_db_conn(*, readonly: bool = True) -> Iterator[sqlite3.Connection | None]:
    conn = _open_index_db(readonly=readonly)
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


class _IndexBuildSkipped(Exception):
    """Raised when index build cannot read the source global database."""


def _read_stored_fingerprint(conn: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'fingerprint'"
        ).fetchone()
        if not row or not row[0]:
            return None
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def _fingerprints_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    from services.summary_cache import _fingerprint_equal

    return _fingerprint_equal(a, b)


def _fts_match_query(query_lower: str) -> str | None:
    """Build an FTS5 prefix query from user input."""
    tokens = [t for t in re.split(r"\W+", query_lower) if t]
    if not tokens:
        return None
    parts: list[str] = []
    for token in tokens:
        escaped = token.replace('"', '""')
        parts.append(f'"{escaped}"*')
    return " AND ".join(parts)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS composers (
            composer_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            created_ms INTEGER NOT NULL DEFAULT 0,
            updated_ms INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_composers_updated_ms
            ON composers(updated_ms);
        CREATE VIRTUAL TABLE IF NOT EXISTS bubbles_fts USING fts5(
            composer_id UNINDEXED,
            text,
            tokenize='unicode61'
        );
        """
    )


def build_search_index(
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    force: bool = False,
) -> bool:
    """Rebuild search index when fingerprint differs. Returns True if rebuilt."""
    if not index_search_enabled():
        return False

    fingerprint = _storage_fingerprint(workspace_path, rules)
    gdb = global_storage_db_path(workspace_path)
    if not os.path.isfile(gdb):
        return False

    with _index_build_lock:
        active_path = _resolve_active_index_db_path()
        if not force and active_path is not None:
            with _index_db_conn(readonly=True) as existing:
                if existing is not None:
                    stored = _read_stored_fingerprint(existing)
                    if stored is not None and _fingerprints_match(stored, fingerprint):
                        return False

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        new_path = CACHE_DIR / f"search_index.{uuid.uuid4().hex[:12]}.sqlite"

        try:
            with closing(sqlite3.connect(new_path)) as conn:
                conn.row_factory = sqlite3.Row
                _create_schema(conn)
                composer_count = 0
                bubble_count = 0
                indexed_composer_ids: set[str] = set()

                with open_global_db(workspace_path) as (src_conn, _):
                    if src_conn is None:
                        raise _IndexBuildSkipped()
                    composer_rows = src_conn.execute(
                        COMPOSER_ROWS_WITH_HEADERS_SQL
                    ).fetchall()
                    for row in composer_rows:
                        composer_id = row["key"].split(":")[1]
                        raw_text = _composer_row_raw_text(row)
                        try:
                            cd = json.loads(raw_text)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            continue
                        if not isinstance(cd, dict):
                            continue
                        title = cd.get("name") or ""
                        if not isinstance(title, str):
                            title = str(title) if title else ""
                        conn.execute(
                            "INSERT OR REPLACE INTO composers"
                            " (composer_id, title, created_ms, updated_ms, raw_json)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (
                                composer_id,
                                title,
                                to_epoch_ms(cd.get("createdAt")) or 0,
                                _composer_dict_timestamp_ms(cd),
                                raw_text,
                            ),
                        )
                        composer_count += 1
                        indexed_composer_ids.add(composer_id)

                    bubble_rows = src_conn.execute(
                        "SELECT key, value FROM cursorDiskKV"
                        " WHERE key LIKE 'bubbleId:%' AND value IS NOT NULL"
                    ).fetchall()
                    for row in bubble_rows:
                        parts = row["key"].split(":")
                        if len(parts) < 2 or not parts[1]:
                            continue
                        composer_id = parts[1]
                        if composer_id not in indexed_composer_ids:
                            continue
                        text = _quick_bubble_text(row["value"])
                        if not text:
                            continue
                        conn.execute(
                            "INSERT INTO bubbles_fts(composer_id, text) VALUES (?, ?)",
                            (composer_id, text),
                        )
                        bubble_count += 1

                conn.execute(
                    "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
                    ("fingerprint", json.dumps(fingerprint, ensure_ascii=False)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
                    ("version", str(INDEX_VERSION)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
                    (
                        "stats",
                        json.dumps(
                            {"composers": composer_count, "bubbles": bubble_count}
                        ),
                    ),
                )
                conn.commit()

            _publish_active_index(new_path)
            _logger.info(
                "Search index rebuilt: %d composers, %d bubbles -> %s",
                composer_count,
                bubble_count,
                new_path.name,
            )
            return True
        except _IndexBuildSkipped:
            if new_path.is_file():
                try:
                    new_path.unlink()
                except OSError:
                    pass
            return False
        except Exception:
            _logger.exception("Search index rebuild failed")
            if new_path.is_file():
                try:
                    new_path.unlink()
                except OSError:
                    pass
            return False


def ensure_search_index(workspace_path: str, rules: list[RuleTokens]) -> None:
    """Build index synchronously if missing or stale."""
    if not index_search_enabled():
        return
    if _resolve_active_index_db_path() is None:
        build_search_index(workspace_path, rules)
        return
    fingerprint = _storage_fingerprint(workspace_path, rules)
    with _index_db_conn(readonly=True) as conn:
        if conn is None:
            build_search_index(workspace_path, rules)
            return
        stored = _read_stored_fingerprint(conn)
        if stored is None or not _fingerprints_match(stored, fingerprint):
            build_search_index(workspace_path, rules)


def start_search_index_background(
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    poll_seconds: int = 60,
) -> None:
    """Kick off initial + periodic index refresh in a daemon thread."""
    global _background_started
    if not index_search_enabled():
        return
    with _index_lock:
        if _background_started:
            return
        _background_started = True

    def _worker() -> None:
        while True:
            try:
                ensure_search_index(workspace_path, rules)
            except Exception:
                _logger.exception("Background search index refresh failed")
            time.sleep(poll_seconds)

    thread = threading.Thread(target=_worker, name="search-index-refresh", daemon=True)
    thread.start()


def query_composer_bubble_hits(
    query_lower: str,
    *,
    since_ms: int | None,
    composer_ids_filter: set[str] | None = None,
) -> dict[str, list[str]]:
    """Return composer_id -> matching bubble texts using the local FTS index."""
    if not query_lower or not index_search_enabled():
        return {}

    fts_q = _fts_match_query(query_lower)
    if not fts_q:
        return {}

    with _index_db_conn(readonly=True) as conn:
        if conn is None:
            return {}
        try:
            if since_ms is None:
                rows = conn.execute(
                    "SELECT composer_id, text FROM bubbles_fts WHERE bubbles_fts MATCH ?",
                    (fts_q,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT b.composer_id, b.text"
                    " FROM bubbles_fts b"
                    " JOIN composers c ON c.composer_id = b.composer_id"
                    " WHERE bubbles_fts MATCH ?"
                    " AND (c.updated_ms >= ? OR c.updated_ms <= 0)",
                    (fts_q, since_ms),
                ).fetchall()
        except sqlite3.Error as exc:
            _logger.debug("FTS query failed (%s); index may be rebuilding", exc)
            return {}

        by_composer: dict[str, list[str]] = {}
        for row in rows:
            composer_id = row["composer_id"]
            if composer_ids_filter is not None and composer_id not in composer_ids_filter:
                continue
            text = row["text"] or ""
            if query_lower not in text.lower():
                continue
            by_composer.setdefault(composer_id, []).append(text)
        return by_composer


def query_composer_title_hits(
    query_lower: str,
    *,
    since_ms: int | None,
) -> list[sqlite3.Row]:
    """Composers whose title matches *query_lower* and pass the date window."""
    if not query_lower or not index_search_enabled():
        return []

    pattern = f"%{query_lower}%"
    with _index_db_conn(readonly=True) as conn:
        if conn is None:
            return []
        try:
            if since_ms is None:
                return list(
                    conn.execute(
                        "SELECT composer_id, title, created_ms, updated_ms, raw_json"
                        " FROM composers WHERE LOWER(title) LIKE ?",
                        (pattern,),
                    ).fetchall()
                )
            return list(
                conn.execute(
                    "SELECT composer_id, title, created_ms, updated_ms, raw_json"
                    " FROM composers"
                    " WHERE LOWER(title) LIKE ?"
                    " AND (updated_ms >= ? OR updated_ms <= 0)",
                    (pattern, since_ms),
                ).fetchall()
            )
        except sqlite3.Error as exc:
            _logger.debug("Title query on search index failed: %s", exc)
            return []


def query_all_composer_bubble_texts(composer_id: str) -> list[str]:
    """All indexed bubble texts for one composer (exclusion-rule checks)."""
    if not composer_id or not index_search_enabled():
        return []
    by_id = query_all_bubble_texts_for_composer_ids({composer_id})
    return by_id.get(composer_id, [])


def query_all_bubble_texts_for_composer_ids(
    composer_ids: set[str] | frozenset[str],
) -> dict[str, list[str]]:
    """Batch-load all indexed bubble texts for exclusion-rule checks."""
    if not composer_ids or not index_search_enabled():
        return {}

    ids = list(composer_ids)
    result: dict[str, list[str]] = {}
    with _index_db_conn(readonly=True) as conn:
        if conn is None:
            return {}
        try:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT composer_id, text FROM bubbles_fts"
                    f" WHERE composer_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    text = row["text"]
                    if text:
                        result.setdefault(row["composer_id"], []).append(text)
        except sqlite3.Error:
            return {}
    return result


def query_composer_rows_in_window(
    since_ms: int | None,
) -> dict[str, sqlite3.Row]:
    """All indexed composers, optionally filtered by ``since_ms``."""
    with _index_db_conn(readonly=True) as conn:
        if conn is None:
            return {}
        try:
            if since_ms is None:
                rows = conn.execute(
                    "SELECT composer_id, title, created_ms, updated_ms, raw_json"
                    " FROM composers"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT composer_id, title, created_ms, updated_ms, raw_json"
                    " FROM composers"
                    " WHERE updated_ms >= ? OR updated_ms <= 0",
                    (since_ms,),
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {row["composer_id"]: row for row in rows}


def index_is_usable(workspace_path: str, rules: list[RuleTokens]) -> bool:
    """True when the on-disk index matches the current Cursor storage fingerprint."""
    if not index_search_enabled() or _resolve_active_index_db_path() is None:
        return False
    fingerprint = _storage_fingerprint(workspace_path, rules)
    with _index_db_conn(readonly=True) as conn:
        if conn is None:
            return False
        stored = _read_stored_fingerprint(conn)
        return stored is not None and _fingerprints_match(stored, fingerprint)
