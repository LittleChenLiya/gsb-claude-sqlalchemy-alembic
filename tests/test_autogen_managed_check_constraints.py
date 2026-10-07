from unittest import mock

import sqlalchemy as sa
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic.autogenerate import api
from alembic.autogenerate import produce_migrations
from alembic.autogenerate.compare import constraints as ck_constraints
from alembic.autogenerate.compare.util import _normalize_check_expression
from alembic.autogenerate.compare.util import (
    _strip_unbalanced_trailing_parens,
)
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.testing import eq_
from alembic.testing import is_false
from alembic.testing import is_true
from alembic.testing import TestBase
from alembic.testing.suite._autogen_fixtures import AutogenFixtureTest
from alembic.util import sqla_compat


def _managed(*names, table="items"):
    return {"managed_check_constraints": {table: list(names)}}


def _items(m, *extra, table="items"):
    Table(
        table,
        m,
        Column("id", Integer, primary_key=True),
        Column("price", Integer),
        *extra,
    )
    return m


def _ck_diffs(diffs):
    return [
        d
        for d in diffs
        if d[0] in ("add_constraint", "remove_constraint")
        and isinstance(d[1], CheckConstraint)
    ]


class NormalizeExpressionTest(TestBase):
    def test_strip_outer_parens(self):
        eq_(_normalize_check_expression("(price >= 0)"), "price>=0")
        eq_(_normalize_check_expression("((price >= 0))"), "price>=0")

    def test_whitespace_removed(self):
        eq_(_normalize_check_expression(" price  >=\t0 \n"), "price>=0")
        eq_(_normalize_check_expression("price>=0"), "price>=0")

    def test_inner_parens_kept(self):
        eq_(
            _normalize_check_expression("(a > 0) AND (b > 0)"),
            "(a>0)AND(b>0)",
        )

    def test_string_literal_verbatim(self):
        eq_(
            _normalize_check_expression("status IN ('a b', 'c')"),
            "status IN('a b','c')",
        )
        # parens / spaces inside the literal do not affect balancing
        eq_(_normalize_check_expression("x = ') ('"), "x=') ('")

    def test_quoted_identifier_verbatim(self):
        eq_(_normalize_check_expression('"My Col" > 0'), '"My Col">0')
        # case is significant inside quotes
        eq_(
            _normalize_check_expression('"Price" > 0')
            == _normalize_check_expression('"price" > 0'),
            False,
        )

    def test_operator_order_significant(self):
        eq_(
            _normalize_check_expression("0 <= price")
            == _normalize_check_expression("price >= 0"),
            False,
        )

    def test_unbalanced_parens_returns_none(self):
        eq_(_normalize_check_expression("(price >= 0"), None)
        eq_(_normalize_check_expression("price >= 0))"), None)

    def test_unterminated_literal_returns_none(self):
        eq_(_normalize_check_expression("x = 'oops"), None)

    def test_word_separation_kept(self):
        eq_(
            _normalize_check_expression("a b")
            == _normalize_check_expression("ab"),
            False,
        )

    def test_strip_reflection_trailing_paren_artifact(self):
        eq_(_strip_unbalanced_trailing_parens("price >= 0)"), "price >= 0")
        eq_(_strip_unbalanced_trailing_parens("x IN (1, 2))"), "x IN (1, 2)")
        eq_(_strip_unbalanced_trailing_parens("f(x) > 0)"), "f(x) > 0")
        # balanced text is returned exactly as given
        eq_(
            _strip_unbalanced_trailing_parens("(a > 0) AND (b > 0)"),
            "(a > 0) AND (b > 0)",
        )
        # a ')' inside a string literal is not touched
        eq_(_strip_unbalanced_trailing_parens("x = ')')"), "x = ')'")
        eq_(_strip_unbalanced_trailing_parens("x = ')'"), "x = ')'")


