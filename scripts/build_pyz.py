#!/usr/bin/env python3
"""Build dist/owl.pyz: the whole owl package as one runnable file (standard library only).

    python3 scripts/build_pyz.py [--output PATH]
    python3 dist/owl.pyz --version       # owl 2.0.0.dev0 (git 4faa3b1)

The archive embeds the git commit it was built from (with "+dirty" if the
working tree had uncommitted changes to owl/), so `owl --version` on a host
says exactly what is deployed. It runs on any Linux with Python >= 3.9; copy
it anywhere, no install step.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipapp

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "owl"

MAIN = '''import sys

if sys.version_info < (3, 9):
    sys.exit("owl needs Python 3.9 or newer")

from owl.cli import main

sys.exit(main())
'''


def git_commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        dirty = git("status", "--porcelain", "--", "owl")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return commit + ("+dirty" if dirty else "")


def build(output: pathlib.Path) -> str:
    commit = git_commit()
    with tempfile.TemporaryDirectory() as tmp:
        staging = pathlib.Path(tmp)
        shutil.copytree(PACKAGE, staging / "owl", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staging / "owl" / "_build.py").write_text(f'COMMIT = "{commit}"\n')
        (staging / "__main__.py").write_text(MAIN)
        output.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(staging, target=output, interpreter="/usr/bin/env python3", compressed=True)
    return commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", "-o", type=pathlib.Path, default=ROOT / "dist" / "owl.pyz")
    args = parser.parse_args()
    commit = build(args.output)
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes, git {commit})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
