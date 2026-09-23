from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy import UniqueConstraint

from alembic import autogenerate
from alembic.autogenerate import api
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.testing.suite._autogen_fixtures import AutogenFixtureTest
from alembic.util import CommandError

RENAME_MAP = {(None, "account", "nickname"): "display_name"}


def _db_metadata():
    m = MetaData()
    Table(
        "account",
        m,
        Column("id", Integer, primary_key=True),
        Column("nickname", String(50), nullable=False),
    )
    return m


def _model_metadata():
    m = MetaData()
    Table(
        "account",
        m,
        Column("id", Integer, primary_key=True),
        Column("display_name", String(50), nullable=False),
    )
    return m


class ColumnRenameMapTest(AutogenFixtureTest, TestBase):
    __backend__ = True

    def _rename_opts(self, rename_map=RENAME_MAP):
        return {"column_rename_map": rename_map}

    def _flatten(self, diffs):
        for d in diffs:
            if isinstance(d, list):
                yield from self._flatten(d)
            else:
                yield d

    def test_rename_detected_instead_of_add_drop(self):
        diffs = list(
            self._flatten(
                self._fixture(
                    _db_metadata(),
                    _model_metadata(),
                    opts=self._rename_opts(),
                )
            )
        )
        eq_(
            diffs,
            [("rename_column", None, "account", "nickname", "display_name")],
        )

    def test_no_map_falls_back_to_add_drop(self):
        diffs = list(
            self._flatten(self._fixture(_db_metadata(), _model_metadata()))
        )
        eq_(
            {d[0] for d in diffs},
            {"add_column", "remove_column"},
        )

    def test_rename_preserves_nullable_change(self):
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(50), nullable=True),
        )
        uo = self._fixture(
            _db_metadata(), m2, opts=self._rename_opts(), return_ops=True
        )
        (modify_table_op,) = uo.ops
        (alter_op,) = modify_table_op.ops
        assert isinstance(alter_op, ops.AlterColumnOp)
        eq_(alter_op.column_name, "nickname")
        eq_(alter_op.modify_name, "display_name")
        eq_(alter_op.modify_nullable, True)
        eq_(alter_op.existing_nullable, False)

    def test_rename_preserves_type_change(self):
        m2 = MetaData()
        Table(
            "account",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(100), nullable=False),
        )
        uo = self._fixture(
            _db_metadata(), m2, opts=self._rename_opts(), return_ops=True
        )
        (modify_table_op,) = uo.ops
        (alter_op,) = modify_table_op.ops
        assert isinstance(alter_op, ops.AlterColumnOp)
        eq_(alter_op.modify_name, "display_name")
        eq_(alter_op.modify_type.length, 100)

    def test_rename_is_reversible(self):
        uo = self._fixture(
            _db_metadata(),
            _model_metadata(),
            opts=self._rename_opts(),
            return_ops=True,
        )
        down = uo.reverse()
        (modify_table_op,) = down.ops
        (alter_op,) = modify_table_op.ops
        assert isinstance(alter_op, ops.AlterColumnOp)
        eq_(alter_op.column_name, "display_name")
        eq_(alter_op.modify_name, "nickname")

    def test_index_follows_renamed_column(self):
        m1 = _db_metadata()
        Index("ix_account_nickname", m1.tables["account"].c.nickname)
        m2 = _model_metadata()
        Index("ix_account_nickname", m2.tables["account"].c.display_name)
        diffs = list(
            self._flatten(self._fixture(m1, m2, opts=self._rename_opts()))
        )
        eq_(
            diffs,
            [("rename_column", None, "account", "nickname", "display_name")],
        )

    def test_named_unique_constraint_follows_renamed_column(self):
        m1 = _db_metadata()
        account1 = m1.tables["account"]
        account1.append_constraint(
            UniqueConstraint("nickname", name="uq_account_nickname")
        )
        m2 = _model_metadata()
        account2 = m2.tables["account"]
        account2.append_constraint(
            UniqueConstraint("display_name", name="uq_account_nickname")
        )
        diffs = list(
            self._flatten(self._fixture(m1, m2, opts=self._rename_opts()))
        )
        eq_(
            diffs,
            [("rename_column", None, "account", "nickname", "display_name")],
        )

    def test_composite_fk_follows_renamed_referred_column(self):
        m1 = _db_metadata()
        account1 = m1.tables["account"]
        account1.append_column(Column("tenant", Integer, nullable=False))
        account1.append_constraint(
            UniqueConstraint("nickname", "tenant", name="uq_account")
        )
        Table(
            "entry",
            m1,
            Column("id", Integer, primary_key=True),
            Column("nick", String(50)),
            Column("tenant", Integer),
            ForeignKeyConstraint(
                ["nick", "tenant"],
                ["account.nickname", "account.tenant"],
                name="fk_entry_account",
            ),
        )
        m2 = _model_metadata()
        account2 = m2.tables["account"]
        account2.append_column(Column("tenant", Integer, nullable=False))
        account2.append_constraint(
            UniqueConstraint("display_name", "tenant", name="uq_account")
        )
        Table(
            "entry",
            m2,
            Column("id", Integer, primary_key=True),
            Column("nick", String(50)),
            Column("tenant", Integer),
            ForeignKeyConstraint(
                ["nick", "tenant"],
                ["account.display_name", "account.tenant"],
                name="fk_entry_account",
            ),
        )
        diffs = list(
            self._flatten(self._fixture(m1, m2, opts=self._rename_opts()))
        )
        eq_(
            diffs,
            [("rename_column", None, "account", "nickname", "display_name")],
        )

    def test_invalid_maps_are_rejected_before_generating(self):
        cases = [
            # duplicate target: two old columns to the same new name
            (
                {
                    (None, "account", "nickname"): "display_name",
                    (None, "account", "id"): "display_name",
                },
                "one-to-one",
            ),
            # old column does not exist in the database
            (
                {(None, "account", "nonexistent"): "display_name"},
                "'nonexistent' does not exist in the database",
            ),
            # new column not present in target MetaData
            (
                {(None, "account", "nickname"): "missing"},
                "'missing' does not exist in the target MetaData",
            ),
            # new column already exists in the database
            (
                {(None, "account", "nickname"): "id"},
                "already exists in the database",
            ),
            # chained rename
            (
                {
                    (None, "account", "nickname"): "display_name",
                    (None, "account", "display_name"): "other",
                },
                "chain",
            ),
            # rename cycle
            (
                {
                    (None, "account", "nickname"): "id",
                    (None, "account", "id"): "nickname",
                },
                "chain",
            ),
            # table not present on both sides
            (
                {(None, "no_such_table", "nickname"): "display_name"},
                "'no_such_table'",
            ),
        ]
        for rename_map, message in cases:
            assert_raises_message(
                CommandError,
                message,
                self._fixture,
                _db_metadata(),
                _model_metadata(),
                opts=self._rename_opts(rename_map),
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
            "still present in the target MetaData",
            self._fixture,
            _db_metadata(),
            m2,
            opts=self._rename_opts(),
        )

    def test_error_message_names_table_and_columns(self):
        try:
            self._fixture(
                _db_metadata(),
                _model_metadata(),
                opts=self._rename_opts(
                    {(None, "account", "nonexistent"): "display_name"}
                ),
            )
        except CommandError as err:
            msg = str(err)
            assert "account" in msg
            assert "nonexistent" in msg
        else:
            assert False, "CommandError was not raised"