class ManagedCheckConstraintCompareTest(AutogenFixtureTest, TestBase):
    __backend__ = True

    def test_no_managed_option_keeps_default_handling(self):
        """without the option, CHECK constraints are not compared at all"""
        m1 = _items(MetaData())
        m2 = _items(MetaData(), CheckConstraint("price > 0", name="ck_price"))
        diffs = self._fixture(m1, m2)
        eq_(_ck_diffs(diffs), [])

    def test_add_managed_constraint(self):
        m1 = _items(MetaData())
        m2 = _items(MetaData(), CheckConstraint("price > 0", name="ck_price"))
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        ck_diffs = _ck_diffs(diffs)
        eq_(len(ck_diffs), 1)
        eq_(ck_diffs[0][0], "add_constraint")
        eq_(ck_diffs[0][1].name, "ck_price")

    def test_remove_managed_constraint(self):
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        m2 = _items(MetaData())
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        ck_diffs = _ck_diffs(diffs)
        eq_(len(ck_diffs), 1)
        eq_(ck_diffs[0][0], "remove_constraint")
        eq_(ck_diffs[0][1].name, "ck_price")

    def test_change_expression_is_drop_and_create(self):
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        m2 = _items(MetaData(), CheckConstraint("price > 0", name="ck_price"))
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        ck_diffs = _ck_diffs(diffs)
        eq_([d[0] for d in ck_diffs], ["remove_constraint", "add_constraint"])
        # the drop carries the original expression text, so that a
        # downgrade restores it verbatim
        eq_(str(ck_diffs[0][1].sqltext), "price >= 0")
        eq_(str(ck_diffs[1][1].sqltext), "price > 0")

    def test_equal_expression_no_diff(self):
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        m2 = _items(
            MetaData(), CheckConstraint("(( price  >=  0 ))", name="ck_price")
        )
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(_ck_diffs(diffs), [])

    def test_unmanaged_constraint_ignored(self):
        m1 = _items(
            MetaData(),
            CheckConstraint("price >= 0", name="ck_price"),
            CheckConstraint("price < 1000", name="ck_other"),
        )
        # ck_other is not managed, so its absence from the metadata
        # generates nothing
        m2 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(_ck_diffs(diffs), [])

    def test_anonymous_constraints_ignored(self):
        m1 = _items(MetaData(), CheckConstraint("price >= 0"))
        m2 = _items(MetaData(), CheckConstraint("price > 0"))
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(_ck_diffs(diffs), [])

    def test_same_name_different_table(self):
        m1 = MetaData()
        m2 = MetaData()
        for m, expr in ((m1, "price >= 0"), (m2, "price > 0")):
            _items(m, CheckConstraint(expr, name="ck_same"), table="t1")
            _items(m, CheckConstraint(expr, name="ck_same"), table="t2")

        uo = self._fixture(
            m1,
            m2,
            opts={"managed_check_constraints": {"t1": ["ck_same"]}},
            return_ops=True,
        )
        table_ops = [op for op in uo.ops if isinstance(op, ops.ModifyTableOps)]
        eq_([op.table_name for op in table_ops], ["t1"])
        eq_(
            [type(op).__name__ for op in table_ops[0].ops],
            ["DropConstraintOp", "CreateCheckConstraintOp"],
        )

    def test_quoted_identifier_expression(self):
        def make(m, expr):
            Table(
                "items",
                m,
                Column("id", Integer, primary_key=True),
                Column("Price", Integer),
                CheckConstraint(expr, name="ck_price"),
            )
            return m

        m1 = make(MetaData(), '"Price" >= 0')
        # only whitespace differs: no change
        m2 = make(MetaData(), '( "Price"   >=   0 )')
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(_ck_diffs(diffs), [])

        # case change inside the quoted identifier is significant
        m3 = make(MetaData(), '"price" >= 0')
        diffs = self._fixture(m1, m3, opts=_managed("ck_price"))
        eq_(
            [d[0] for d in _ck_diffs(diffs)],
            ["remove_constraint", "add_constraint"],
        )

    def test_string_literal_expression(self):
        def make(m, expr):
            Table(
                "items",
                m,
                Column("id", Integer, primary_key=True),
                Column("status", Integer),
                CheckConstraint(expr, name="ck_status"),
            )
            return m

        m1 = make(MetaData(), "status IN (1, 2)")
        # whitespace-only difference: no change
        m2 = make(MetaData(), "status IN (1,2)")
        diffs = self._fixture(m1, m2, opts=_managed("ck_status"))
        eq_(_ck_diffs(diffs), [])

        m3 = make(MetaData(), "status IN (1, 3)")
        diffs = self._fixture(m1, m3, opts=_managed("ck_status"))
        eq_(
            [d[0] for d in _ck_diffs(diffs)],
            ["remove_constraint", "add_constraint"],
        )

    def test_operator_order_not_rewritten(self):
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        m2 = _items(MetaData(), CheckConstraint("0 <= price", name="ck_price"))
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(
            [d[0] for d in _ck_diffs(diffs)],
            ["remove_constraint", "add_constraint"],
        )

    def test_unnormalizable_expression_conservative_difference(self):
        """an expression that cannot be normalized is conservatively
        reported as different, generating a complete drop+create pair"""
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        # unterminated string literal: cannot be tokenized
        m2 = _items(
            MetaData(), CheckConstraint("price >= 0 -- 'x", name="ck_price")
        )
        diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(
            [d[0] for d in _ck_diffs(diffs)],
            ["remove_constraint", "add_constraint"],
        )

    def test_comparison_error_generates_no_partial_ops(self):
        """if comparison fails unexpectedly, no operations at all are
        generated for the managed constraints"""
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        m2 = _items(MetaData(), CheckConstraint("price > 0", name="ck_price"))
        with mock.patch.object(
            ck_constraints,
            "_ck_expressions_differ",
            side_effect=RuntimeError("cannot compare"),
        ):
            diffs = self._fixture(m1, m2, opts=_managed("ck_price"))
        eq_(_ck_diffs(diffs), [])

    def test_naming_convention_name_resolved(self):
        m1 = _items(
            MetaData(),
            CheckConstraint("price >= 0", name="ck_items_price_positive"),
        )
        m2 = MetaData(
            naming_convention={"ck": "ck_%(table_name)s_%(constraint_name)s"}
        )
        _items(m2, CheckConstraint("price > 0", name="price_positive"))
        diffs = self._fixture(
            m1,
            m2,
            opts=_managed("ck_items_price_positive"),
        )
        eq_(
            [d[0] for d in _ck_diffs(diffs)],
            ["remove_constraint", "add_constraint"],
        )

    def test_sqlite_ops_marked_for_batch(self):
        m1 = _items(MetaData(), CheckConstraint("price >= 0", name="ck_price"))
        m2 = _items(MetaData(), CheckConstraint("price > 0", name="ck_price"))
        uo = self._fixture(m1, m2, opts=_managed("ck_price"), return_ops=True)
        table_ops = [op for op in uo.ops if isinstance(op, ops.ModifyTableOps)]
        eq_(len(table_ops), 1)
        if self.bind.dialect.name == "sqlite":
            is_true(table_ops[0].force_batch)
            # the reversed (downgrade) ops keep the batch marker
            is_true(table_ops[0].reverse().force_batch)
        else:
            is_false(table_ops[0].force_batch)


