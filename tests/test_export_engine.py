"""Unit tests for services.export_engine orchestration."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models import Bubble  # noqa: E402
from services.export_engine import (  # noqa: E402
    GlobalDbExportData,
    WorkspaceOrchestration,
    _collect_ide_export_entries,
    collect_export_entries,
    read_last_export_ms,
)
from utils.exclusion_rules import load_rules  # noqa: E402
from utils.text_extract import slug  # noqa: E402


class _TempExportPathsMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_ws = os.path.join(self._tmp.name, "ws")
        self.tmp_out = os.path.join(self._tmp.name, "out")
        os.makedirs(self.tmp_ws, exist_ok=True)
        os.makedirs(self.tmp_out, exist_ok=True)


def _fake_composer_row(composer_id: str, cd: dict[str, object]) -> object:
    class FakeRow:
        def __getitem__(self, key: str) -> str:
            if key == "key":
                return f"composerData:{composer_id}"
            return json.dumps(cd)

    return FakeRow()


def _minimal_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.project_name_to_workspace_id = {}
    ctx.workspace_path_to_id = {}
    ctx.composer_id_to_workspace_id = {}
    ctx.invalid_workspace_ids = set()
    return ctx


def _minimal_orch(
    tmp_ws: str,
    *,
    display_name: dict[str, str] | None = None,
    slug_map: dict[str, str] | None = None,
) -> WorkspaceOrchestration:
    return WorkspaceOrchestration(
        workspace_path=tmp_ws,
        workspace_entries=[],
        ctx=_minimal_ctx(),
        workspace_id_to_display_name=display_name or {},
        workspace_id_to_slug=slug_map or {},
    )


class TestReadLastExportMs(unittest.TestCase):
    def test_since_all_returns_zero(self):
        self.assertEqual(read_last_export_ms("all", state={"lastExportTime": "2026-01-01"}), 0)

    def test_since_last_reads_state_dict(self):
        ms = read_last_export_ms(
            "last",
            state={"lastExportTime": "2026-01-01T12:00:00"},
        )
        self.assertGreater(ms, 0)


class TestCollectExportEntriesNocache(_TempExportPathsMixin, unittest.TestCase):
    def test_nocache_env_passed_to_prepare_workspace_orchestration(self):
        with patch.dict(os.environ, {"CURSOR_CHAT_BROWSER_NOCACHE": "1"}):
            with patch(
                "services.export_engine.prepare_workspace_orchestration",
            ) as mock_prepare:
                mock_prepare.return_value = MagicMock(spec=WorkspaceOrchestration)
                with patch(
                    "services.export_engine.load_global_db_export_data",
                    return_value=None,
                ):
                    collect_export_entries(
                        workspace_path=self.tmp_ws,
                        exclusion_rules=[],
                        since="all",
                        last_export_ms=0,
                        out_dir=self.tmp_out,
                        include_composer=False,
                        include_cli=False,
                    )
        mock_prepare.assert_called_once()
        self.assertTrue(mock_prepare.call_args.kwargs["nocache"])


class TestCollectExportEntriesCorruptComposer(
    _TempExportPathsMixin,
    unittest.TestCase,
):
    def test_non_dict_composer_row_is_skipped(self):
        orch = _minimal_orch(self.tmp_ws)
        db_data = GlobalDbExportData(
            project_layouts_map={},
            bubble_map={},
            code_block_diff_map={},
            ide_composer_rows=[_fake_composer_row("bad-row", [])],  # type: ignore[arg-type]
            invalid_workspace_aliases={},
        )
        with patch(
            "services.export_engine.prepare_workspace_orchestration",
            return_value=orch,
        ):
            with patch(
                "services.export_engine.load_global_db_export_data",
                return_value=db_data,
            ):
                exported = collect_export_entries(
                    workspace_path=self.tmp_ws,
                    exclusion_rules=[],
                    since="all",
                    last_export_ms=0,
                    out_dir=self.tmp_out,
                    include_cli=False,
                )
        self.assertEqual(exported, [])


class TestCollectIdeExportEntries(_TempExportPathsMixin, unittest.TestCase):
    def _collect(
        self,
        cd: dict[str, object],
        *,
        composer_id: str = "cmp-1",
        exclusion_rules: list | None = None,
        orch: WorkspaceOrchestration | None = None,
        project_id: str = "ws-unknown-abcdefghijklmnop",
    ) -> list:
        bubble_id = "bubble-1"
        bubble_map = {
            bubble_id: Bubble.from_dict(
                {"type": "user", "text": "Hello from the test bubble."},
                bubble_id=bubble_id,
            ),
        }
        db_data = GlobalDbExportData(
            project_layouts_map={},
            bubble_map=bubble_map,
            code_block_diff_map={},
            ide_composer_rows=[_fake_composer_row(composer_id, cd)],
            invalid_workspace_aliases={},
        )
        orch = orch or _minimal_orch(self.tmp_ws)
        with patch(
            "services.export_engine.determine_project_for_conversation",
            return_value=project_id,
        ):
            with patch(
                "services.export_engine.cursor_ide_chat_to_markdown",
                return_value="# exported markdown",
            ):
                return _collect_ide_export_entries(
                    orch=orch,
                    db_data=db_data,
                    exclusion_rules=exclusion_rules or [],
                    since="all",
                    last_export_ms=0,
                    today="2026-06-22",
                    out_dir=self.tmp_out,
                )

    def test_last_updated_at_only_no_created_at_fallback(self):
        created_ms = 1739200000000
        fixed_now = datetime(2026, 6, 22, 12, 0, 0)
        with patch("services.export_engine.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            exported = self._collect({
                "name": "Created-only chat",
                "modelConfig": {},
                "fullConversationHeadersOnly": [{"bubbleId": "bubble-1", "type": 1}],
                "createdAt": created_ms,
            })
        self.assertEqual(len(exported), 1)
        entry = exported[0]
        self.assertEqual(entry["updatedAt"], 0)
        ts_str = fixed_now.strftime("%Y-%m-%dT%H-%M-%S")
        self.assertIn(ts_str, entry["rel_path"])
        self.assertNotIn(
            datetime.fromtimestamp(created_ms / 1000).strftime("%Y-%m-%dT%H-%M-%S"),
            entry["rel_path"],
        )

    def test_display_name_falls_back_to_slug_of_workspace_id_prefix(self):
        ws_id = "abcdefghijklmnop"
        exported = self._collect(
            {
                "name": "Workspace fallback chat",
                "modelConfig": {},
                "fullConversationHeadersOnly": [{"bubbleId": "bubble-1", "type": 1}],
                "lastUpdatedAt": 1739300000000,
            },
            project_id=ws_id,
            orch=_minimal_orch(self.tmp_ws),
        )
        self.assertEqual(len(exported), 1)
        expected_display = slug(ws_id[:12])
        self.assertEqual(exported[0]["workspace"], expected_display)
        self.assertIn(expected_display, exported[0]["rel_path"])

    def test_exclusion_rules_filter_ide_entry(self):
        rules_path = os.path.join(self._tmp.name, "rules.txt")
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write("roadmap\n")
        rules = load_rules(rules_path)

        exported = self._collect(
            {
                "name": "Roadmap planning",
                "modelConfig": {},
                "fullConversationHeadersOnly": [{"bubbleId": "bubble-1", "type": 1}],
                "lastUpdatedAt": 1739300000000,
            },
            exclusion_rules=rules,
        )
        self.assertEqual(exported, [])


if __name__ == "__main__":
    unittest.main()
