"""The ``masterly`` command line — installed by the ``cli`` extra (``pip install masterly[cli]``).

One command today: ``masterly config migrate``, which moves a configuration repository to a
newer schema version as a reviewable edit (ADR 0082, rule 5). The client library itself does
not depend on this package; the extra is what pulls in the YAML round-trip parser the command
needs, and the base ``masterly`` stays a two-dependency client.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from masterly import __version__
from masterly.cli._schema import CURRENT_SCHEMA_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="masterly",
        description="Masterly command line. The Python client is `import masterly`.",
    )
    parser.add_argument("--version", action="version", version=f"masterly {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    config = commands.add_parser("config", help="work on a configuration repository")
    config_commands = config.add_subparsers(dest="config_command", metavar="<subcommand>")

    migrate = config_commands.add_parser(
        "migrate",
        help="move a configuration repository to a newer schema version",
        description=(
            "Rewrite the configuration documents in a repository from the schema version it "
            "declares (masterly.yaml; none means the version that predates the declaration) to "
            f"a newer one — the newest this client knows, {CURRENT_SCHEMA_VERSION}, by default — "
            "and print the unified diff of what changed. Only the files a migration step reaches "
            "are rewritten, keeping their comments, key order and indentation; what a step "
            "cannot do mechanically is reported for review. Nothing is committed: review the "
            "diff and commit it yourself."
        ),
    )
    migrate.add_argument(
        "path",
        nargs="?",
        default=".",
        help="the repository root (default: the current directory)",
    )
    migrate.add_argument(
        "--to",
        type=int,
        default=None,
        metavar="N",
        help=f"the schema version to migrate to (default: {CURRENT_SCHEMA_VERSION})",
    )
    mode = migrate.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the diff and write nothing",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 when a migration is pending (for CI)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "config" or args.config_command != "migrate":
        parser.print_help()
        return 2
    return _run_migrate(args)


def _run_migrate(args: argparse.Namespace) -> int:
    from masterly.cli import _engine as engine

    try:
        plan = engine.plan(Path(args.path), to_version=args.to)
    except engine.MigrateError as exc:
        print(f"masterly config migrate: {exc}", file=sys.stderr)
        return 2
    for change in plan.changes:
        sys.stdout.write(change.unified_diff())
    if not plan.is_noop and not (args.dry_run or args.check):
        engine.write(plan)
    summary = engine.describe(plan)
    if args.check and not plan.is_noop:
        print(f"{summary}\nPending: run `masterly config migrate` and commit the result.")
        return 1
    if args.dry_run and not plan.is_noop:
        summary += "\nDry run: nothing was written."
    print(summary)
    return 0
