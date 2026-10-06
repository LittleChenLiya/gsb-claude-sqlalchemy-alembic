import os
import sys
from types import SimpleNamespace
from unittest import mock

import sqlalchemy as sa
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text

from alembic import autogenerate
from alembic import op
from alembic.autogenerate import api
from alembic.autogenerate.compare import checks
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.testing import config
from alembic.testing import eq_
from alembic.testing import is_false
from alembic.testing import is_true
from alembic.testing import TestBase
from alembic.testing.suite._autogen_fixtures import AutogenFixtureTest


def _table(*args, meta=None, name="t"):
    m1 = meta or MetaData()
    Table(name, m1, *args)
    return m1


class NormalizeCheckExpressionTest(TestBase):
    """Unit tests for the expression normalization used to compare
    managed CHECK constraints."""

    def _norm(self, expr):
        return checks.normalize_check_expression(expr)

    def _equal(self, a, b):
        return checks._check_expressions_equal(a, b)

    def test_whitespace_is_insignificant(self):
        is_true(self._equal("price >= 0", "  price   >=\n 0  "))
        is_true(self._equal("price>=0", "price >= 0"))

    def test_outer_parens_are_insignificant(self):
        is_true(self._equal("price >= 0", "(price >= 0)"))
        is_true(self._equal("price >= 0", "((price >= 0))"))
        is_true(self._equal("price >= 0", "( ( price >= 0 ) )"))

    def test_inner_parens_are_significant(self):
        is_false(
            self._equal(
                "a > 0 AND (b > 0 OR c > 0)", "a > 0 AND b > 0 OR c > 0"
            )
        )

    def test_string_literals_not_rewritten(self):
        is_false(self._equal("status = 'A'", "status = 'a'"))
        is_true(self._equal("status = 'A'", "status='A'"))
        is_true(self._equal("status = 'it''s'", "status = 'it''s'"))
        is_false(self._equal("status = 'it''s'", "status = 'its'"))

    def test_quoted_identifiers_not_rewritten(self):
        is_false(self._equal('"Price" >= 0', '"price" >= 0'))
        is_true(self._equal('"Price" >= 0', '( "Price"  >=  0 )'))
        is_false(self._equal('"Price" >= 0', "Price >= 0"))

    def test_case_is_significant(self):
        is_false(self._equal("PRICE >= 0", "price >= 0"))

    def test_operator_order_is_significant(self):
        is_false(self._equal("price >= 0", "0 <= price"))
        is_false(self._equal("a > b", "b > a"))

    def test_operator_tokens(self):
        is_true(self._equal("a >= 1 AND b <= 2", "a>=1 AND b<=2"))
        is_false(self._equal("a >= 1", "a > 1"))

    def test_unbalanced_parens_cannot_normalize(self):
        eq_(self._norm("(price >= 0"), None)
        eq_(self._norm("price >= 0 AND (x"), None)

    def test_unterminated_literal_cannot_normalize(self):
        eq_(self._norm("status = 'A"), None)
        eq_(self._norm('name = "foo'), None)

    def test_reflection_artifact_trailing_paren(self):
        # SQLite reflection on some SQLAlchemy versions includes the
        # CHECK (...) wrapper's own closing paren in the reflected text
        is_true(self._equal("price >= 0", "price >= 0)"))
        is_true(self._equal("length(name) > 2", "length(name) > 2)"))
        eq_(self._norm("price >= 0)"), "price>=0")

    def test_comments_are_whitespace(self):
        is_true(self._equal("price >= 0", "price /* hi */ >= 0"))
        is_true(self._equal("price >= 0", "price -- hi\n>= 0"))

    def test_failed_normalization_is_conservative_difference(self):
        # an expression that cannot be normalized must compare as
        # different, never as equal
        is_false(self._equal("(price >= 0", "price >= 0"))
        is_false(self._equal("price >= 0", "(price >= 0"))
        # ... unless the raw text is exactly the same
        is_true(self._equal("(price >= 0", "(price >= 0"))

    def test_sanitize_reflected_sqltext(self):
        eq_(
            checks._sanitize_reflected_sqltext("price  >=  0)"),
            "price  >=  0",
        )
        eq_(
            checks._sanitize_reflected_sqltext("length(name) > 2"),
            "length(name) > 2",
        )
        eq_(
            checks._sanitize_reflected_sqltext("x IN (1, 2))"),
            "x IN (1, 2)",
        )


