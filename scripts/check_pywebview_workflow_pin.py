"""Fail if workflow YAML hardcodes a pywebview version instead of read_desktop_pywebview_spec."""

from __future__ import annotations

import sys
from pathlib import Path

READ_SCRIPT = "read_desktop_pywebview_spec.py"
PYWEBVIEW_INSTALL_WORKFLOWS = ("release.yml", "tests.yml")


def main() -> None:
    for path in sorted(Path(".github/workflows").glob("*.yml")):
        text = path.read_text()
        if "pywebview>=" in text or "pywebview<" in text:
            print(
                f"{path} hardcodes a pywebview version; use scripts/{READ_SCRIPT}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if READ_SCRIPT not in text and "pywebview" in text.lower():
            if path.name in PYWEBVIEW_INSTALL_WORKFLOWS:
                print(
                    f"{path} installs pywebview but does not read the pin from pyproject",
                    file=sys.stderr,
                )
                raise SystemExit(1)


if __name__ == "__main__":
    main()
