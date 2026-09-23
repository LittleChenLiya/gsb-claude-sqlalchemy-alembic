from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text

from alembic import autogenerate
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import ModifyTableOps
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.testing.suite._autogen_fixtures import AutogenFixtureTest
from alembic.util import CommandError


def _account_db_schema(include_index=False):
    m = MetaData()
    t = Table(
        "account",
        m,
        Column("id", Integer, primary_key=True),
        Column("nickname", String(50)),
    )
    if include_index:
        Index("ix_account_nickname", t.c.nickname)
    return m


def _account_model_schema(include_index=False, nullable=True):
    m = MetaData()
    t = Table(
        "account",
        m,
        Column("id", Integer, primary_key=True),
        Column("display_name", String(50), nullable=nullable),
    )
    if include_index:
        Index("ix_account_nickname", t.c.display_name)
    return m


rename_map = {(None, "account", "nickname"): "display_name"}


class ColumnRenameAutogenTest(AutogenFixtureTest, TestBase):
    __requires__ = ()

    def _flatten_diffs(self, diffs):
        for d in diffs:
            if isinstance(d, list):
                yield from self._flatten_diffs(d)
            else:
                yield d

    def _rename_opts(self, renames=None):
        return {"column_renames": renames or rename_map}

    def test_rename_not_drop_add(self):
        diffs = self._fixture(
            _account_db_schema(),
            _account_model_schema(),
            opts=self._rename_opts(),
        )
        flat = list(self._flatten_diffs(diffs))
        eq_(len(flat), 1)
        eq_(flat[0][0], "modify_name")
        eq_(flat[0][1:4], (None, "account", "nickname"))
        eq_(flat[0][-2:], ("nickname", "display_name"))

    def test_rename_two_tuple_key(self):
        diffs = self._fixture(
            _account_db_schema(),
            _account_model_schema(),
            opts=self._rename_opts({("account", "nickname"): "display_name"}),
        )
        flat = list(self._flatten_diffs(diffs))
        eq_(len(flat), 1)
        eq_(flat[0][0], "modify_name")

    def test_no_mapping_keeps_drop_add(self):
        diffs = self._fixture(_account_db_schema(), _account_model_schema())
        flat = list(self._flatten_diffs(diffs))
        eq_(
            {d[0] for d in flat},
            {"add_column", "remove_column"},
        )
        eq_(
            {d[3].name for d in flat},
            {"nickname", "display_name"},
        )

    def test_rename_preserves_nullable_and_type_changes(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(30)),
        )
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(50), nullable=False),
        )
        diffs = self._fixture(m1, m2, opts=self._rename_opts())
        flat = list(self._flatten_diffs(diffs))
        eq_(
            {d[0] for d in flat},
            {"modify_name", "modify_type", "modify_nullable"},
        )
        for d in flat:
            eq_(d[1:4], (None, "account", "nickname"))

    def test_rename_with_index_no_index_diffs(self):
        diffs = self._fixture(
            _account_db_schema(include_index=True),
            _account_model_schema(include_index=True),
            opts=self._rename_opts(),
        )
        flat = list(self._flatten_diffs(diffs))
        eq_(len(flat), 1)
        eq_(flat[0][0], "modify_name")

    def test_rename_fk_referred_column(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50), unique=True),
        )
        Table(
            "profile",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nick", String(50), ForeignKey("account.nickname")),
        )
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(50), unique=True),
        )
        Table(
            "profile",
            m2,
            Column("id", Integer, primary_key=True),
            Column("nick", String(50), ForeignKey("account.display_name")),
        )
        diffs = self._fixture(m1, m2, opts=self._rename_opts())
        flat = list(self._flatten_diffs(diffs))
        eq_([d[0] for d in flat], ["modify_name"])

    def test_rename_composite_fk(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer),
            Column("nickname", String(50)),
            PrimaryKeyConstraint("id", "nickname"),
        )
        Table(
            "profile",
            m1,
            Column("id", Integer, primary_key=True),
            Column("acct_id", Integer),
            Column("nick", String(50)),
            ForeignKeyConstraint(
                ["acct_id", "nick"], ["account.id", "account.nickname"]
            ),
        )
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer),
            Column("display_name", String(50)),
            PrimaryKeyConstraint("id", "display_name"),
        )
        Table(
            "profile",
            m2,
            Column("id", Integer, primary_key=True),
            Column("acct_id", Integer),
            Column("nick", String(50)),
            ForeignKeyConstraint(
                ["acct_id", "nick"], ["account.id", "account.display_name"]
            ),
        )
        diffs = self._fixture(m1, m2, opts=self._rename_opts())
        flat = list(self._flatten_diffs(diffs))
        eq_([d[0] for d in flat], ["modify_name"])

    def test_render_upgrade_and_downgrade(self):
        uo = self._fixture(
            _account_db_schema(),
            _account_model_schema(nullable=False),
            opts=self._rename_opts(),
            return_ops=True,
        )
        upgrade_code = autogenerate.render_python_code(uo)
        assert "new_column_name='display_name'" in upgrade_code
        assert "nullable=False" in upgrade_code

        downgrade_code = autogenerate.render_python_code(uo.reverse())
        assert "new_column_name='nickname'" in downgrade_code
        assert "nullable=True" in downgrade_code

    def test_render_as_batch(self):
        uo = self._fixture(
            _account_db_schema(),
            _account_model_schema(nullable=False),
            opts=self._rename_opts(),
            return_ops=True,
        )
        code = autogenerate.render_python_code(uo, render_as_batch=True)
        assert "batch_op.alter_column('nickname'" in code
        assert "new_column_name='display_name'" in code

    def test_duplicate_target_rejected(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50)),
            Column("other", String(50)),
        )
        assert_raises_message(
            CommandError,
            r"table 'account'.*same target column 'display_name'",
            self._fixture,
            m1,
            _account_model_schema(),
            opts=self._rename_opts(
                {
                    (None, "account", "nickname"): "display_name",
                    (None, "account", "other"): "display_name",
                }
            ),
        )

    def test_missing_old_column_rejected(self):
        assert_raises_message(
            CommandError,
            r"column 'missing'.*table 'account'",
            self._fixture,
            _account_db_schema(),
            _account_model_schema(),
            opts=self._rename_opts(
                {(None, "account", "missing"): "display_name"}
            ),
        )

    def test_new_column_exists_in_db_rejected(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50)),
            Column("display_name", String(50)),
        )
        assert_raises_message(
            CommandError,
            r"column 'display_name' already exists in the database",
            self._fixture,
            m1,
            _account_model_schema(),
            opts=self._rename_opts(),
        )

    def test_old_column_still_in_metadata_rejected(self):
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50)),
            Column("display_name", String(50)),
        )
        assert_raises_message(
            CommandError,
            r"column 'nickname' is still present in the target MetaData",
            self._fixture,
            _account_db_schema(),
            m2,
            opts=self._rename_opts(),
        )

    def test_new_column_not_in_metadata_rejected(self):
        assert_raises_message(
            CommandError,
            r"column 'nothere' is not present in the target MetaData",
            self._fixture,
            _account_db_schema(),
            _account_model_schema(),
            opts=self._rename_opts({(None, "account", "nickname"): "nothere"}),
        )

    def test_chained_rename_rejected(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50)),
        )
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(50)),
            Column("final_name", String(50)),
        )
        assert_raises_message(
            CommandError,
            r"chained or circular column rename on table 'account'",
            self._fixture,
            m1,
            m2,
            opts=self._rename_opts(
                {
                    (None, "account", "nickname"): "display_name",
                    (None, "account", "display_name"): "final_name",
                }
            ),
        )

    def test_circular_rename_rejected(self):
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50)),
            Column("other", String(50)),
        )
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(50)),
            Column("other_name", String(50)),
        )
        assert_raises_message(
            CommandError,
            r"chained or circular column rename on table 'account'",
            self._fixture,
            m1,
            m2,
            opts=self._rename_opts(
                {
                    (None, "account", "nickname"): "other_name",
                    (None, "account", "other"): "nickname",
                }
            ),
        )

    def test_unknown_table_rejected(self):
        assert_raises_message(
            CommandError,
            r"table 'nothere' is not present in the target MetaData",
            self._fixture,
            _account_db_schema(),
            _account_model_schema(),
            opts=self._rename_opts(
                {(None, "nothere", "nickname"): "display_name"}
            ),
        )

    def test_invalid_key_rejected(self):
        assert_raises_message(
            CommandError,
            r"column_renames key.*is not a",
            self._fixture,
            _account_db_schema(),
            _account_model_schema(),
            opts=self._rename_opts({"account.nickname": "display_name"}),
        )

    def test_validation_is_all_or_nothing(self):
        # one valid mapping plus one invalid mapping: the whole
        # autogenerate run fails before any operations are produced
        m1 = MetaData()
        Table(
            "account",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nickname", String(50)),
        )
        assert_raises_message(
            CommandError,
            r"column 'missing'.*table 'account'",
            self._fixture,
            m1,
            _account_model_schema(),
            opts=self._rename_opts(
                {
                    (None, "account", "nickname"): "display_name",
                    (None, "account", "missing"): "whatever",
                }
            ),
        )


