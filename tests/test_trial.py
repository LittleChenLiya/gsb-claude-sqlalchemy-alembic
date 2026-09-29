"""Tests for the SQLite migration trial runner (:mod:`alembic.trial`)."""

import sqlite3

import pytest

from alembic import command
from alembic.config import Config
from alembic.testing.fixtures import TestBase
from alembic.trial import run_sqlite_trial
from alembic.trial import STATUS_CANCELLED
from alembic.trial import STATUS_FAILED
from alembic.trial import STATUS_SUCCESS
from alembic.trial import TrialError

ENV_PY = """\
from alembic.trial.env import run_migrations

run_migrations()
"""

REV_TEMPLATE = """\
from alembic import op
import sqlalchemy as sa

revision = {rev!r}
down_revision = {down!r}


def upgrade():
{body}


def downgrade():
    pass
"""


def _write_script_dir(tmp_path, revisions):
    """Create a migration environment using the trial env.py.

    :param revisions: list of (revision, down_revision, upgrade_body).
    """
    script_dir = tmp_path / "scripts"
    versions = script_dir / "versions"
    versions.mkdir(parents=True)
    (script_dir / "env.py").write_text(ENV_PY, encoding="utf-8")
    for rev, down, body in revisions:
        indented = "\n".join(
            "    " + line if line.strip() else line
            for line in body.splitlines()
        )
        (versions / f"{rev}.py").write_text(
            REV_TEMPLATE.format(rev=rev, down=down, body=indented),
            encoding="utf-8",
        )
    return script_dir