class ManagedCheckConstraintRenderTest(TestBase):
    """Offline rendering of the generated operations."""

    def _make_ops(self, force_batch):
        m_old = MetaData()
        Table(
            "items",
            m_old,
            Column("price", Integer),
            CheckConstraint("price >= 0", name="ck_price"),
        )
        m_new = MetaData()
        Table(
            "items",
            m_new,
            Column("price", Integer),
            CheckConstraint("price > 0", name="ck_price"),
        )
        old_ck = sqla_compat.all_table_check_constraints(
            m_old.tables["items"]
        ).pop()
        new_ck = sqla_compat.all_table_check_constraints(
            m_new.tables["items"]
        ).pop()
        modify = ops.ModifyTableOps(
            "items",
            [
                ops.DropConstraintOp.from_constraint(old_ck),
                ops.AddConstraintOp.from_constraint(new_ck),
            ],
            force_batch=force_batch,
        )
        upgrade = ops.UpgradeOps([modify])
        return upgrade, upgrade.reverse()

    def test_render_batch_for_sqlite(self):
        upgrade, downgrade = self._make_ops(force_batch=True)
        up_code = api.render_python_code(upgrade)
        down_code = api.render_python_code(downgrade)
        assert "op.batch_alter_table('items'" in up_code
        assert "batch_op.drop_constraint" in up_code
        assert "batch_op.create_check_constraint" in up_code
        assert "'price > 0'" in up_code
        # downgrade restores the original expression text
        assert "op.batch_alter_table('items'" in down_code
        assert "'price >= 0'" in down_code

    def test_render_plain_ddl_for_other_dialects(self):
        upgrade, downgrade = self._make_ops(force_batch=False)
        up_code = api.render_python_code(upgrade)
        down_code = api.render_python_code(downgrade)
        assert "batch_alter_table" not in up_code
        assert "op.drop_constraint('ck_price'" in up_code
        assert (
            "op.create_check_constraint('ck_price', 'items', 'price > 0')"
            in up_code
        )
        assert "batch_alter_table" not in down_code
        assert "'price >= 0'" in down_code