class ColumnRenameRoundTripTest(TestBase):
    """Acceptance: temp SQLite database, account(id, nickname) with two
    rows, rename nickname -> display_name via the mapping, execute the
    generated upgrade and downgrade, data preserved in both directions."""

    __requires__ = ()

    def _engine(self):
        return create_engine("sqlite://")

    def _run_ops(self, mc, ops_list):
        op_obj = Operations(mc)
        for op in ops_list:
            if isinstance(op, ModifyTableOps):
                for sub_op in op.ops:
                    op_obj.invoke(sub_op)
            else:
                op_obj.invoke(op)

    def _account_fixture(self, conn, include_index=False):
        m1 = _account_db_schema(include_index=include_index)
        m1.create_all(conn)
        conn.execute(
            text("insert into account (id, nickname) values (1, 'alice')")
        )
        conn.execute(
            text("insert into account (id, nickname) values (2, 'bob')")
        )
        conn.commit()

    def _produce(self, conn, m2, renames=None):
        mc = MigrationContext.configure(
            connection=conn,
            opts={
                "target_metadata": m2,
                "column_renames": renames or rename_map,
            },
        )
        return mc, autogenerate.produce_migrations(mc, m2)

    def test_upgrade_downgrade_data_preserved(self):
        eng = self._engine()
        conn = eng.connect()
        self._account_fixture(conn)

        m2 = _account_model_schema()
        mc, migration_script = self._produce(conn, m2)

        self._run_ops(mc, migration_script.upgrade_ops.ops)
        eq_(
            [c["name"] for c in inspect(conn).get_columns("account")],
            ["id", "display_name"],
        )
        eq_(
            conn.execute(
                text("select id, display_name from account order by id")
            ).all(),
            [(1, "alice"), (2, "bob")],
        )

        self._run_ops(mc, migration_script.downgrade_ops.ops)
        eq_(
            [c["name"] for c in inspect(conn).get_columns("account")],
            ["id", "nickname"],
        )
        eq_(
            conn.execute(
                text("select id, nickname from account order by id")
            ).all(),
            [(1, "alice"), (2, "bob")],
        )
        conn.close()

    def test_rename_with_index_reflected(self):
        eng = self._engine()
        conn = eng.connect()
        self._account_fixture(conn, include_index=True)

        m2 = _account_model_schema(include_index=True)
        mc, migration_script = self._produce(conn, m2)

        upgrade_code = autogenerate.render_python_code(
            migration_script.upgrade_ops
        )
        assert "create_index" not in upgrade_code
        assert "drop_index" not in upgrade_code
        assert "new_column_name='display_name'" in upgrade_code

        self._run_ops(mc, migration_script.upgrade_ops.ops)
        eq_(
            [
                (ix["name"], ix["column_names"])
                for ix in inspect(conn).get_indexes("account")
            ],
            [("ix_account_nickname", ["display_name"])],
        )
        eq_(
            conn.execute(
                text("select id, display_name from account order by id")
            ).all(),
            [(1, "alice"), (2, "bob")],
        )
        conn.close()

    def test_batch_rename_with_nullable_change(self):
        eng = self._engine()
        conn = eng.connect()
        self._account_fixture(conn)

        m2 = _account_model_schema(nullable=False)
        mc, migration_script = self._produce(conn, m2)

        upgrade_code = autogenerate.render_python_code(
            migration_script.upgrade_ops, render_as_batch=True
        )
        assert "batch_op.alter_column('nickname'" in upgrade_code
        assert "new_column_name='display_name'" in upgrade_code
        assert "nullable=False" in upgrade_code

        op_obj = Operations(mc)
        with op_obj.batch_alter_table("account") as batch_op:
            batch_op.alter_column(
                "nickname",
                new_column_name="display_name",
                nullable=False,
                existing_type=String(50),
            )

        cols = {c["name"]: c for c in inspect(conn).get_columns("account")}
        eq_(set(cols), {"id", "display_name"})
        eq_(cols["display_name"]["nullable"], False)
        eq_(
            conn.execute(
                text("select id, display_name from account order by id")
            ).all(),
            [(1, "alice"), (2, "bob")],
        )

        # batch downgrade restores the original column
        with op_obj.batch_alter_table("account") as batch_op:
            batch_op.alter_column(
                "display_name",
                new_column_name="nickname",
                nullable=True,
                existing_type=String(50),
            )
        eq_(
            [c["name"] for c in inspect(conn).get_columns("account")],
            ["id", "nickname"],
        )
        eq_(
            conn.execute(
                text("select id, nickname from account order by id")
            ).all(),
            [(1, "alice"), (2, "bob")],
        )
        conn.close()