def _write_source_db(path, rows=((1, "alpha"), (2, "beta"))):
    """Create a WAL-mode source database with an item table."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE item ("
        "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
    )
    conn.executemany("INSERT INTO item (id, name) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _config(script_dir):
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    return cfg


def _source_state(path):
    """(columns of item, rows of item, has alembic_version table)."""
    conn = sqlite3.connect(str(path))
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(item)")]
        rows = sorted(conn.execute("SELECT id, name FROM item"))
        has_version = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        return cols, rows, bool(has_version)
    finally:
        conn.close()


def _assert_source_untouched(path, expected_rows=((1, "alpha"), (2, "beta"))):
    cols, rows, has_version = _source_state(path)
    assert cols == ["id", "name"]
    assert rows == sorted(expected_rows)
    assert not has_version


def _assert_source_writable(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("INSERT INTO item (id, name) VALUES (99, 'probe')")
        conn.commit()
        conn.execute("DELETE FROM item WHERE id = 99")
        conn.commit()
    finally:
        conn.close()


ADD_FLAG = "op.add_column('item', sa.Column('flag', sa.Integer))"
ADD_TAG = "op.add_column('item', sa.Column('tag', sa.String))"
BAD_INSERT = "op.execute(\"INSERT INTO item (id, name) VALUES (1, 'dupe')\")"


@pytest.fixture
def good_env(tmp_path):
    """Two-step environment: t0001 adds a column, t0002 adds another."""
    script_dir = _write_script_dir(
        tmp_path,
        [("t0001", None, ADD_FLAG), ("t0002", "t0001", ADD_TAG)],
    )
    return _config(script_dir)


@pytest.fixture
def failing_env(tmp_path):
    """t0001 adds a column; t0002 violates the UNIQUE constraint."""
    script_dir = _write_script_dir(
        tmp_path,
        [("t0001", None, ADD_FLAG), ("t0002", "t0001", BAD_INSERT)],
    )
    return _config(script_dir)


@pytest.fixture
def source_db(tmp_path):
    path = tmp_path / "source.db"
    _write_source_db(path)
    return path


class SQLiteTrialTest(TestBase):
    def test_trial_success_leaves_source_untouched(self, source_db, good_env):
        result = run_sqlite_trial(source_db, good_env, "t0001")

        assert result.status == STATUS_SUCCESS
        assert result.ok
        assert result.start_revision is None
        assert result.end_revision == "t0001"
        assert result.applied_revisions == ("t0001",)
        assert result.error is None
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_trial_failure_is_not_reported_as_complete(
        self, source_db, failing_env
    ):
        result = run_sqlite_trial(source_db, failing_env, "head")

        assert result.status == STATUS_FAILED
        assert not result.ok
        assert result.start_revision is None
        # the first step committed on the copy; the failing step
        # rolled back
        assert result.end_revision == "t0001"
        assert result.applied_revisions == ("t0001",)
        assert result.error is not None
        assert "UNIQUE" in result.error
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_wal_only_committed_data_is_copied(self, tmp_path):
        """Committed data still living in the WAL must reach the copy."""
        db = tmp_path / "wal.db"
        writer = sqlite3.connect(str(db))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE item ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
        )
        writer.executemany(
            "INSERT INTO item (id, name) VALUES (?, ?)",
            [(1, "a"), (2, "b"), (3, "c")],
        )
        writer.commit()
        # data is committed but only present in the WAL; keep the
        # writer open so no checkpoint happens.
        try:
            script_dir = _write_script_dir(
                tmp_path,
                [
                    (
                        "t0001",
                        None,
                        "bind = op.get_bind()\n"
                        "count = bind.execute("
                        "sa.text('SELECT COUNT(*) FROM item')"
                        ").scalar()\n"
                        "assert count == 3, "
                        '"WAL data missing: %s" % count\n' + ADD_FLAG,
                    )
                ],
            )
            result = run_sqlite_trial(db, _config(script_dir), "head")
            assert result.status == STATUS_SUCCESS, result.error
            assert result.applied_revisions == ("t0001",)
        finally:
            writer.close()
        _assert_source_untouched(
            db, expected_rows=((1, "a"), (2, "b"), (3, "c"))
        )

    def test_source_with_active_read_connection(self, source_db, good_env):
        reader = sqlite3.connect(str(source_db))
        try:
            reader.execute("BEGIN")
            held = reader.execute("SELECT COUNT(*) FROM item").fetchone()
            assert held == (2,)
            result = run_sqlite_trial(source_db, good_env, "t0001")
            assert result.status == STATUS_SUCCESS, result.error
            # the pre-existing read connection still works afterwards
            assert reader.execute("SELECT COUNT(*) FROM item").fetchone() == (
                2,
            )
        finally:
            reader.close()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_attach_is_denied(self, tmp_path, source_db):
        script_dir = _write_script_dir(
            tmp_path,
            [
                (
                    "t0001",
                    None,
                    "op.execute(\"ATTACH DATABASE ':memory:' AS evil\")",
                )
            ],
        )
        result = run_sqlite_trial(source_db, _config(script_dir), "head")

        assert result.status == STATUS_FAILED
        assert "not authorized" in result.error.lower()
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_load_extension_is_denied(self, tmp_path, source_db):
        script_dir = _write_script_dir(
            tmp_path,
            [
                (
                    "t0001",
                    None,
                    "op.execute(\"SELECT load_extension('missing_ext')\")",
                )
            ],
        )
        result = run_sqlite_trial(source_db, _config(script_dir), "head")

        assert result.status == STATUS_FAILED
        assert "not authorized" in result.error.lower()
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)

    def test_unknown_revision_is_reported(self, source_db, good_env):
        result = run_sqlite_trial(source_db, good_env, "no_such_revision")

        assert result.status == STATUS_FAILED
        assert "Can't locate revision" in result.error
        assert result.applied_revisions == ()
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_migration_exception_rolls_back_and_cleans_up(
        self, tmp_path, source_db
    ):
        script_dir = _write_script_dir(
            tmp_path,
            [
                ("t0001", None, ADD_FLAG),
                ("t0002", "t0001", "raise RuntimeError('boom')"),
            ],
        )
        result = run_sqlite_trial(source_db, _config(script_dir), "head")

        assert result.status == STATUS_FAILED
        assert "boom" in result.error
        assert result.error_type == "RuntimeError"
        assert result.end_revision == "t0001"
        assert result.applied_revisions == ("t0001",)
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_cancellation_via_injected_stop_point(self, source_db, good_env):
        calls = []

        def stop_check(next_revision):
            calls.append(next_revision)
            return next_revision == "t0002"

        result = run_sqlite_trial(
            source_db, good_env, "head", stop_check=stop_check
        )

        assert result.status == STATUS_CANCELLED
        assert not result.ok
        assert calls == ["t0001", "t0002"]
        assert result.applied_revisions == ("t0001",)
        assert result.end_revision == "t0001"
        assert "cancelled" in result.error.lower()
        assert not result.workdir.exists()
        _assert_source_untouched(source_db)
        _assert_source_writable(source_db)

    def test_temporary_directory_is_empty_afterwards(
        self, tmp_path, source_db, good_env
    ):
        work_parent = tmp_path / "trialwork"
        result = run_sqlite_trial(
            source_db, good_env, "t0001", tempdir_parent=work_parent
        )

        assert result.status == STATUS_SUCCESS
        assert result.workdir.parent == work_parent
        assert list(work_parent.iterdir()) == []

    def test_env_py_requires_the_injected_connection(self, tmp_path, good_env):
        # running the trial env.py outside the trial runner (no
        # injected connection) must fail loudly instead of connecting
        # somewhere else
        with pytest.raises(RuntimeError, match="trial_connection"):
            command.upgrade(good_env, "t0001")

    def test_missing_source_database_raises(self, tmp_path, good_env):
        with pytest.raises(TrialError, match="does not exist"):
            run_sqlite_trial(tmp_path / "missing.db", good_env, "head")
