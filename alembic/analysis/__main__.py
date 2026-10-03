"""Self-contained, in-memory demo for the transaction-boundary analyzer.

Run with::

    python -m alembic.analysis

The demo builds operation trees from plain objects -- no database, no
configuration, no SQL execution -- and prints the fragment report that an
offline script-generation step could be previewed against.
"""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String

from .txboundary import analyze_transaction_boundaries
from .txboundary import TransactionAnalysis
from .txboundary import TransactionBoundaryOp
from ..operations.ops import AddColumnOp
from ..operations.ops import CreateIndexOp
from ..operations.ops import ExecuteSQLOp
from ..operations.ops import ModifyTableOps
from ..operations.ops import UpgradeOps


def _build_acceptance_tree() -> UpgradeOps:
    """add column, boundary marker, re-index -> two ordered fragments."""

    return UpgradeOps(
        ops=[
            AddColumnOp("account", Column("email", String(100))),
            TransactionBoundaryOp(note="commit before index rebuild"),
            CreateIndexOp("ix_account_email", "account", ["email"]),
        ]
    )


def _build_nested_tree() -> UpgradeOps:
    """Nested batch container, repeated markers and an unknown op."""

    return UpgradeOps(
        ops=[
            TransactionBoundaryOp(),  # leading marker: no empty fragment
            ModifyTableOps(
                "account",
                ops=[
                    AddColumnOp("account", Column("age", Integer())),
                    TransactionBoundaryOp(),  # marker nested inside batch
                    # repeated marker right after the first: still one cut,
                    # never an empty fragment
                    TransactionBoundaryOp(),
                    CreateIndexOp(
                        "ix_account_age",
                        "account",
                        ["age"],
                        postgresql_concurrently=True,
                    ),
                ],
            ),
            ExecuteSQLOp("VACUUM ANALYZE account"),  # -> unknown
            ExecuteSQLOp("-- alembic:transaction-boundary"),  # SQL marker
            ModifyTableOps("account", ops=[]),  # empty batch: nothing emitted
            AddColumnOp("account", Column("score", Integer())),
        ]
    )


def main() -> None:
    print("### Scenario 1: add column + boundary marker + re-index ###")
    tree = _build_acceptance_tree()
    analysis: TransactionAnalysis = analyze_transaction_boundaries(tree)
    print(analysis.render_report())
    print()
    print(
        "### Scenario 2: nesting, repeated markers, unknown, empty batch ###"
    )
    analysis2 = analyze_transaction_boundaries(_build_nested_tree())
    print(analysis2.render_report())
    print()
    print("fully-safe scenario 1:", analysis.is_fully_safe())
    print("unknown ops scenario 2:", analysis2.unknown_count)


if __name__ == "__main__":
    main()