class ManagedSpecTest(TestBase):
    def _names(self, managed, schema, tname):
        autogen_context = SimpleNamespace(
            opts={"managed_check_constraints": managed}
        )
        return checks.managed_check_constraint_names(
            autogen_context, schema, tname
        )

    def test_plain_table_name(self):
        eq_(
            self._names({"t": ["ck_a"]}, None, "t"),
            frozenset(["ck_a"]),
        )

    def test_other_table_not_matched(self):
        eq_(self._names({"t": ["ck_a"]}, None, "t2"), frozenset())

    def test_schema_qualified_key(self):
        eq_(
            self._names({"s.t": ["ck_a"]}, "s", "t"),
            frozenset(["ck_a"]),
        )
        eq_(self._names({"s.t": ["ck_a"]}, None, "t"), frozenset())
        eq_(self._names({"s.t": ["ck_a"]}, "other", "t"), frozenset())

    def test_tuple_key(self):
        eq_(
            self._names({("s", "t"): ["ck_a"]}, "s", "t"),
            frozenset(["ck_a"]),
        )

    def test_no_option(self):
        eq_(self._names(None, None, "t"), frozenset())


class ManagedCheckConstraintAutogenTest(AutogenFixtureTest, TestBase):
    __backend__ = True
    __requires__ = ("check_constraint_reflection",)

    def _managed_opts(self, managed):
        return {"managed_check_constraints": managed}

    def _ck_diffs(self, diffs):
        return [
            d
            for d in diffs
            if d[0] in ("add_constraint", "remove_constraint")
            and isinstance(d[1], CheckConstraint)
        ]

    def test_changed_expression_is_drop_and_create(self):
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 2)
        # explicit drop + create pair, drop first
        eq_(ck_diffs[0][0], "remove_constraint")
        eq_(ck_diffs[1][0], "add_constraint")
        eq_(ck_diffs[0][1].name, "ck_t_x")
        eq_(ck_diffs[1][1].name, "ck_t_x")
        # the drop carries the original expression, the create carries
        # the new one
        eq_(str(ck_diffs[0][1].sqltext), "x >= 0")
        eq_(str(ck_diffs[1][1].sqltext), "x > 0")

    def test_whitespace_only_difference_ignored(self):
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x  >=   0", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        eq_(self._ck_diffs(diffs), [])

    def test_outer_parens_difference_ignored(self):
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("(x >= 0)", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        eq_(self._ck_diffs(diffs), [])

    def test_string_literal_difference_detected(self):
        m1 = _table(
            Column("x", String(10)),
            CheckConstraint("x IN ('A', 'B')", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", String(10)),
            CheckConstraint("x IN ('a', 'B')", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        eq_(len(self._ck_diffs(diffs)), 2)

    def test_quoted_identifier_expression(self):
        m1 = _table(
            Column("x", String(10)),
            CheckConstraint("\"x\" IN ('A','B')", name="ck_t_x"),
        )
        # same expression, only formatting differs
        m2 = _table(
            Column("x", String(10)),
            CheckConstraint("( \"x\"  IN  ( 'A', 'B' ) )", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        eq_(self._ck_diffs(diffs), [])

        # changing the quoted identifier's case *is* a difference
        m3 = _table(
            Column("x", String(10)),
            CheckConstraint("\"X\" IN ('A','B')", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m3, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        eq_(len(self._ck_diffs(diffs)), 2)

    def test_added_constraint(self):
        m1 = _table(Column("x", Integer))
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 1)
        eq_(ck_diffs[0][0], "add_constraint")
        eq_(ck_diffs[0][1].name, "ck_t_x")

    def test_removed_constraint(self):
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        m2 = _table(Column("x", Integer))
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 1)
        eq_(ck_diffs[0][0], "remove_constraint")
        eq_(ck_diffs[0][1].name, "ck_t_x")

    _byname_plugin_opts = {
        "autogenerate_plugins": [
            "alembic.autogenerate.*",
            "alembic.ext.checkconstraint_byname",
        ]
    }

    def _byname_opts(self, managed):
        opts = dict(self._byname_plugin_opts)
        opts["managed_check_constraints"] = managed
        return opts

    def test_no_double_add_with_byname_plugin(self):
        # when the opt-in name-based CHECK constraint plugin is also
        # enabled, managed constraints are handled exactly once
        m1 = _table(Column("x", Integer))
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._byname_opts({"t": ["ck_t_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 1)
        eq_(ck_diffs[0][0], "add_constraint")

    def test_no_double_remove_with_byname_plugin(self):
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        m2 = _table(Column("x", Integer))
        diffs = self._fixture(
            m1, m2, opts=self._byname_opts({"t": ["ck_t_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 1)
        eq_(ck_diffs[0][0], "remove_constraint")

    def test_no_double_change_with_byname_plugin(self):
        # expression change: the managed comparator's drop + create
        # pair, and nothing more
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._byname_opts({"t": ["ck_t_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 2)
        eq_(ck_diffs[0][0], "remove_constraint")
        eq_(ck_diffs[1][0], "add_constraint")

    def test_unmanaged_constraint_not_compared(self):
        # a named CHECK constraint that is not listed in
        # managed_check_constraints keeps the default handling,
        # i.e. it is not compared at all
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        diffs = self._fixture(m1, m2)
        eq_(self._ck_diffs(diffs), [])

        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_other"]})
        )
        eq_(self._ck_diffs(diffs), [])

    def test_anonymous_constraint_not_compared(self):
        # unnamed constraints cannot be named in the managed list and
        # are therefore never compared
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x"]})
        )
        eq_(self._ck_diffs(diffs), [])

    def test_same_name_different_tables(self):
        m1 = MetaData()
        Table(
            "t1",
            m1,
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_x"),
        )
        Table(
            "t2",
            m1,
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_x"),
        )
        m2 = MetaData()
        Table(
            "t1",
            m2,
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_x"),
        )
        Table(
            "t2",
            m2,
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_x"),
        )
        # only t1's constraint is managed; t2's is left alone
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t1": ["ck_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 2)
        for d in ck_diffs:
            eq_(d[1].table.name, "t1")

        # managing both tables reports both
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t1": ["ck_x"], "t2": ["ck_x"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 4)
        eq_(
            {d[1].table.name for d in ck_diffs},
            {"t1", "t2"},
        )

    def test_naming_convention_name(self):
        naming_convention = {"ck": "ck_%(table_name)s_%(constraint_name)s"}
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x_positive"),
        )
        m2 = MetaData(naming_convention=naming_convention)
        Table(
            "t",
            m2,
            Column("x", Integer),
            CheckConstraint("x > 0", name="x_positive"),
        )
        diffs = self._fixture(
            m1, m2, opts=self._managed_opts({"t": ["ck_t_x_positive"]})
        )
        ck_diffs = self._ck_diffs(diffs)
        eq_(len(ck_diffs), 2)
        eq_(ck_diffs[0][0], "remove_constraint")
        eq_(ck_diffs[1][0], "add_constraint")

    def test_failed_comparison_emits_complete_pair(self):
        # when expressions cannot be normalized, the difference is
        # reported conservatively, and always as a complete
        # drop + create pair, never a half-set of operations
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        with mock.patch.object(
            checks, "normalize_check_expression", return_value=None
        ):
            uo = self._fixture(
                m1,
                m2,
                opts=self._managed_opts({"t": ["ck_t_x"]}),
                return_ops=True,
            )
        table_ops = [o for o in uo.ops if isinstance(o, ops.ModifyTableOps)]
        eq_(len(table_ops), 1)
        eq_(len(table_ops[0].ops), 2)
        is_true(isinstance(table_ops[0].ops[0], ops.DropConstraintOp))
        is_true(isinstance(table_ops[0].ops[1], ops.CreateCheckConstraintOp))

    def test_sqlite_marks_ops_for_batch_rendering(self):
        m1 = _table(
            Column("x", Integer),
            CheckConstraint("x >= 0", name="ck_t_x"),
        )
        m2 = _table(
            Column("x", Integer),
            CheckConstraint("x > 0", name="ck_t_x"),
        )
        uo = self._fixture(
            m1,
            m2,
            opts=self._managed_opts({"t": ["ck_t_x"]}),
            return_ops=True,
        )
        table_ops = [o for o in uo.ops if isinstance(o, ops.ModifyTableOps)]
        eq_(len(table_ops), 1)
        if config.db.dialect.name == "sqlite":
            is_true(table_ops[0].render_as_batch)
            # the flag propagates to the downgrade ops
            reversed_ops = table_ops[0].reverse()
            is_true(reversed_ops.render_as_batch)
        else:
            is_false(table_ops[0].render_as_batch)


class SQLiteRoundTripTest(TestBase):
    """Generate a migration against a SQLite database, run the upgrade
    and the downgrade, and verify the CHECK constraint behavior at
    each step."""

    def _metadata(self, condition):
        m = MetaData()
        Table(
            "item",
            m,
            Column("id", Integer, primary_key=True),
            Column("price", Integer),
            CheckConstraint(condition, name="ck_price"),
        )
        return m

    def _produce_ops(self, conn, target_metadata):
        context = MigrationContext.configure(
            connection=conn,
            opts={
                "target_metadata": target_metadata,
                "managed_check_constraints": {"item": ["ck_price"]},
            },
        )
        autogen_context = api.AutogenContext(context, target_metadata)
        uo = ops.UpgradeOps(ops=[])
        autogenerate._produce_net_changes(autogen_context, uo)
        return context, uo

    def _run_ops(self, context, rendered_code):
        ns = {"op": op, "sa": sa}
        exec("def _migrate():\n" + rendered_code, ns)
        with Operations.context(context):
            ns["_migrate"]()

    def _insert_price(self, conn, value):
        conn.execute(
            text("INSERT INTO item (price) VALUES (:v)"), {"v": value}
        )
        conn.commit()

    def test_upgrade_downgrade_round_trip(self):
        engine = sa.create_engine("sqlite://")
        m1 = self._metadata("price >= 0")
        m1.create_all(engine)

        m2 = self._metadata("price > 0")

        with engine.connect() as conn:
            # initially, 0 is allowed
            self._insert_price(conn, 0)
            conn.execute(text("DELETE FROM item"))
            conn.commit()

            context, uo = self._produce_ops(conn, m2)
            eq_(len(uo.ops), 1)

            upgrade_code = api.render_python_code(
                uo, migration_context=context
            )
            downgrade_code = api.render_python_code(
                uo.reverse(), migration_context=context
            )

            # SQLite renders the batch "recreate table" path
            assert "batch_alter_table" in upgrade_code
            assert "batch_alter_table" in downgrade_code
            # the downgrade re-creates the constraint with the
            # *original* expression
            assert "'price >= 0'" in downgrade_code
            assert "'price > 0'" in upgrade_code

            # upgrade: 0 is now rejected
            self._run_ops(context, upgrade_code)
            conn.commit()
            try:
                self._insert_price(conn, 0)
                raise AssertionError("insert of 0 should fail after upgrade")
            except sa.exc.IntegrityError:
                conn.rollback()
            self._insert_price(conn, 5)
            conn.execute(text("DELETE FROM item"))
            conn.commit()

            # downgrade: the original constraint is restored
            self._run_ops(context, downgrade_code)
            conn.commit()
            self._insert_price(conn, 0)

            # verify the restored constraint really is the original
            # one: -1 must still be rejected
            try:
                self._insert_price(conn, -1)
                raise AssertionError(
                    "insert of -1 should fail after downgrade"
                )
            except sa.exc.IntegrityError:
                conn.rollback()
            conn.execute(text("DELETE FROM item"))
            conn.commit()

            # and the cycle is repeatable: upgrade again, 0 is
            # rejected again
            self._run_ops(context, upgrade_code)
            conn.commit()
            try:
                self._insert_price(conn, 0)
                raise AssertionError(
                    "insert of 0 should fail after re-upgrade"
                )
            except sa.exc.IntegrityError:
                conn.rollback()


class OfflineRenderTest(TestBase):
    """Render managed CHECK constraint operations to migration code
    without a database connection."""

    def _make_ops(self, render_as_batch):
        m = MetaData()
        t = Table(
            "item",
            m,
            Column("id", Integer, primary_key=True),
            Column("price", Integer),
        )
        old_ck = CheckConstraint("price >= 0", name="ck_price")
        t.append_constraint(old_ck)
        drop_op = ops.DropConstraintOp.from_constraint(old_ck)
        t.constraints.discard(old_ck)
        new_ck = CheckConstraint("price > 0", name="ck_price")
        t.append_constraint(new_ck)
        add_op = ops.AddConstraintOp.from_constraint(new_ck)
        return ops.UpgradeOps(
            ops=[
                ops.ModifyTableOps(
                    "item",
                    [drop_op, add_op],
                    render_as_batch=render_as_batch,
                )
            ]
        )

    def test_render_sqlite_batch(self):
        context = MigrationContext.configure(dialect_name="sqlite")
        uo = self._make_ops(render_as_batch=True)
        upgrade_code = api.render_python_code(uo, migration_context=context)
        assert (
            "with op.batch_alter_table('item', schema=None) as batch_op:"
            in upgrade_code
        )
        assert "batch_op.create_check_constraint('ck_price', 'price > 0')" in (
            upgrade_code
        )

        downgrade_code = api.render_python_code(
            uo.reverse(), migration_context=context
        )
        assert "batch_alter_table" in downgrade_code
        # downgrade uses the original expression
        assert "'price >= 0'" in downgrade_code

    def test_render_other_dialects_plain_ops(self):
        context = MigrationContext.configure(dialect_name="postgresql")
        uo = self._make_ops(render_as_batch=False)
        upgrade_code = api.render_python_code(uo, migration_context=context)
        assert "batch_alter_table" not in upgrade_code
        assert "op.drop_constraint('ck_price', 'item', type_='check')" in (
            upgrade_code
        )
        assert (
            "op.create_check_constraint('ck_price', 'item', 'price > 0')"
            in upgrade_code
        )

        downgrade_code = api.render_python_code(
            uo.reverse(), migration_context=context
        )
        assert "op.drop_constraint('ck_price', 'item', type_='check')" in (
            downgrade_code
        )
        assert (
            "op.create_check_constraint('ck_price', 'item', 'price >= 0')"
            in downgrade_code
        )


class DemoScriptTest(TestBase):
    """The tools/demo_managed_check_constraints.py script demonstrates
    generate -> upgrade -> downgrade end to end; keep it working."""

    def test_demo_script(self):
        import subprocess

        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools",
            "demo_managed_check_constraints.py",
        )
        proc = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=90,
        )
        eq_(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        assert "DEMO OK" in proc.stdout
