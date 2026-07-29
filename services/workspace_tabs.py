from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

_logger = logging.getLogger(__name__)

from utils.path_helpers import (
    get_workspace_folder_paths,
    normalize_file_path,
    to_epoch_ms,
    warn_workspace_json_read,
)
from utils.exclusion_rules import RuleTokens, build_searchable_text, is_excluded_by_rules
from utils.display_bubble import (
    annotate_response_times,
    bubble_display_timestamp_ms,
    build_storage_bubble_metadata,
)
from utils.text_extract import extract_text_from_bubble
from utils.workspace_descriptor import read_json_file
from models import (
    Bubble,
    Composer,
    DisplayBubble,
    ParseWarningCollector,
)
from models.bubble_display import BubbleRole
from models.raw_access import (
    conversation_header_bubble_id,
    message_request_context_project_layouts,
)
from services.workspace_composer_scan import (
    assign_composer_workspace,
    composer_chat_title,
    composer_model_names,
    is_composer_excluded,
    parse_composer_data_row,
)
from services.summary_cache import (
    get_or_build_cached_tab_summaries,
    nocache_enabled,
)
from services.workspace_context import (
    resolve_workspace_context_cached,
    with_invalid_workspace_aliases,
)
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    collect_workspace_entries,
    load_bubble_map,
    load_bubbles_for_composer,
    load_code_block_diff_map,
    load_code_block_diffs_for_composer,
    load_message_request_context_for_composer,
    load_project_layouts_for_composer,
    load_project_layouts_map,
    open_global_db,
    safe_fetchall,
)
from services.workspace_resolver import (
    lookup_workspace_display_name,
    matching_workspace_ids_for_folder,
)


ERR_CONVERSATION_NOT_FOUND = "Conversation not found"
ERR_GLOBAL_STORAGE_NOT_FOUND = "Global storage not found"
CODE_CONVERSATION_NOT_FOUND = "conversation_not_found"
CODE_GLOBAL_STORAGE_NOT_FOUND = "global_storage_not_found"
_COMPOSER_ROW_ERRORS = (KeyError, TypeError, ValueError, json.JSONDecodeError)


def _record_composer_processing_failure(
    composer_id: str,
    exc: BaseException,
    *,
    parse_warnings: ParseWarningCollector,
) -> None:
    _logger.warning(
        "Failed to process Composer from composerData:%s: %s",
        composer_id,
        exc,
    )
    parse_warnings.record_composer_processing_failure()


def _loads_kv_value_logged(key: str, raw: object | None) -> Any | None:
    """Parse a cursorDiskKV ``value``; log and return ``None`` on decode failure."""
    if raw is None:
        return None
    if not isinstance(raw, (str, bytes, bytearray)):
        payload_len, payload_fp = _kv_payload_log_meta(raw)
        _logger.warning(
            "Failed to decode cursorDiskKV value for %s: unsupported type %s (payload_len=%d, payload_sha256=%s)",
            key,
            type(raw).__name__,
            payload_len,
            payload_fp,
        )
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        payload_len, payload_fp = _kv_payload_log_meta(raw)
        _logger.warning(
            "Failed to decode cursorDiskKV value for %s: %s (payload_len=%d, payload_sha256=%s)",
            key,
            e,
            payload_len,
            payload_fp,
        )
        return None


def _composer_tab_timestamp_ms(composer: Composer) -> int:
    if composer.last_updated_at is not None:
        return to_epoch_ms(composer.last_updated_at)
    if composer.created_at is not None:
        return to_epoch_ms(composer.created_at)
    return int(datetime.now().timestamp() * 1000)


def _kv_payload_log_meta(value: object | None) -> tuple[int, str | None]:
    """Byte length and short SHA-256 prefix for logs without emitting raw KV payloads."""
    if value is None:
        return 0, None
    if isinstance(value, bytes):
        payload = value
    else:
        payload = str(value).encode("utf-8", errors="replace")
    return len(payload), hashlib.sha256(payload).hexdigest()[:12]


