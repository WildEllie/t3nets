"""
`t3nets practice run-local` — boot the platform dev server with the
current directory mounted as an extra practice source.

Requires the t3nets platform to be installed in the same Python
environment (i.e. `pip install -e .` from the platform repo root). The
practice directory is validated first so errors surface before the server
starts.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from t3nets_sdk.cli.validate import collect_errors


def run(args: argparse.Namespace) -> int:
    practice_dir = Path(args.dir).resolve()

    errors = collect_errors(practice_dir)
    if errors:
        print(
            f"Cannot run — practice validation failed ({len(errors)} error(s)):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        "-m",
        "adapters.local.dev_server",
        "--extra-practice-dir",
        str(practice_dir),
    ]
    if args.port:
        cmd.extend(["--port", str(args.port)])

    print(f"Starting dev server with practice from {practice_dir}")
    print(f"  {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, env={**os.environ}, check=False)
        return result.returncode
    except KeyboardInterrupt:
        return 0
