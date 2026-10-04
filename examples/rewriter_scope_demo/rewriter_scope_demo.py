"""Demo for :meth:`.Rewriter.scope` - no database required.

Run with::

    python examples/rewriter_scope_demo/rewriter_scope_demo.py

The script builds a small in-memory operation tree (the same kind of object
that autogenerate hands to ``process_revision_directives``) and runs it
through a :class:`.Rewriter` three times:

1. with the permanently registered rule (column name marked ``A``),
2. inside a nested temporary scope whose rule marks ``B``,
3. after the scope exits, back on the permanent rule (``A`` again).

It also demonstrates that scopes restore their rules when the traversal
raises, and that a second traversal of the same tree is never skipped due
to markers left by the first.
"""

from __future__ import annotations

from unittest import mock

import sqlalchemy as sa

from alembic import autogenerate, util
from alembic.operations import ops


def build_directives() -> list[ops.MigrationScript]:
    return [
        ops.MigrationScript(
            util.rev_id(),
            ops.UpgradeOps(
                ops=[
                    ops.ModifyTableOps(
                        "t1",
                        ops=[
                            ops.AddColumnOp(
                                "t1", sa.Column("col", sa.Integer())
                            )
                        ],
                    )
                ]
            ),
            ops.DowngradeOps(ops=[]),
        )
    ]


def column_name(directives: list[ops.MigrationScript]) -> str:
    return directives[0].upgrade_ops.ops[0].ops[0].column.name


def main() -> None:
    writer = autogenerate.Rewriter()

    @writer.rewrites(ops.AddColumnOp)
    def mark_a(context, revision, op):
        """permanent rule: mark the column name with _A"""
        op.column.name = f"{op.column.name}_A"
        return op

    ctx, rev = mock.Mock(), mock.Mock()

    # 1. permanent rule
    directives = build_directives()
    writer(ctx, rev, directives)
    print(f"outside scope:        {column_name(directives)}")
    assert column_name(directives) == "col_A"

    with writer.scope() as scoped:

        @scoped.rewrites(ops.AddColumnOp)
        def mark_b(context, revision, op):
            """temporary rule: mark the column name with _B"""
            op.column.name = f"{op.column.name}_B"
            return op

        # 2. temporary rule inside the scope
        directives = build_directives()
        writer(ctx, rev, directives)
        print(f"inside scope:         {column_name(directives)}")
        assert column_name(directives) == "col_B"

        # an independent Rewriter never sees this temporary registration
        other = autogenerate.Rewriter()

        @other.rewrites(ops.AddColumnOp)
        def mark_other(context, revision, op):
            op.column.name = f"{op.column.name}_OTHER"
            return op

        directives = build_directives()
        other(ctx, rev, directives)
        print(f"other rewriter:       {column_name(directives)}")
        assert column_name(directives) == "col_OTHER"

        # a raising rule must still restore the original rules on exit
        with writer.scope() as boom_scope:

            @boom_scope.rewrites(ops.AddColumnOp)
            def boom(context, revision, op):
                raise RuntimeError("simulated failure in temporary rule")

            try:
                writer(ctx, rev, build_directives())
            except RuntimeError as err:
                print(f"inner rule raised:    {err}")

        # after the inner (raising) scope, the enclosing rule is restored
        directives = build_directives()
        writer(ctx, rev, directives)
        print(f"back in outer scope:  {column_name(directives)}")
        assert column_name(directives) == "col_B"

    # 3. scope exited - permanent rule again
    directives = build_directives()
    writer(ctx, rev, directives)
    print(f"after scope exit:     {column_name(directives)}")
    assert column_name(directives) == "col_A"

    # the same tree can be traversed again - markers never leak between
    # calls, so the rule runs one more time
    writer(ctx, rev, directives)
    print(f"second traversal:     {column_name(directives)}")
    assert column_name(directives) == "col_A_A"

    print("\nall demo assertions passed")


if __name__ == "__main__":
    main()