def _context_text_for_bubble(
    bubble_id: str,
    contexts: list[dict[str, Any]],
) -> str:
    """Append message-request context blocks for one bubble."""
    parts: list[str] = []
    for ctx in contexts:
        if ctx.get("bubbleId") != bubble_id:
            continue
        if ctx.get("gitStatusRaw"):
            parts.append(f"\n\n**Git Status:**\n```\n{ctx['gitStatusRaw']}\n```")
        tf = ctx.get("terminalFiles")
        if isinstance(tf, list) and tf:
            parts.append("\n\n**Terminal Files:**")
            for f in tf:
                if not isinstance(f, dict):
                    continue
                parts.append(f"\n- {f.get('path', '')}")
        af = ctx.get("attachedFoldersListDirResults")
        if isinstance(af, list) and af:
            parts.append("\n\n**Attached Folders:**")
            for fld in af:
                if not isinstance(fld, dict):
                    continue
                files = fld.get("files")
                if isinstance(files, list) and files:
                    parts.append(f"\n\n**Folder:** {fld.get('path', 'Unknown')}")
                    for fi in files:
                        if not isinstance(fi, dict):
                            continue
                        parts.append(f"\n- {fi.get('name', '')} ({fi.get('type', '')})")
        cr = ctx.get("cursorRules")
        if isinstance(cr, list) and cr:
            parts.append("\n\n**Cursor Rules:**")
            for rule in cr:
                if not isinstance(rule, dict):
                    continue
                parts.append(f"\n- {rule.get('name') or rule.get('description') or 'Rule'}")
        sc = ctx.get("summarizedComposers")
        if isinstance(sc, list) and sc:
            parts.append("\n\n**Related Conversations:**")
            for comp in sc:
                if not isinstance(comp, dict):
                    continue
                parts.append(f"\n- {comp.get('name') or comp.get('composerId') or 'Conversation'}")
    return "".join(parts)


def _build_tab_display_bubbles(
    composer_id: str,
    composer: Composer,
    bubble_map: Mapping[str, Bubble],
    contexts: list[dict[str, Any]],
) -> list[DisplayBubble]:
    """Build renderable display bubbles from composer headers and storage."""
    bubbles: list[DisplayBubble] = []
    for header in composer.full_conversation_headers_only:
        if not isinstance(header, dict):
            continue
        bubble_id = conversation_header_bubble_id(header, composer_id=composer_id)
        if not bubble_id:
            continue
        bubble = bubble_map.get(bubble_id)
        if bubble is None:
            continue

        is_user = header.get("type") == 1
        msg_type: BubbleRole = "user" if is_user else "ai"
        text = extract_text_from_bubble(bubble)
        full_text = text + _context_text_for_bubble(bubble_id, contexts)
        metadata = build_storage_bubble_metadata(bubble, msg_type)
        tool_calls = (metadata or {}).get("toolCalls")
        thinking = (metadata or {}).get("thinking")
        has_content = full_text.strip() or tool_calls or thinking
        if not has_content:
            continue

        display_text = full_text.strip()
        if not display_text and tool_calls:
            tc = tool_calls[0]
            if isinstance(tc, dict):
                display_text = f"**Tool: {tc.get('name', 'unknown')}**"
                if tc.get("status"):
                    display_text += f" ({tc['status']})"
        if not display_text and thinking:
            display_text = thinking

        b_entry: DisplayBubble = {
            "type": msg_type,
            "text": display_text,
            "timestamp": bubble_display_timestamp_ms(bubble),
        }
        if metadata:
            b_entry["metadata"] = metadata
        bubbles.append(b_entry)
    return bubbles


def _derive_composer_tab_title(
    composer_id: str,
    composer: Composer,
    bubbles: list[DisplayBubble],
) -> str:
    """Derive sidebar title from composer name or first user message."""
    title = composer.name or f"Conversation {composer_id[:8]}"
    if not composer.name and bubbles:
        first_msg = bubbles[0].get("text", "")
        if first_msg:
            first_lines = [ln for ln in first_msg.split("\n") if ln.strip()]
            if first_lines:
                title = first_lines[0][:100]
                if len(title) == 100:
                    title += "..."
    return title