class SQLiteBatchRenameRoundTripTest(TestBase):
    """Generate upgrade + downgrade from a rename map and execute both
    against a real SQLite database in batch mode, verifying that data is
    preserved across both renames."""

    def _engine(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "create table account ("
                    "id integer primary key, "
                    "nickname varchar(50) not null)"
                )
            )
            conn.execute(
                text("create index ix_account_nickname on account (nickname)")
            )
            conn.execute(
                text("insert into account values (1, 'alice'), (2, 'bob')")
            )
        return engine

    def _produce(self, conn, metadata):
        context = MigrationContext.configure(
            conn,
            opts={
                "column_rename_map": RENAME_MAP,
                "compare_type": True,
                "compare_server_default": True,
                "target_metadata": metadata,
            },
        )
        return autogenerate.produce_migrations(context, metadata)

    def _run_rendered(self, conn, rendered):
        operations = Operations(MigrationContext.configure(conn))
        namespace = {"op": operations, "sa": sa}
        exec("def _run():\n" + rendered + "\n_run()", namespace)

    def test_upgrade_downgrade_round_trip(self):
        engine = self._engine()
        metadata = MetaData()
        Table(
            "account",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(50), nullable=True),
            Index("ix_account_nickname", "display_name"),
        )

        with engine.connect() as conn:
            migration_script = self._produce(conn, metadata)
            upgrade_code = api.render_python_code(
                migration_script.upgrade_ops, render_as_batch=True
            )
            downgrade_code = api.render_python_code(
                migration_script.downgrade_ops, render_as_batch=True
            )

        assert "new_column_name='display_name'" in upgrade_code
        assert "new_column_name='nickname'" in downgrade_code
        # the rename keeps the nullable change on the same op
        assert "nullable=True" in upgrade_code

        with engine.connect() as conn:
            self._run_rendered(conn, upgrade_code)
            conn.commit()

        with engine.connect() as conn:
            eq_(
                conn.execute(text("select * from account")).all(),
                [(1, "alice"), (2, "bob")],
            )
            inspector = inspect(conn)
            eq_(
                [c["name"] for c in inspector.get_columns("account")],
                ["id", "display_name"],
            )
            eq_(
                [
                    ix["column_names"]
                    for ix in inspector.get_indexes("account")
                ],
                [["display_name"]],
            )

        with engine.connect() as conn:
            self._run_rendered(conn, downgrade_code)
            conn.commit()

        with engine.connect() as conn:
            eq_(
                conn.execute(text("select * from account")).all(),
                [(1, "alice"), (2, "bob")],
            )
            eq_(
                [c["name"] for c in inspect(conn).get_columns("account")],
                ["id", "nickname"],
            )
