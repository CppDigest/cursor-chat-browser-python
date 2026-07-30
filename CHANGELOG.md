# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-07-30

### Added
- **Content-Security-Policy header** with nonce for inline bootstrap (#147)
- **highlight.js SRI pins** for script and theme (#145)
- **Playwright XSS tests** for `renderMarkdownSafe` (#151)

### Changed
- **Structured `{error, code}` bodies** across all API blueprints (#146)
- CI regression gate excludes `composers-50` export benchmark (#148)

### Fixed
- **HTTP 409** from `/api/set-workspace` when multiple WSGI workers are in use (#150)
- **Summary-cache fingerprint read-compare-write** serialised under a module lock (#158)

## [0.2.0] - 2026-07-17

### Added
- **Strict mypy** — `strict = true` in `pyproject.toml`; core TypedDict models
  (`SearchResult`, `ConversationSummary`) and full annotations on API routes and
  `utils/` (#100, #103)
- **Shared export engine** — web `POST /api/export` and CLI `cursor-chat-export`
  share `services/export_engine.py`; CLI wired to summary cache (#112)
- **FTS search index** — faster workspace listing and search on large global DBs (#113)
- **pytest-benchmark suite** with CI regression gate and stored baselines (#120, #121)
- **Bubble typed accessors** — `Bubble`/`Composer` boundary tightened with typed
  field access instead of raw dict reads (#133)
- **`RuleTokens` typing** — exclusion-rules consumers threaded end-to-end; mypy
  strict verifies the filter boundary (#134)
- **`raw_access` helpers** — shared `_optional_*` parsers for Bubble/Composer
  optional properties (#137)
- **Google-style docstrings** on public `api/`, `services/`, `utils/`, and
  `models/` surfaces (#119)
- **Flask API test coverage** for search, workspaces, and export routes (#101, #104)

### Changed
- CI typecheck job runs `mypy .` using pyproject config (strict production code;
  per-module overrides for `scripts/export.py` and `tests.*`) (#100)
- **Search handler** decomposed from a monolithic module into per-source functions (#99, #102)
- **`models/__init__.py` exports trimmed** — `BubbleRole` and `BubbleMetadata`
  import from `models.bubble_display` (#135)
- **`workspace_tabs` / `cursor_md_exporter` / `display_bubble` hygiene** — phase
  helpers, shared loops, narrowed exception handling, typed metadata returns (#138)
- **Invalid-workspace alias cache** keyed by storage fingerprint (#116, #125)
- Desktop optional dep `pywebview` range widened to `>=5.0,<7` (#58)

### Fixed
- Export workspace path override when `CURSOR_WORKSPACE_PATH` is set (#114)
- Search UI tooltip and distinct `/api/search` error codes (#117, #126)
- Per-composer tab assembly skips drifted metadata instead of 500ing the listing (#138)
- Tab metadata aggregation edge cases (`ctx_token_limit`, CLI meta guard,
  `tool_result_stats` for search/web-only sessions) (#138)

## [0.1.0] - 2026-06-04

### Added
- **Summary disk cache (Phase 3)** — project list and tab summaries cached under
  `~/.cache/cursor-chat-browser/`, invalidated when global or per-workspace DB
  mtimes change; bypass with `?nocache=1` or `CURSOR_CHAT_BROWSER_NOCACHE=1` (#84)
- **Lazy-load workspace UI** — workspace sidebar renders from a lightweight summary
  payload; full bubble content is fetched per-conversation when the user selects it,
  reducing first-paint time from 1–2 min to < 3 s on large local fixtures (#84)
- **`GET /api/workspaces/<id>/tabs?summary=1`** — new summary-only variant returns
  `id`, `title`, `timestamp`, `messageCount`, and optional `metadata.modelsUsed`
  without loading any bubble data (#84)
- **`GET /api/workspaces/<id>/tabs/<composer_id>`** — new single-conversation
  endpoint loads only scoped `bubbleId:{id}:%`, `messageRequestContext:{id}:%`,
  and `codeBlockDiff:{id}:%` KV rows, avoiding a full global bubble scan (#84)
- **Scoped KV loaders** in `services/workspace_db.py`:
  `load_bubbles_for_composer`, `load_message_request_context_for_composer`,
  `load_code_block_diffs_for_composer` — used by the single-tab path (#84)
- **Web UI** — browse and search all Cursor AI workspaces; conversation view with syntax-highlighted code blocks, dark/light mode, and bookmarkable chat URLs (#63)
- **Export formats** — one-click export of chats as Markdown, HTML, PDF, JSON, and CSV from the web UI (#63)
- **CLI export** (`cursor-chat-export` / `scripts/export.py`) — zip archive or individual Markdown files with YAML frontmatter; incremental mode (`--since last`) preserves state across runs (#63, #42, #61)
- **Cursor CLI agent session support** — browse and export sessions stored in `~/.cursor/chats/` by the `cursor agent` CLI; gracefully degrades when the IDE database is absent (#7, #8, #63)
- **Desktop app packaging** — Windows `.exe` via PyInstaller + pywebview; no Python installation required on the target machine (#63)
- **Type-safe models** with schema validation at SQLite read boundaries (#24, #30)
- **CI matrix** (Linux / macOS / Windows) running pytest, mypy, and gitleaks (#13, #19, #44, #62)
- **Python packaging infrastructure** (`pyproject.toml` with hatchling, bounded dependency pins, `requirements-lock.txt`, Dependabot) (#45, #47, #49, #53)
- Optional exclusion rules for sensitive projects and chats (#1, #2)
- Full-text search with workspace and log-type filters (#63)
- Hypothesis property-based tests for blob and bubble parsing (#71, #81)
- PDF export endpoint coverage in CI (#72)
- Unit tests for `determine_project_for_conversation` fallback chain (#87, #89)

### Changed
- **List-path performance** — skip full `messageRequestContext` scan unless
  invalid workspace aliases are needed; filter `composerData` in SQL; skip
  `Composer.from_dict` on list/summary paths; cache `composer_id_to_ws` mapping (#84)
- **`GET /api/workspaces`** (`list_workspace_projects`) no longer performs a
  global `bubbleId:%` scan; conversation presence is determined from
  `fullConversationHeadersOnly` headers alone, and workspace assignment relies
  on `composer_id_to_ws` (primary) plus `projectLayouts` from MRC (#84)
- **`assemble_workspace_tabs`** inner per-composer loop refactored into a shared
  `_assemble_tab_from_composer_data` helper reused by `assemble_single_tab`; full
  path behaviour is unchanged (#84)
- Extract shared `from_dict` validation helpers for model classes, reducing duplication (#70, #80)
- Enable mypy `strict-optional` and fix nullability gaps across the codebase (#69, #79)

### Deprecated
- Direct use of `GET /api/workspaces/<id>/tabs` (no `?summary=1`) from the workspace
  UI on page load; the UI now calls `?summary=1` for first paint and lazy-fetches
  individual tabs. The full-assembly endpoint remains available for export,
  search, and backward-compatible consumers (planned removal: post-1.0) (#84)

### Fixed
- Path traversal and symlink-escape protection on `/api/set-workspace` (#15, #22)
- Disabled Werkzeug debug mode by default; opt-in via `--debug` / `FLASK_DEBUG=1` (#9, #20)
- Sanitise Marked.js HTML output with DOMPurify (#11, #21)
- Wrapped all production `sqlite3.connect()` calls in context managers (#17, #23)
- Skip NULL bubble rows in workspace tabs loader (#50, #52)
- Thread-unsafe `_workspace_path_override` race condition (#43, #54)
- Normalise Windows-style paths on non-Windows hosts (#8)
- Add incomplete-result signaling on parse failure so callers can distinguish partial vs. complete data (#67, #78)
- Replace `print()` error output with structured logging throughout (#68, #77)
- Replace silent `except Exception: pass` with structured logging in workspace and bubble load paths (#66, #76)
- Decouple API handlers from private `_`-prefixed service internals (#73)

[Unreleased]: https://github.com/cppalliance/cppa-cursor-browser/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/cppalliance/cppa-cursor-browser/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/cppalliance/cppa-cursor-browser/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cppalliance/cppa-cursor-browser/releases/tag/v0.1.0