def _aggregate_tab_metadata(
    bubbles: list[DisplayBubble],
    composer: Composer,
) -> dict[str, Any] | None:
    """Aggregate bubble metadata into tab-level usage statistics."""
    total_input = 0
    total_output = 0
    total_cached = 0
    total_response_ms = 0
    total_cost = 0.0
    total_tool_calls = 0
    total_thinking_ms = 0.0
    models_set: set[str] = set()
    max_ctx_tokens = 0
    ctx_token_limit = 0
    for b in bubbles:
        m = b.get("metadata") or {}
        if m.get("inputTokens"):
            total_input += m["inputTokens"]
        if m.get("outputTokens"):
            total_output += m["outputTokens"]
        if m.get("cachedTokens"):
            total_cached += m["cachedTokens"]
        if m.get("responseTimeMs"):
            total_response_ms += m["responseTimeMs"]
        if m.get("cost") is not None:
            total_cost += m["cost"]
        if m.get("modelName"):
            models_set.add(m["modelName"])
        if m.get("toolCalls"):
            total_tool_calls += len(m["toolCalls"])
        if m.get("thinkingDurationMs"):
            total_thinking_ms += m["thinkingDurationMs"]
        if m.get("contextTokensUsed", 0) > max_ctx_tokens:
            max_ctx_tokens = m["contextTokensUsed"]
        if m.get("contextTokenLimit", 0) > ctx_token_limit:
            ctx_token_limit = m["contextTokenLimit"]

    usage = composer.usage_data
    composer_cost = usage.get("cost") or usage.get("estimatedCost")
    if isinstance(composer_cost, (int, float)) and total_cost == 0:
        total_cost = composer_cost

    lines_added = composer.total_lines_added
    lines_removed = composer.total_lines_removed
    files_added = composer.added_files
    files_removed = composer.removed_files

    has_any = any([
        total_input, total_output, total_cached, total_response_ms,
        total_cost, models_set, total_tool_calls, total_thinking_ms,
        lines_added, lines_removed, files_added, files_removed,
        max_ctx_tokens, ctx_token_limit,
    ])
    if not has_any:
        tab_meta: dict[str, Any] | None = None
    else:
        tab_meta_raw = {
            "totalInputTokens": total_input or None,
            "totalOutputTokens": total_output or None,
            "totalCachedTokens": total_cached or None,
            "modelsUsed": list(models_set) if models_set else None,
            "totalResponseTimeMs": total_response_ms or None,
            "totalCost": total_cost if total_cost > 0 else None,
            "totalToolCalls": total_tool_calls or None,
            "totalThinkingDurationMs": total_thinking_ms or None,
            "totalLinesAdded": lines_added if lines_added else None,
            "totalLinesRemoved": lines_removed if lines_removed else None,
            "totalFilesAdded": files_added if files_added else None,
            "totalFilesRemoved": files_removed if files_removed else None,
            "maxContextTokensUsed": max_ctx_tokens if max_ctx_tokens else None,
            "contextTokenLimit": ctx_token_limit if ctx_token_limit else None,
        }
        tab_meta = {k: v for k, v in tab_meta_raw.items() if v is not None}

    model_name_from_config = composer.model_name_from_config()
    if model_name_from_config and model_name_from_config != "default":
        if not tab_meta:
            tab_meta = {}
        models_used = tab_meta.get("modelsUsed")
        if not isinstance(models_used, list):
            tab_meta["modelsUsed"] = [model_name_from_config]
        elif model_name_from_config not in models_used:
            models_used.insert(0, model_name_from_config)

    return tab_meta


