"""Stdout the pywebview pin from pyproject.toml [desktop]."""

from __future__ import annotations

import sys
import tomllib


def main() -> None:
    deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["optional-dependencies"]["desktop"]
    if len(deps) != 1 or not deps[0].startswith("pywebview"):
        print(
            "need exactly one pywebview dep in [project.optional-dependencies].desktop",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(deps[0])


if __name__ == "__main__":
    main()
