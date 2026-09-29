"""Command line entry point for SQLite migration trial runs.

Example::

    python -m alembic.trial /path/to/app.db --config alembic.ini ``
        --revision head

Exit status is ``0`` if the trial reached the target revision on the
disposable copy, ``1`` if it failed, and ``2`` if it was cancelled
through the injected stop point.  The original database is never
modified in any case.

"""

from __future__ import annotations

import argparse
import sys
from typing import Callable
from typing import Optional
from typing import Sequence

from . import run_sqlite_trial
from . import STATUS_CANCELLED
from . import STATUS_FAILED
from . import STATUS_SUCCESS
from . import TrialError

_EXIT_CODES = {STATUS_SUCCESS: 0, STATUS_FAILED: 1, STATUS_CANCELLED: 2}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alembic.trial",
        description=(
            "Trial-run Alembic migrations against a disposable copy of "
            "a SQLite database; the original database is opened "
            "read-only and never modified."
        ),
    )
    parser.add_argument(
        "database", help="path to the existing SQLite database file"
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="path to the alembic.ini for the migration environment",
    )
    parser.add_argument(
        "-r",
        "--revision",
        default="head",
        help="target revision (default: head)",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        metavar="N",
        help=(
            "inject a stop point that cancels the trial before the "
            "(N+1)-th migration step; useful to rehearse cancellation"
        ),
    )
    args = parser.parse_args(argv)

    stop_check: Optional[Callable[[str], bool]] = None
    if args.stop_after is not None:
        stop_after = int(args.stop_after)
        applied = {"count": 0}

        def _stop_check(next_revision: str) -> bool:
            applied["count"] += 1
            return applied["count"] > stop_after

        stop_check = _stop_check

    try:
        result = run_sqlite_trial(
            args.database,
            args.config,
            args.revision,
            stop_check=stop_check,
        )
    except TrialError as exc:
        print(f"trial could not start: {exc}", file=sys.stderr)
        return 1
    print(result.format())
    return _EXIT_CODES[result.status]


if __name__ == "__main__":
    sys.exit(main())