def _build_tab_dict(
    composer_id: str,
    title: str,
    composer: Composer,
    bubbles: list[DisplayBubble],
    code_block_diffs: list[dict[str, Any]],
    tab_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the final tab payload dict."""
    tab: dict[str, Any] = {
        "id": composer_id,
        "title": title,
        "timestamp": _composer_tab_timestamp_ms(composer),
        "bubbles": [{
            "type": b["type"],
            "text": b.get("text", ""),
            "timestamp": b.get("timestamp", 0),
            **({"metadata": b["metadata"]} if b.get("metadata") else {}),
        } for b in bubbles],
        "codeBlockDiffs": code_block_diffs,
    }
    if tab_meta:
        tab["metadata"] = tab_meta
    return tab


def _iter_assigned_workspace_composers(
    composer_rows: list[sqlite3.Row],
    *,
    parse_warnings: ParseWarningCollector,
    project_layouts_map: dict[str, list[str]],
    project_name_map: dict[str, str],
    workspace_path_map: dict[str, str],
    workspace_entries: list[dict[str, Any]],
    composer_id_to_ws: dict[str, str],
    invalid_workspace_ids: set[str],
    invalid_workspace_aliases: dict[str, str],
    matching_ws_ids: set[str],
    bubble_map: Mapping[str, Bubble],
) -> Iterator[tuple[Composer, str]]:
    """Yield composers assigned to the requested workspace, skipping parse failures."""
    for row in composer_rows:
        composer = parse_composer_data_row(
            row["key"], row["value"], parse_warnings=parse_warnings,
        )
        if composer is None:
            continue
        composer_id = composer.composer_id
        try:
            assigned = assign_composer_workspace(
                composer,
                project_layouts_map=project_layouts_map,
                project_name_map=project_name_map,
                workspace_path_map=workspace_path_map,
                workspace_entries=workspace_entries,
                bubble_map=bubble_map,
                composer_id_to_ws=composer_id_to_ws,
                invalid_workspace_ids=invalid_workspace_ids,
                invalid_workspace_aliases=invalid_workspace_aliases,
            )
            if assigned not in matching_ws_ids:
                continue
            yield composer, composer_id
        except _COMPOSER_ROW_ERRORS as e:
            _record_composer_processing_failure(
                composer_id, e, parse_warnings=parse_warnings,
            )


def _assemble_tab_from_composer_data(
    composer_id: str,
    composer: Composer,
    bubble_map: Mapping[str, Bubble],
    contexts: list[dict[str, Any]],
    code_block_diffs: list[dict[str, Any]],
    workspace_display_name: str,
    rules: list[RuleTokens],
    parse_warnings: ParseWarningCollector,
) -> dict[str, Any] | None:
    """Assemble a single tab dict from a validated :class:`Composer`.

    Args:
        composer_id: Composer UUID.
        composer: Validated composer model (typed field access, not raw dict reads).
        bubble_map: ``{bubble_id: Bubble}`` — global or scoped.
        contexts: ``messageRequestContext`` entries for *this* composer
            (list of dicts, each with an injected ``contextId`` key and a
            ``bubbleId`` field from the JSON value).
        code_block_diffs: ``codeBlockDiff`` entries for *this* composer.
        workspace_display_name: Human-readable workspace name for rule matching.
        rules: Exclusion rule token lists.
        parse_warnings: Collector for skipped-bubble warnings.

    Returns:
        A tab dict on success, or ``None`` when the tab should be omitted
        (no renderable bubbles or excluded by rules).
    """
    bubbles = _build_tab_display_bubbles(
        composer_id, composer, bubble_map, contexts,
    )
    if not bubbles:
        return None

    title = _derive_composer_tab_title(composer_id, composer, bubbles)

    if is_excluded_by_rules(rules, build_searchable_text(
        project_name=workspace_display_name,
        chat_title=title,
        model_names=composer_model_names(composer),
    )):
        return None

    bubbles.sort(key=lambda b: b.get("timestamp") or 0)
    annotate_response_times(bubbles)

    tab_meta = _aggregate_tab_metadata(bubbles, composer)
    return _build_tab_dict(
        composer_id, title, composer, bubbles, code_block_diffs, tab_meta,
    )


def _safe_assemble_tab_from_composer_data(
    composer_id: str,
    composer: Composer,
    bubble_map: Mapping[str, Bubble],
    contexts: list[dict[str, Any]],
    code_block_diffs: list[dict[str, Any]],
    workspace_display_name: str,
    rules: list[RuleTokens],
    parse_warnings: ParseWarningCollector,
) -> dict[str, Any] | None:
    """Like :func:`_assemble_tab_from_composer_data`, but skip+log on drift errors."""
    try:
        return _assemble_tab_from_composer_data(
            composer_id=composer_id,
            composer=composer,
            bubble_map=bubble_map,
            contexts=contexts,
            code_block_diffs=code_block_diffs,
            workspace_display_name=workspace_display_name,
            rules=rules,
            parse_warnings=parse_warnings,
        )
    except Exception as e:
        _record_composer_processing_failure(
            composer_id, e, parse_warnings=parse_warnings,
        )
        return None


def _build_matching_ws_ids(
    workspace_id: str,
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
) -> set[str]:
    """Return workspace folder IDs sharing the same on-disk project folder."""
    return matching_workspace_ids_for_folder(workspace_id, workspace_path, workspace_entries)


def list_workspace_tab_summaries(
    workspace_id: str,
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
) -> tuple[dict[str, Any], int]:
    """Return summary tab list for GET /api/workspaces/<id>/tabs?summary=1.

    Does **not** load the global ``bubbleId:%`` index.  Each tab entry contains
    only the fields needed by the sidebar: ``id``, ``title``, ``timestamp``,
    ``messageCount``, and an optional ``metadata.modelsUsed``.  Full bubble
    bodies are omitted; the UI fetches them on demand via
    ``GET /api/workspaces/<id>/tabs/<composer_id>``.

    Args:
        workspace_id: Workspace folder name, or ``"global"`` for unassigned chats.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists.

    Returns:
        ``(payload, status)`` — same envelope as :func:`assemble_workspace_tabs`
        but ``tabs`` entries carry no ``bubbles`` field.
    """
    workspace_entries = collect_workspace_entries(workspace_path)

    def build() -> tuple[dict[str, Any], int]:
        return _build_workspace_tab_summaries_uncached(
            workspace_id, workspace_path, rules, workspace_entries, nocache=nocache,
        )

    if nocache_enabled(request_nocache=nocache):
        return build()

    return get_or_build_cached_tab_summaries(
        workspace_path,
        workspace_entries,
        rules,
        workspace_id,
        build_fn=build,
    )


def _build_workspace_tab_summaries_uncached(
    workspace_id: str,
    workspace_path: str,
    rules: list[RuleTokens],
    workspace_entries: list[dict[str, Any]],
    *,
    nocache: bool,
) -> tuple[dict[str, Any], int]:
    parse_warnings = ParseWarningCollector()
    response: dict[str, Any] = {"tabs": []}

    ctx = resolve_workspace_context_cached(
        workspace_path,
        rules,
        workspace_entries=workspace_entries,
        nocache=nocache,
    )
    invalid_workspace_ids = ctx.invalid_workspace_ids
    project_name_map = ctx.project_name_to_workspace_id
    workspace_path_map = ctx.workspace_path_to_id
    composer_id_to_ws = ctx.composer_id_to_workspace_id
    matching_ws_ids = _build_matching_ws_ids(workspace_id, workspace_path, workspace_entries)

    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": ERR_GLOBAL_STORAGE_NOT_FOUND, "code": CODE_GLOBAL_STORAGE_NOT_FOUND}, 404

        workspace_display_name = lookup_workspace_display_name(workspace_path, workspace_id)

        project_layouts_map = load_project_layouts_map(global_db)

        composer_rows = safe_fetchall(global_db, COMPOSER_ROWS_WITH_HEADERS_SQL)

        ctx = with_invalid_workspace_aliases(
            ctx,
            global_db,
            workspace_path,
            rules,
            nocache=nocache,
            project_layouts_map=project_layouts_map,
        )
        invalid_workspace_aliases = ctx.invalid_workspace_aliases or {}

        for composer, composer_id in _iter_assigned_workspace_composers(
            composer_rows,
            parse_warnings=parse_warnings,
            project_layouts_map=project_layouts_map,
            project_name_map=project_name_map,
            workspace_path_map=workspace_path_map,
            workspace_entries=workspace_entries,
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids=invalid_workspace_ids,
            invalid_workspace_aliases=invalid_workspace_aliases,
            matching_ws_ids=matching_ws_ids,
            bubble_map={},
        ):
            if is_composer_excluded(
                rules,
                project_name=workspace_display_name,
                composer=composer,
            ):
                continue

            headers = composer.full_conversation_headers_only
            title = composer_chat_title(composer)

            _early_model_names = composer_model_names(composer)
            tab_meta: dict[str, Any] | None = None
            if _early_model_names:
                tab_meta = {"modelsUsed": _early_model_names}

            tab_entry: dict[str, Any] = {
                "id": composer_id,
                "title": title,
                "timestamp": _composer_tab_timestamp_ms(composer),
                "messageCount": len(headers),
            }
            if tab_meta:
                tab_entry["metadata"] = tab_meta
            response["tabs"].append(tab_entry)

        response["tabs"].sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
        return parse_warnings.attach_to(response), 200


def assemble_single_tab(
    workspace_id: str,
    composer_id: str,
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
) -> tuple[dict[str, Any], int]:
    """Assemble a single conversation tab for GET /api/workspaces/<id>/tabs/<composer_id>.

    Loads only the KV rows scoped to *composer_id* (``bubbleId:{id}:%``,
    ``messageRequestContext:{id}:%``, ``codeBlockDiff:{id}:%``) instead of
    performing a full global scan.

    Args:
        workspace_id: Workspace folder name, or ``"global"``.
        composer_id: UUID of the composer / conversation to assemble.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists.
        nocache: When ``True``, bypass alias disk cache reads and writes.

    Returns:
        ``(payload, status)``.  On success (``200``), *payload* is
        ``{"tab": {...}}``, optionally with ``"warnings"``.  ``404`` when the
        global DB is missing, the composer is not found, or it is not assigned
        to *workspace_id* (``{"error", "code"}`` with
        ``global_storage_not_found`` or ``conversation_not_found``).
    """
    parse_warnings = ParseWarningCollector()

    ctx = resolve_workspace_context_cached(
        workspace_path, rules, nocache=nocache,
    )
    workspace_entries = ctx.workspace_entries
    invalid_workspace_ids = ctx.invalid_workspace_ids
    project_name_map = ctx.project_name_to_workspace_id
    workspace_path_map = ctx.workspace_path_to_id
    composer_id_to_ws = ctx.composer_id_to_workspace_id
    matching_ws_ids = _build_matching_ws_ids(workspace_id, workspace_path, workspace_entries)

    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": ERR_GLOBAL_STORAGE_NOT_FOUND, "code": CODE_GLOBAL_STORAGE_NOT_FOUND}, 404

        workspace_display_name = lookup_workspace_display_name(workspace_path, workspace_id)

        rows = safe_fetchall(
            global_db,
            "SELECT key, value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        )
        if not rows:
            return {"error": ERR_CONVERSATION_NOT_FOUND, "code": CODE_CONVERSATION_NOT_FOUND}, 404

        row = rows[0]
        composer = parse_composer_data_row(
            row["key"], row["value"], parse_warnings=parse_warnings,
        )
        if composer is None:
            return {"error": ERR_CONVERSATION_NOT_FOUND, "code": CODE_CONVERSATION_NOT_FOUND}, 404

        project_layouts_map: dict[str, list[str]] = {}
        project_layouts_map[composer_id] = load_project_layouts_for_composer(
            global_db, composer_id,
        )
        ctx = with_invalid_workspace_aliases(
            ctx, global_db, workspace_path, rules, nocache=nocache,
        )
        invalid_workspace_aliases = ctx.invalid_workspace_aliases or {}

        bubble_map = load_bubbles_for_composer(
            global_db, composer_id, parse_warnings=parse_warnings,
        )
        assigned = assign_composer_workspace(
            composer,
            project_layouts_map=project_layouts_map,
            project_name_map=project_name_map,
            workspace_path_map=workspace_path_map,
            workspace_entries=workspace_entries,
            bubble_map=bubble_map,
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids=invalid_workspace_ids,
            invalid_workspace_aliases=invalid_workspace_aliases,
        )

        if assigned not in matching_ws_ids:
            return {"error": ERR_CONVERSATION_NOT_FOUND, "code": CODE_CONVERSATION_NOT_FOUND}, 404

        contexts = load_message_request_context_for_composer(global_db, composer_id)
        code_block_diffs = load_code_block_diffs_for_composer(global_db, composer_id)

        tab = _assemble_tab_from_composer_data(
            composer_id=composer_id,
            composer=composer,
            bubble_map=bubble_map,
            contexts=contexts,
            code_block_diffs=code_block_diffs,
            workspace_display_name=workspace_display_name,
            rules=rules,
            parse_warnings=parse_warnings,
        )

        if tab is None:
            return {"error": ERR_CONVERSATION_NOT_FOUND, "code": CODE_CONVERSATION_NOT_FOUND}, 404

        response: dict[str, Any] = {"tab": tab}
        return parse_warnings.attach_to(response), 200


def assemble_workspace_tabs(
    workspace_id: str,
    workspace_path: str,
    rules: list[RuleTokens],
    *,
    nocache: bool = False,
) -> tuple[dict[str, Any], int]:
    """Build tabs payload for GET /api/workspaces/<id>/tabs (IDE workspaces).

    Args:
        workspace_id: Workspace folder name, or ``"global"`` for unassigned chats.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists from :func:`utils.exclusion_rules.load_rules`.
        nocache: When ``True``, bypass alias disk cache reads and writes.

    Returns:
        ``(payload, status)``. On success (``200``), *payload* contains ``tabs``
        (list of tab dicts with ``id``, ``title``, ``timestamp``, ``bubbles``,
        optional ``metadata`` / ``codeBlockDiffs``) and optional ``warnings``
        when parse failures were skipped. On failure (``404``), *payload* is
        ``{"error": "...", "code": "global_storage_not_found"}``.
    """
    parse_warnings = ParseWarningCollector()
    response: dict[str, Any] = {"tabs": []}

    ctx = resolve_workspace_context_cached(
        workspace_path, rules, nocache=nocache,
    )
    workspace_entries = ctx.workspace_entries
    invalid_workspace_ids = ctx.invalid_workspace_ids
    project_name_map = ctx.project_name_to_workspace_id
    workspace_path_map = ctx.workspace_path_to_id
    composer_id_to_ws = ctx.composer_id_to_workspace_id
    matching_ws_ids = _build_matching_ws_ids(workspace_id, workspace_path, workspace_entries)

    code_block_diff_map: dict[str, list[dict[str, Any]]] = {}
    message_request_context_map: dict[str, list[dict[str, Any]]] = {}

    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": ERR_GLOBAL_STORAGE_NOT_FOUND, "code": CODE_GLOBAL_STORAGE_NOT_FOUND}, 404

        workspace_display_name = lookup_workspace_display_name(workspace_path, workspace_id)

        bubble_map = load_bubble_map(global_db, parse_warnings=parse_warnings)

        code_block_diff_map = load_code_block_diff_map(global_db)

        project_layouts_map: dict[str, list[str]] = {}
        for row in safe_fetchall(
            global_db,
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'",
        ):
            parts = row["key"].split(":")
            if len(parts) < 2:
                continue
            chat_id = parts[1]
            mrc = _loads_kv_value_logged(row["key"], row["value"])
            if not isinstance(mrc, dict):
                continue

            # Per-bubble context map (needs the contextId at parts[2])
            if len(parts) >= 3:
                context_id = parts[2]
                message_request_context_map.setdefault(chat_id, []).append({
                    **mrc,
                    "contextId": context_id,
                })

            # Project-layout map (root paths used by the resolver)
            layouts = message_request_context_project_layouts(mrc, composer_id=chat_id)
            if layouts:
                project_layouts_map.setdefault(chat_id, [])
                for layout in layouts:
                    if isinstance(layout, str):
                        layout = _loads_kv_value_logged(
                            f"{row['key']}:projectLayout",
                            layout,
                        )
                        if not isinstance(layout, dict):
                            continue
                    if isinstance(layout, dict) and layout.get("rootPath"):
                        project_layouts_map[chat_id].append(layout["rootPath"])

        composer_rows = safe_fetchall(global_db, COMPOSER_ROWS_WITH_HEADERS_SQL)

        ctx = with_invalid_workspace_aliases(
            ctx,
            global_db,
            workspace_path,
            rules,
            nocache=nocache,
            project_layouts_map=project_layouts_map,
        )
        invalid_workspace_aliases = ctx.invalid_workspace_aliases or {}

        for composer, composer_id in _iter_assigned_workspace_composers(
            composer_rows,
            parse_warnings=parse_warnings,
            project_layouts_map=project_layouts_map,
            project_name_map=project_name_map,
            workspace_path_map=workspace_path_map,
            workspace_entries=workspace_entries,
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids=invalid_workspace_ids,
            invalid_workspace_aliases=invalid_workspace_aliases,
            matching_ws_ids=matching_ws_ids,
            bubble_map={},
        ):
            tab = _safe_assemble_tab_from_composer_data(
                composer_id=composer_id,
                composer=composer,
                bubble_map=bubble_map,
                contexts=message_request_context_map.get(composer_id, []),
                code_block_diffs=code_block_diff_map.get(composer_id, []),
                workspace_display_name=workspace_display_name,
                rules=rules,
                parse_warnings=parse_warnings,
            )
            if tab is not None:
                response["tabs"].append(tab)

        response["tabs"].sort(key=lambda t: t.get("timestamp") or 0, reverse=True)

        return parse_warnings.attach_to(response), 200
