"""
`t3nets` command-line entry point.

Dispatches to the `practice` subcommands: init, validate, package. Kept
deliberately thin — each subcommand lives in its own module so it can be
tested without going through argparse.
"""

from __future__ import annotations

import argparse
import sys

from t3nets_sdk.cli import init as init_cmd
from t3nets_sdk.cli import package as package_cmd
from t3nets_sdk.cli import run_local as run_local_cmd
from t3nets_sdk.cli import validate as validate_cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t3nets",
        description="t3nets practice developer CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    practice = sub.add_parser("practice", help="Practice-related commands")
    practice_sub = practice.add_subparsers(dest="subcommand", required=True)

    init_p = practice_sub.add_parser("init", help="Scaffold a new practice")
    init_p.add_argument("name", help="Practice name (letters, digits, dashes, underscores)")
    init_p.add_argument(
        "--dir",
        default=".",
        help="Parent directory to create the practice in (default: current directory)",
    )
    init_p.set_defaults(func=init_cmd.run)

    val_p = practice_sub.add_parser("validate", help="Validate a practice directory")
    val_p.add_argument(
        "--dir",
        default=".",
        help="Practice directory to validate (default: current directory)",
    )
    val_p.set_defaults(func=validate_cmd.run)

    pkg_p = practice_sub.add_parser("package", help="Build a distributable practice.zip")
    pkg_p.add_argument(
        "--dir",
        default=".",
        help="Practice directory to package (default: current directory)",
    )
    pkg_p.add_argument(
        "--output",
        default=None,
        help="Output zip path (default: <dir>/dist/practice.zip)",
    )
    pkg_p.set_defaults(func=package_cmd.run)

    run_p = practice_sub.add_parser(
        "run-local",
        help="Boot the dev server with this practice loaded",
    )
    run_p.add_argument(
        "--dir",
        default=".",
        help="Practice directory (default: current directory)",
    )
    run_p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: PORT env var or 8080)",
    )
    run_p.set_defaults(func=run_local_cmd.run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
