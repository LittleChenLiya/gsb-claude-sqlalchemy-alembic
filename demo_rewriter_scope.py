"""Demo: temporary rewrite-rule scopes on :class:`.Rewriter`.

This script demonstrates the :meth:`.Rewriter.scope` context manager,
which provides a temporary registration scope for rewrite rules.  It does
not connect to a database; it builds an in-memory operation tree and
prints the column name after each traversal.

Run with::

    python demo_rewriter_scope.py

"""

from __future__ import annotations

from unittest import mock

import sqlalchemy as sa

from alembic import util
from alembic.autogenerate import rewriter
from alembic.operations import ops


def make_directives() -> list:
    """Build a fresh migration script containing one AddColumnOp."""
    return [
        ops.MigrationScript(
            util.rev_id(),
            ops.UpgradeOps(
                ops=[
                    ops.ModifyTableOps(
                        "t1",
                        ops=[
                            ops.AddColumnOp(
                                "t1", sa.Column("x", sa.Integer())
                            )
                        ],
                    )
                ]
            ),
            ops.DowngradeOps(ops=[]),
        )
    ]


def column_name(directives: list) -> str:
    return directives[0].upgrade_ops.ops[0].ops[0].column.name


def main() -> None:
    ctx, rev = mock.Mock(), mock.Mock()

    writer = rewriter.Rewriter()

    # Outer rule: mark the column name "A".
    @writer.rewrites(ops.AddColumnOp)
    def mark_a(context, revision, op):
        op.column.name = "A"
        return op

    # 1) Before the scope, the outer rule is applied.
    directives = make_directives()
    writer(ctx, rev, directives)
    print(f"before scope:      {column_name(directives)}  (expected A)")

    # 2) Within the scope, the temporary rule "B" overrides "A".
    with writer.scope():

        @writer.rewrites(ops.AddColumnOp)
        def mark_b(context, revision, op):
            op.column.name = "B"
            return op

        directives = make_directives()
        writer(ctx, rev, directives)
        print(f"in scope:          {column_name(directives)}  (expected B)")

        # A second traversal of the same tree still applies "B"; the
        # processed marker from the first traversal does not cause the
        # second traversal to be skipped.
        writer(ctx, rev, directives)
        print(f"in scope (2nd):    {column_name(directives)}  (expected B)")

    # 3) After the scope exits, the outer rule "A" is applied again.
    directives = make_directives()
    writer(ctx, rev, directives)
    print(f"after scope:       {column_name(directives)}  (expected A)")

    # 4) The scope is restored even if an exception is raised.
    try:
        with writer.scope():

            @writer.rewrites(ops.AddColumnOp)
            def mark_c(context, revision, op):
                op.column.name = "C"
                return op

            raise RuntimeError("boom")
    except RuntimeError:
        pass

    directives = make_directives()
    writer(ctx, rev, directives)
    print(f"after exception:   {column_name(directives)}  (expected A)")

    # 5) The scope is local to this Rewriter; another Rewriter does not
    #    see the temporary rule.
    other = rewriter.Rewriter()
    with writer.scope():

        @writer.rewrites(ops.AddColumnOp)
        def mark_d(context, revision, op):
            op.column.name = "D"
            return op

        directives = make_directives()
        other(ctx, rev, directives)
        print(
            f"other in scope:    {column_name(directives)}  "
            f"(expected x, unchanged)"
        )


if __name__ == "__main__":
    main()
