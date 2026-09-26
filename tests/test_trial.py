import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

import alembic
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from alembic.testing.fixtures import TestBase
from alembic.trial import run_sqlite_trial
from alembic.trial import TRIAL_AFTER_BACKUP
from alembic.trial import TRIAL_AFTER_FAILURE
from alembic.trial import TRIAL_AFTER_MIGRATE
from alembic.trial import TrialCancelled

TRIAL_TEMPLATE_ENV = (
    Path(alembic.__file__).parent / "templates" / "trial" / "env.py"
)

CREATE_ITEM = (
    "CREATE TABLE item ("
    "id INTEGER PRIMARY KEY, "
    "name VARCHAR(50) NOT NULL UNIQUE)"
)

REV_ADD_COLUMN = '''
"""add note column"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade():
    op.add_column("item", sa.Column("note", sa.String(length=50)))


def downgrade():
    op.drop_column("item", "note")
'''

REV_BAD_INSERT = '''
"""insert a row that violates the NOT NULL constraint"""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("INSERT INTO item (name) VALUES (NULL)")


def downgrade():
    pass
'''

REV_ATTACH = '''
"""try to attach another database"""

from alembic import op

revision = "a001"
down_revision = None


def upgrade():
    op.execute("ATTACH DATABASE ':memory:' AS other")


def downgrade():
    pass
'''

REV_LOAD_EXTENSION = '''
"""try to load a sqlite extension"""

from alembic import op

revision = "e001"
down_revision = None


def upgrade():
    op.execute("SELECT load_extension('/no/such/extension')")


def downgrade():
    pass
'''

REV_RAISE = '''
"""migration that blows up with a plain Python error"""

revision = "b001"
down_revision = None


def upgrade():
    raise RuntimeError("boom")


def downgrade():
    pass
'''


def _make_script_dir(base, revisions):
    script_dir = base / "scripts"
    versions = script_dir / "versions"
    versions.mkdir(parents=True)
    shutil.copy(TRIAL_TEMPLATE_ENV, script_dir / "env.py")
    for filename, body in revisions.items():
        (versions / filename).write_text(body)
    return script_dir


def _make_config(script_dir):
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    return cfg


def _snapshot(db_path):
    """Capture the schema and full table contents of a database."""
    conn = sqlite3.connect(str(db_path))
    try:
        schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        data = {}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            data[name] = conn.execute('SELECT * FROM "%s"' % name).fetchall()
        return schema, data
    finally:
        conn.close()


