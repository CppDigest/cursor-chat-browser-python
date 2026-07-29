"""Concurrency regression tests for summary-cache fingerprint read-compare-write."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services import summary_cache
from services.summary_cache import (
    get_or_build_cached_projects,
    get_cached_projects,
)


def _make_workspace_fixture(root: str) -> tuple[str, list[dict[str, object]]]:
    entry_dir = os.path.join(root, "entry1")
    os.makedirs(entry_dir)
    db_path = os.path.join(entry_dir, "state.vscdb")
    with open(db_path, "wb") as f:
        f.write(b"x")
    workspace_path = root
    entries: list[dict[str, object]] = [
        {
            "name": "entry1",
            "workspaceJsonPath": os.path.join(entry_dir, "workspace.json"),
        },
    ]
    return workspace_path, entries


class TestSummaryCacheConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(summary_cache, "CACHE_DIR", Path(self.tmp.name))
        self.cache_patch.start()
        summary_cache.PROJECTS_CACHE_FILE = Path(self.tmp.name) / "projects.json"
        summary_cache.COMPOSER_MAP_CACHE_FILE = (
            Path(self.tmp.name) / "composer-id-to-ws.json"
        )
        summary_cache.INVALID_WORKSPACE_ALIASES_CACHE_FILE = (
            Path(self.tmp.name) / "invalid-workspace-aliases.json"
        )

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_blocked_build_recheck_returns_peer_cache(self):
        """Lost-update: peer write while build is blocked must win on recheck."""
        ws_root = os.path.join(self.tmp.name, "ws")
        os.makedirs(ws_root)
        workspace_path, workspace_entries = _make_workspace_fixture(ws_root)

        peer_projects = [
            {"id": "peer", "name": "Peer", "conversationCount": 1, "lastModified": "x"},
        ]
        stale_projects = [
            {"id": "stale", "name": "Stale", "conversationCount": 1, "lastModified": "y"},
        ]
        build_started = threading.Event()
        allow_build_finish = threading.Event()
        build_count = 0
        build_count_lock = threading.Lock()

        def blocked_build() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
            nonlocal build_count
            with build_count_lock:
                build_count += 1
                count = build_count
            build_started.set()
            self.assertTrue(
                allow_build_finish.wait(timeout=5.0),
                msg="timed out waiting to finish blocked build",
            )
            if count == 1:
                return stale_projects, []
            return peer_projects, []

        errors: list[str] = []
        results: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

        def thread_a() -> None:
            try:
                results.append(
                    get_or_build_cached_projects(
                        workspace_path,
                        workspace_entries,  # type: ignore[arg-type]
                        [],
                        build_fn=blocked_build,  # type: ignore[arg-type]
                    ),
                )
            except Exception as exc:
                errors.append(f"thread A: {exc}")

        def thread_b() -> None:
            try:
                self.assertTrue(
                    build_started.wait(timeout=5.0),
                    msg="thread B started before thread A entered build_fn",
                )
                results.append(
                    get_or_build_cached_projects(
                        workspace_path,
                        workspace_entries,  # type: ignore[arg-type]
                        [],
                        build_fn=lambda: (peer_projects, []),  # type: ignore[arg-type]
                    ),
                )
            except Exception as exc:
                errors.append(f"thread B: {exc}")
            finally:
                allow_build_finish.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(thread_a)
            fut_b = pool.submit(thread_b)
            for fut in as_completed([fut_a, fut_b]):
                fut.result()

        self.assertEqual(errors, [], "\n".join(errors))
        self.assertEqual(len(results), 2)
        for projects, _warnings in results:
            self.assertEqual(projects, peer_projects)

        with summary_cache.PROJECTS_CACHE_FILE.open(encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk.get("projects"), peer_projects)

    def test_concurrent_get_or_build_returns_consistent_results(self):
        ws_root = os.path.join(self.tmp.name, "ws-warm")
        os.makedirs(ws_root)
        workspace_path, workspace_entries = _make_workspace_fixture(ws_root)
        warm_projects = [
            {"id": "warm", "name": "Warm", "conversationCount": 2, "lastModified": "z"},
        ]
        fingerprint = summary_cache._workspace_storage_fingerprint(
            workspace_path,
            workspace_entries,  # type: ignore[arg-type]
            [],
        )
        summary_cache.set_cached_projects(fingerprint, warm_projects, [])

        barrier = threading.Barrier(8)
        errors: list[str] = []
        collected: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

        def reader() -> None:
            try:
                barrier.wait(timeout=5.0)
                hit = get_or_build_cached_projects(
                    workspace_path,
                    workspace_entries,  # type: ignore[arg-type]
                    [],
                    build_fn=lambda: (_ for _ in ()).throw(  # type: ignore[arg-type]
                        AssertionError("build_fn must not run on warm cache"),
                    ),
                )
                collected.append(hit)
            except Exception as exc:
                errors.append(str(exc))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(reader) for _ in range(8)]
            for fut in as_completed(futures):
                fut.result()

        self.assertEqual(errors, [], "\n".join(errors))
        self.assertEqual(len(collected), 8)
        for projects, warnings in collected:
            self.assertEqual(projects, warm_projects)
            self.assertEqual(warnings, [])

    def test_get_cached_projects_remains_thread_safe(self):
        fp = {"version": 1, "workspace_path": "/ws", "global_db_mtime_ns": 100}
        projects = [{"id": "a", "name": "A", "conversationCount": 1, "lastModified": "x"}]
        summary_cache.set_cached_projects(fp, projects, [])

        barrier = threading.Barrier(12)
        errors: list[str] = []
        hits: list[tuple[list[dict[str, object]], list[dict[str, object]]] | None] = []

        def reader() -> None:
            try:
                barrier.wait(timeout=5.0)
                hits.append(get_cached_projects(fp))
            except Exception as exc:
                errors.append(str(exc))

        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(reader) for _ in range(12)]
            for fut in as_completed(futures):
                fut.result()

        self.assertEqual(errors, [], "\n".join(errors))
        self.assertTrue(all(hit is not None and hit[0] == projects for hit in hits))


if __name__ == "__main__":
    unittest.main()