class ManagedCheckConstraintRoundtripTest(TestBase):
    """Acceptance: generate, upgrade and downgrade a managed CHECK
    constraint against an in-memory SQLite database, verifying the
    enforced expression at each step."""

    def _metadata(self, expr):
        m = MetaData()
        Table(
            "items",
            m,
            Column("id", Integer, primary_key=True),
            Column("price", Integer),
            CheckConstraint(expr, name="ck_price"),
        )
        return m

    def _run_code(self, code, conn):
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        ns = {"op": op, "sa": sa}
        exec("def migrate():\n" + code, ns)
        ns["migrate"]()

    def _zero_insertable(self, conn):
        try:
            conn.execute(text("INSERT INTO items (price) VALUES (0)"))
        except IntegrityError:
            conn.rollback()
            return False
        else:
            conn.rollback()
            return True

    def test_upgrade_downgrade_roundtrip(self):
        engine = create_engine("sqlite://")
        self._metadata("price >= 0").create_all(engine)

        target = self._metadata("price > 0")
        opts = {
            "target_metadata": target,
            "managed_check_constraints": {"items": ["ck_price"]},
        }

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=opts)
            script = produce_migrations(ctx, target)
            upgrade_code = api.render_python_code(
                script.upgrade_ops, migration_context=ctx
            )
            downgrade_code = api.render_python_code(
                script.downgrade_ops, migration_context=ctx
            )

        # SQLite renders the batch (table recreate) form
        assert "batch_alter_table" in upgrade_code
        assert "'price > 0'" in upgrade_code
        # the downgrade uses the original expression text
        assert "batch_alter_table" in downgrade_code
        assert "'price >= 0'" in downgrade_code

        with engine.connect() as conn:
            is_true(self._zero_insertable(conn))

            self._run_code(upgrade_code, conn)
            is_false(self._zero_insertable(conn))

            self._run_code(downgrade_code, conn)
            is_true(self._zero_insertable(conn))

            # the restored behavior can be changed again
            self._run_code(upgrade_code, conn)
            is_false(self._zero_insertable(conn))

        # after the upgrade, autogenerate sees no further diff
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=opts)
            script = produce_migrations(ctx, target)
            eq_(script.upgrade_ops.as_diffs(), [])