def _assert_source_writable(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("INSERT INTO item (name) VALUES ('post_trial')")
        conn.commit()
        assert conn.execute(
            "SELECT name FROM item WHERE name='post_trial'"
        ).fetchone() == ("post_trial",)
        conn.execute("DELETE FROM item WHERE name='post_trial'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def trial_tmp(tmp_path):
    work = tmp_path / "trial_work"
    work.mkdir()
    yield work
    assert not any(
        work.iterdir()
    ), "temporary trial directory was not cleaned up"


@pytest.fixture()
def script_dir(tmp_path):
    return _make_script_dir(
        tmp_path,
        {
            "0001_add_note_column.py": REV_ADD_COLUMN,
            "0002_bad_insert.py": REV_BAD_INSERT,
        },
    )


@pytest.fixture()
def source_db(tmp_path):
    path = tmp_path / "source.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(CREATE_ITEM)
        conn.execute("INSERT INTO item (name) VALUES ('alpha')")
        conn.commit()
    finally:
        conn.close()
    return path


class TrialRunTest(TestBase):
    def test_successful_trial_leaves_source_untouched(
        self, script_dir, source_db, trial_tmp
    ):
        before = _snapshot(source_db)
        events = []
        result = run_sqlite_trial(
            source_db,
            _make_config(script_dir),
            "0001",
            temporary_directory=trial_tmp,
            hook=lambda event, info: events.append(event),
        )
        assert result.success
        assert result.starting_revisions == ()
        assert result.ending_revisions == ("0001",)
        assert result.error is None
        assert "SUCCESS" in str(result)
        assert events == [TRIAL_AFTER_BACKUP, TRIAL_AFTER_MIGRATE]
        # the source schema and data are unchanged
        assert _snapshot(source_db) == before

    def test_failed_trial_leaves_source_untouched(
        self, script_dir, source_db, trial_tmp
    ):
        before = _snapshot(source_db)
        failure_info = {}

        def hook(event, info):
            if event == TRIAL_AFTER_FAILURE:
                conn = sqlite3.connect(str(info["copy_path"]))
                try:
                    tables = [
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table'"
                        )
                    ]
                    versions = []
                    if "alembic_version" in tables:
                        versions = [
                            row[0]
                            for row in conn.execute(
                                "SELECT version_num FROM alembic_version"
                            )
                        ]
                    failure_info["versions"] = versions
                finally:
                    conn.close()

        result = run_sqlite_trial(
            source_db,
            _make_config(script_dir),
            "head",
            temporary_directory=trial_tmp,
            hook=hook,
        )
        assert not result.success
        assert result.starting_revisions == ()
        # a failed run is never reported as a completed migration
        assert result.ending_revisions is None
        assert "constraint" in result.error.lower()
        assert "Traceback" in result.error_traceback
        assert "FAILED" in str(result)
        # the failed migration was not recorded as applied on the copy
        assert failure_info["versions"] == []
        # the source schema and data are unchanged
        assert _snapshot(source_db) == before

    def test_wal_uncheckpointed_data_is_copied(
        self, script_dir, tmp_path, trial_tmp
    ):
        path = tmp_path / "wal_source.db"
        keeper = sqlite3.connect(str(path))
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(CREATE_ITEM)
        keeper.execute("INSERT INTO item (name) VALUES ('alpha')")
        keeper.commit()
        keeper.execute("INSERT INTO item (name) VALUES ('beta')")
        keeper.commit()

        wal_path = Path(str(path) + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0
        # 'beta' lives only in the WAL file, not in the main db file
        assert b"beta" not in path.read_bytes()

        copied = {}

        def hook(event, info):
            if event == TRIAL_AFTER_BACKUP:
                conn = sqlite3.connect(str(info["copy_path"]))
                try:
                    copied["names"] = [
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM item ORDER BY name"
                        )
                    ]
                finally:
                    conn.close()

        try:
            result = run_sqlite_trial(
                path,
                _make_config(script_dir),
                "0001",
                temporary_directory=trial_tmp,
                hook=hook,
            )
        finally:
            keeper.close()
        assert result.success
        # the backup API picked up committed data still in the WAL
        assert copied["names"] == ["alpha", "beta"]

    def test_source_with_active_read_connection(
        self, script_dir, source_db, trial_tmp
    ):
        before = _snapshot(source_db)
        reader = sqlite3.connect(str(source_db), isolation_level=None)
        reader.execute("BEGIN")
        try:
            assert reader.execute("SELECT name FROM item").fetchall() == [
                ("alpha",)
            ]
            result = run_sqlite_trial(
                source_db,
                _make_config(script_dir),
                "0001",
                temporary_directory=trial_tmp,
            )
        finally:
            reader.execute("COMMIT")
            reader.close()
        assert result.success
        assert _snapshot(source_db) == before

    def test_attach_is_denied(self, source_db, tmp_path, trial_tmp):
        before = _snapshot(source_db)
        script_dir = _make_script_dir(tmp_path, {"a001_attach.py": REV_ATTACH})
        result = run_sqlite_trial(
            source_db,
            _make_config(script_dir),
            "head",
            temporary_directory=trial_tmp,
        )
        assert not result.success
        assert "not authorized" in result.error.lower()
        assert _snapshot(source_db) == before

    def test_load_extension_is_denied(self, source_db, tmp_path, trial_tmp):
        before = _snapshot(source_db)
        script_dir = _make_script_dir(
            tmp_path, {"e001_load_extension.py": REV_LOAD_EXTENSION}
        )
        result = run_sqlite_trial(
            source_db,
            _make_config(script_dir),
            "head",
            temporary_directory=trial_tmp,
        )
        assert not result.success
        assert "not authorized" in result.error.lower()
        assert _snapshot(source_db) == before

    def test_revision_not_found(self, script_dir, source_db, trial_tmp):
        before = _snapshot(source_db)
        result = run_sqlite_trial(
            source_db,
            _make_config(script_dir),
            "no-such-revision",
            temporary_directory=trial_tmp,
        )
        assert not result.success
        assert "no-such-revision" in result.error
        assert _snapshot(source_db) == before
        _assert_source_writable(source_db)

    def test_migration_exception_source_still_usable(
        self, source_db, tmp_path, trial_tmp
    ):
        before = _snapshot(source_db)
        script_dir = _make_script_dir(tmp_path, {"b001_boom.py": REV_RAISE})
        result = run_sqlite_trial(
            source_db,
            _make_config(script_dir),
            "head",
            temporary_directory=trial_tmp,
        )
        assert not result.success
        assert "boom" in result.error
        assert _snapshot(source_db) == before
        _assert_source_writable(source_db)

    @pytest.mark.parametrize(
        "cancel_event", [TRIAL_AFTER_BACKUP, TRIAL_AFTER_MIGRATE]
    )
    def test_cancellation_via_hook_cleans_up(
        self, script_dir, source_db, trial_tmp, cancel_event
    ):
        before = _snapshot(source_db)

        def hook(event, info):
            if event == cancel_event:
                raise TrialCancelled("stopped at %s" % event)

        with pytest.raises(TrialCancelled):
            run_sqlite_trial(
                source_db,
                _make_config(script_dir),
                "0001",
                temporary_directory=trial_tmp,
                hook=hook,
            )
        assert _snapshot(source_db) == before
        _assert_source_writable(source_db)

    def test_default_temporary_directory_cleanup(self, script_dir, source_db):
        system_tmp = Path(tempfile.gettempdir())
        before = set(system_tmp.glob("alembic_trial_*"))
        result = run_sqlite_trial(source_db, _make_config(script_dir), "0001")
        assert result.success
        assert set(system_tmp.glob("alembic_trial_*")) == before

    def test_env_template_requires_supplied_connection(self, script_dir):
        cfg = _make_config(script_dir)
        script = ScriptDirectory.from_config(cfg)
        with EnvironmentContext(cfg, script, fn=lambda rev, context: []):
            with pytest.raises(RuntimeError, match="config.attributes"):
                script.run_env()

    def test_missing_source_database(self, script_dir, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_sqlite_trial(
                tmp_path / "does_not_exist.db",
                _make_config(script_dir),
                "head",
            )
