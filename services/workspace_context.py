"""Workspace determination ceremony — single orchestrator for shared maps."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from utils.exclusion_rules import RuleTokens

from models import Bubble
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    build_composer_id_to_workspace_id,
    build_composer_id_to_workspace_id_cached,
    collect_invalid_workspace_ids,
    collect_workspace_entries,
    load_bubble_map,
    load_project_layouts_map,
    safe_fetchall,
)
from services.workspace_resolver import (
    create_project_name_to_workspace_id_map,
    create_workspace_path_to_id_map,
    infer_invalid_workspace_aliases,
)


@dataclass(frozen=True)
class WorkspaceContext:
    """Precomputed workspace-resolution maps for conversation assignment."""

    workspace_entries: list[dict[str, Any]]
    invalid_workspace_ids: set[str]
    composer_id_to_workspace_id: dict[str, str]
    project_name_to_workspace_id: dict[str, str]
    workspace_path_to_id: dict[str, str]
    project_layouts_map: dict[str, list[str]]
    bubble_map: dict[str, Bubble]
    invalid_workspace_aliases: dict[str, str] | None = None


def _entries(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if workspace_entries is not None:
        return workspace_entries
    return collect_workspace_entries(workspace_path)


def _assemble_context(
    entries: list[dict[str, Any]],
    *,
    invalid_workspace_ids: set[str],
    workspace_path_to_id: dict[str, str],
    composer_id_to_workspace_id: dict[str, str],
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_entries=entries,
        invalid_workspace_ids=invalid_workspace_ids,
        composer_id_to_workspace_id=composer_id_to_workspace_id,
        project_name_to_workspace_id=create_project_name_to_workspace_id_map(entries),
        workspace_path_to_id=workspace_path_to_id,
        project_layouts_map={},
        bubble_map={},
    )


def resolve_workspace_context(
    workspace_path: str,
    *,
    workspace_entries: list[dict[str, Any]] | None = None,
) -> WorkspaceContext:
    """Full workspace maps with an uncached composer→workspace scan (CLI export)."""
    entries = _entries(workspace_path, workspace_entries)
    return _assemble_context(
        entries,
        invalid_workspace_ids=collect_invalid_workspace_ids(entries),
        workspace_path_to_id=create_workspace_path_to_id_map(entries),
        composer_id_to_workspace_id=build_composer_id_to_workspace_id(
            workspace_path, entries,
        ),
    )


def resolve_workspace_context_cached(
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    workspace_entries: list[dict[str, Any]] | None = None,
    nocache: bool = False,
) -> WorkspaceContext:
    """Full workspace maps with a mtime-keyed composer map (listing / tabs)."""
    entries = _entries(workspace_path, workspace_entries)
    return _assemble_context(
        entries,
        invalid_workspace_ids=collect_invalid_workspace_ids(entries),
        workspace_path_to_id=create_workspace_path_to_id_map(entries),
        composer_id_to_workspace_id=build_composer_id_to_workspace_id_cached(
            workspace_path, entries, rules, nocache=nocache,
        ),
    )


def resolve_workspace_context_minimal(
    workspace_path: str,
    *,
    workspace_entries: list[dict[str, Any]] | None = None,
) -> WorkspaceContext:
    """Entries, project-name, and composer maps only (HTTP export).

    Args:
        workspace_path: Cursor ``workspaceStorage`` root.
        workspace_entries: Pre-collected entries; when ``None``, scanned from disk.
    """
    entries = _entries(workspace_path, workspace_entries)
    return _assemble_context(
        entries,
        invalid_workspace_ids=set(),
        workspace_path_to_id={},
        composer_id_to_workspace_id=build_composer_id_to_workspace_id(
            workspace_path, entries,
        ),
    )


def enrich_workspace_context_from_global_db(
    ctx: WorkspaceContext,
    global_db: sqlite3.Connection,
    *,
    populate_project_layouts: bool = False,
    populate_bubble_map: bool = False,
) -> WorkspaceContext:
    """Return *ctx* with global KV maps loaded from an open global DB connection."""
    updates: dict[str, Any] = {}
    if populate_project_layouts:
        updates["project_layouts_map"] = load_project_layouts_map(global_db)
    if populate_bubble_map:
        updates["bubble_map"] = load_bubble_map(global_db)
    if not updates:
        return ctx
    return replace(ctx, **updates)


def resolve_invalid_workspace_aliases_cached(
    ctx: WorkspaceContext,
    global_db: sqlite3.Connection,
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
    project_layouts_map: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Return invalid-workspace alias map, using the summary-cache fingerprint.

    Computes ``infer_invalid_workspace_aliases`` at most once per storage
    fingerprint (same mtime key as composer-map / tab-summary caches). When
    *ctx* already carries a populated ``invalid_workspace_aliases`` field,
    that value is returned without touching disk or the global DB roster.

    Args:
        ctx: Workspace maps from :func:`resolve_workspace_context_cached`.
        global_db: Open global ``state.vscdb`` connection.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists (fingerprint input).
        nocache: When ``True``, bypass disk cache reads and writes.
        project_layouts_map: Pre-loaded layouts; loaded from *global_db* when
            ``None``.

    Returns:
        ``{invalid_id: replacement_id}``, or ``{}`` when every workspace is valid.
    """
    if ctx.invalid_workspace_aliases is not None:
        return ctx.invalid_workspace_aliases
    if not ctx.invalid_workspace_ids:
        return {}

    from services.summary_cache import (
        get_or_build_cached_invalid_workspace_aliases,
        nocache_enabled,
    )

    def build() -> dict[str, str]:
        layouts = (
            project_layouts_map
            if project_layouts_map is not None
            else load_project_layouts_map(global_db)
        )
        composer_rows = safe_fetchall(global_db, COMPOSER_ROWS_WITH_HEADERS_SQL)
        return infer_invalid_workspace_aliases(
            composer_rows=composer_rows,
            project_layouts_map=layouts,
            project_name_map=ctx.project_name_to_workspace_id,
            workspace_path_map=ctx.workspace_path_to_id,
            workspace_entries=ctx.workspace_entries,
            bubble_map={},
            composer_id_to_ws=ctx.composer_id_to_workspace_id,
            invalid_workspace_ids=ctx.invalid_workspace_ids,
        )

    if nocache_enabled(request_nocache=nocache):
        return build()

    return get_or_build_cached_invalid_workspace_aliases(
        workspace_path,
        ctx.workspace_entries,
        rules,
        build_fn=build,
    )


def with_invalid_workspace_aliases(
    ctx: WorkspaceContext,
    global_db: sqlite3.Connection,
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
    project_layouts_map: dict[str, list[str]] | None = None,
) -> WorkspaceContext:
    """Return *ctx* with ``invalid_workspace_aliases`` populated from cache."""
    if ctx.invalid_workspace_aliases is not None:
        return ctx
    aliases = resolve_invalid_workspace_aliases_cached(
        ctx,
        global_db,
        workspace_path,
        rules,
        nocache=nocache,
        project_layouts_map=project_layouts_map,
    )
    return replace(ctx, invalid_workspace_aliases=aliases)
