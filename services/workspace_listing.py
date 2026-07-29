from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

_logger = logging.getLogger(__name__)

from utils.cli_chat_reader import list_cli_projects
from utils.exclusion_rules import RuleTokens, build_searchable_text, is_excluded_by_rules
from utils.path_helpers import (
    get_workspace_folder_paths,
    normalize_file_path,
    to_epoch_ms,
    warn_workspace_json_read,
)
from utils.workspace_descriptor import read_json_file
from models import Bubble, ParseWarningCollector
from services.export_engine import WorkspaceOrchestration, prepare_workspace_orchestration
from services.workspace_composer_scan import (
    assign_composer_workspace,
    composer_chat_title,
    composer_model_names,
    parse_composer_data_row,
)
from services.summary_cache import (
    get_or_build_cached_projects,
    nocache_enabled,
)
from services.workspace_context import resolve_invalid_workspace_aliases_cached
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    collect_workspace_entries,
    global_storage_db_path,
    load_project_layouts_map,
    open_global_db,
    safe_fetchall,
)
from utils.workspace_path import get_cli_chats_path
from services.workspace_resolver import (
    build_composer_ids_by_workspace,
    infer_workspace_name_from_layouts,
    lookup_workspace_display_name,
)


def list_workspace_projects(
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """List workspace projects for GET /api/workspaces.

    Args:
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists from :func:`utils.exclusion_rules.load_rules`.
        nocache: When ``True``, skip the mtime-keyed disk cache (Phase 3).

    Returns:
        ``(projects, warnings)``. Each project dict has ``id``, ``name``,
        ``path`` (``workspace.json`` path), ``conversationCount``,
        ``lastModified`` (ISO 8601), and optional ``aliasIds`` / ``source``
        (``"cli"`` for Cursor CLI projects). *warnings* is a list of structured
        parse-error dicts (``type``, ``count``, ``detail``) from
        :meth:`models.ParseWarningCollector.to_api_list`; empty when no skips.
    """
    effective_nocache = nocache_enabled(request_nocache=nocache)
    if effective_nocache:
        orch = prepare_workspace_orchestration(
            workspace_path,
            rules,
            nocache=True,
        )
        return _build_workspace_projects_uncached(
            workspace_path, rules, orch, nocache=True,
        )

    workspace_entries = collect_workspace_entries(workspace_path)

    def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        orch = prepare_workspace_orchestration(
            workspace_path,
            rules,
            nocache=False,
            workspace_entries=workspace_entries,
        )
        return _build_workspace_projects_uncached(
            workspace_path, rules, orch, nocache=False,
        )

    return get_or_build_cached_projects(
        workspace_path,
        workspace_entries,
        rules,
        build_fn=build,
    )


def _build_workspace_projects_uncached(
    workspace_path: str,
    rules: list[RuleTokens],
    orch: WorkspaceOrchestration,
    *,
    nocache: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parse_warnings = ParseWarningCollector()
    ctx = orch.ctx
    workspace_entries = orch.workspace_entries
    invalid_workspace_ids = ctx.invalid_workspace_ids
    project_name_map = ctx.project_name_to_workspace_id
    workspace_path_map = ctx.workspace_path_to_id
    composer_id_to_ws = ctx.composer_id_to_workspace_id
    composer_ids_by_ws = build_composer_ids_by_workspace(composer_id_to_ws)

    conversation_map: dict[str, list[dict[str, Any]]] = {}
    project_layouts_map: dict[str, list[str]] = {}

    with open_global_db(workspace_path) as (global_db, _):
        if global_db:
            try:
                composer_rows = safe_fetchall(global_db, COMPOSER_ROWS_WITH_HEADERS_SQL)
                project_layouts_map = load_project_layouts_map(global_db)

                bubble_map: dict[str, Bubble] = {}
                invalid_workspace_aliases = resolve_invalid_workspace_aliases_cached(
                    ctx,
                    global_db,
                    workspace_path,
                    rules,
                    nocache=nocache,
                    project_layouts_map=project_layouts_map,
                )

                for row in composer_rows:
                    composer = parse_composer_data_row(
                        row["key"], row["value"], parse_warnings=parse_warnings,
                    )
                    if composer is None:
                        continue
                    cid = composer.composer_id
                    try:
                        assigned = assign_composer_workspace(
                            composer,
                            project_layouts_map=project_layouts_map,
                            project_name_map=project_name_map,
                            workspace_path_map=workspace_path_map,
                            workspace_entries=workspace_entries,
                            # Empty bubble map matches summary assignment (#95 perf tradeoff).
                            bubble_map=bubble_map,
                            composer_id_to_ws=composer_id_to_ws,
                            invalid_workspace_ids=invalid_workspace_ids,
                            invalid_workspace_aliases=invalid_workspace_aliases,
                        )

                        conversation_map.setdefault(assigned, []).append({
                            "composerId": cid,
                            "name": composer_chat_title(composer),
                            "modelNames": composer_model_names(composer),
                            "lastUpdatedAt": (
                                to_epoch_ms(composer.last_updated_at)
                                or to_epoch_ms(composer.created_at)
                                or 0
                            ),
                            "createdAt": to_epoch_ms(composer.created_at) or 0,
                        })
                    except Exception as e:
                        _logger.warning(
                            "Failed to process Composer from composerData:%s: %s",
                            cid,
                            e,
                        )
                        parse_warnings.record_composer_processing_failure()
            except Exception as e:
                _logger.error(
                    "Failed to load composer rows from global storage: %s",
                    e,
                    exc_info=True,
                )

    # Group workspace entries by normalized folder path (first folder in workspace.json).
    folder_to_entries: dict[str, list[dict[str, Any]]] = {}
    entry_folder_map: dict[str, str] = {}
    for entry in workspace_entries:
        norm_folder = ""
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            folders = get_workspace_folder_paths(wd)
            first_folder = folders[0] if folders else None
            if first_folder:
                norm_folder = normalize_file_path(first_folder)
        except Exception as e:
            warn_workspace_json_read(_logger, entry["name"], e)
        if not norm_folder:
            norm_folder = entry["name"]
        entry_folder_map[entry["name"]] = norm_folder
        folder_to_entries.setdefault(norm_folder, []).append(entry)

    projects: list[dict[str, Any]] = []
    seen_folders: set[str] = set()
    for entry in workspace_entries:
        norm_folder = entry_folder_map[entry["name"]]
        if norm_folder in seen_folders:
            continue
        seen_folders.add(norm_folder)

        group = folder_to_entries[norm_folder]
        primary = group[0]
        all_ws_ids = [e["name"] for e in group]

        try:
            mtime = max(
                os.path.getmtime(os.path.join(workspace_path, e["name"], "state.vscdb"))
                for e in group
                if os.path.isfile(os.path.join(workspace_path, e["name"], "state.vscdb"))
            )
        except Exception as e:
            _logger.warning(
                "Failed to resolve mtime for workspace folder %s: %s",
                norm_folder,
                e,
            )
            mtime = 0

        workspace_name = lookup_workspace_display_name(workspace_path, primary["name"])
        if workspace_name == primary["name"]:
            inferred = infer_workspace_name_from_layouts(
                [cid for ws_id in all_ws_ids for cid in composer_ids_by_ws.get(ws_id, [])],
                project_layouts_map,
            )
            workspace_name = inferred or f"Project {primary['name'][:8]}"

        if is_excluded_by_rules(rules, workspace_name):
            continue

        convos = []
        for ws_id in all_ws_ids:
            for c in conversation_map.get(ws_id, []):
                searchable = build_searchable_text(
                    project_name=workspace_name,
                    chat_title=c.get("name"),
                    model_names=c.get("modelNames"),
                )
                if not is_excluded_by_rules(rules, searchable):
                    convos.append(c)

        if not convos:
            continue

        projects.append({
            "id": primary["name"],
            "name": workspace_name,
            "path": primary["workspaceJsonPath"],
            "conversationCount": len(convos),
            "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            **({"aliasIds": all_ws_ids} if len(all_ws_ids) > 1 else {}),
        })

    # Global (unmatched) conversations
    global_convos = [
        c for c in conversation_map.get("global", [])
        if not is_excluded_by_rules(
            rules,
            build_searchable_text(
                project_name="Other chats",
                chat_title=c.get("name"),
                model_names=c.get("modelNames"),
            ),
        )
    ]
    if global_convos:
        last_updated = max((c.get("lastUpdatedAt") or 0 for c in global_convos), default=0)
        projects.append({
            "id": "global",
            "name": "Other chats",
            "conversationCount": len(global_convos),
            "lastModified": (
                datetime.fromtimestamp(last_updated / 1000, tz=timezone.utc).isoformat()
                if last_updated > 0
                else datetime.now(tz=timezone.utc).isoformat()
            ),
        })

    # Cursor CLI projects
    try:
        cli_projects = list_cli_projects(get_cli_chats_path())
        for cp in cli_projects:
            if not isinstance(cp, dict):
                continue
            project_id = cp.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                continue
            ws_name = cp.get("workspace_name") or project_id[:12]
            if is_excluded_by_rules(rules, ws_name):
                continue
            sessions = cp.get("sessions") or []
            if not isinstance(sessions, list):
                continue
            cli_convos = []
            for s in sessions:
                if not isinstance(s, dict):
                    continue
                session_id = s.get("session_id")
                if not session_id:
                    continue
                meta = s.get("meta") or {}
                session_name = meta.get("name") or f"Session {session_id[:8]}"
                searchable = build_searchable_text(
                    project_name=ws_name,
                    chat_title=session_name,
                )
                if not is_excluded_by_rules(rules, searchable):
                    cli_convos.append(session_name)
            if not cli_convos:
                continue
            last_ms = cp.get("last_updated_ms")
            projects.append({
                "id": f"cli:{project_id}",
                "name": ws_name,
                "conversationCount": len(cli_convos),
                "lastModified": (
                    datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
                    if last_ms
                    else datetime.now(tz=timezone.utc).isoformat()
                ),
                "source": "cli",
            })
    except Exception as e:
        _logger.warning("Failed to load CLI projects: %s", e)

    projects.sort(key=lambda p: p["lastModified"], reverse=True)
    return projects, parse_warnings.to_api_list()
