import json
from pathlib import Path
import sys

import pytest

from alembic import command
from alembic.config import Config
from alembic.script import lockfile
from alembic.script import ScriptDirectory
from alembic.testing.fixtures import TestBase
from alembic.util import CommandError


def _fmt(value):
    if value is None:
        return "None"
    if isinstance(value, str):
        return repr(value)
    return repr(tuple(value))


def _write_script(
    versions,
    name,
    revision,
    down_revision=None,
    depends_on=None,
    branch_labels=None,
    extra="",
    newline="\n",
):
    text = (
        f'"""revision {revision}"""\n'
        f"revision = {_fmt(revision)}\n"
        f"down_revision = {_fmt(down_revision)}\n"
        f"branch_labels = {_fmt(branch_labels)}\n"
        f"depends_on = {_fmt(depends_on)}\n"
    ) + extra
    if newline != "\n":
        text = text.replace("\n", newline)
    (versions / name).write_bytes(text.encode("utf-8"))


def _branch_merge_scripts(versions):
    """a base, two branches, and a merge revision."""
    _write_script(versions, "a1_base.py", "a1")
    _write_script(
        versions,
        "b1_branch.py",
        "b1",
        down_revision="a1",
        branch_labels=("feature_b",),
    )
    _write_script(versions, "c1_branch.py", "c1", down_revision="a1")
    _write_script(versions, "d1_merge.py", "d1", down_revision=("b1", "c1"))


@pytest.fixture
def scriptdir(tmp_path):
    versions = tmp_path / "scripts" / "versions"
    versions.mkdir(parents=True)
    return ScriptDirectory(dir=str(tmp_path / "scripts"))


@pytest.fixture
def versions(scriptdir):
    return Path(scriptdir.dir) / "versions"


@pytest.fixture
def locked(scriptdir, versions):
    """a script directory with branch+merge scripts and its manifest"""
    _branch_merge_scripts(versions)
    return lockfile.generate_manifest(scriptdir)


def _kinds(result):
    return {diff["revision"]: diff["kind"] for diff in result["differences"]}


def _config(scriptdir, stdout=None):
    cfg = Config(stdout=stdout or sys.stdout)
    cfg.set_main_option("script_location", scriptdir.dir)
    return cfg


class TestGenerate(TestBase):
    def test_manifest_sorted_relative_no_abspaths(
        self, scriptdir, versions, locked, tmp_path
    ):
        assert locked["format_version"] == lockfile.FORMAT_VERSION
        revisions = [r["revision"] for r in locked["revisions"]]
        assert revisions == sorted(revisions) == ["a1", "b1", "c1", "d1"]
        by_rev = {r["revision"]: r for r in locked["revisions"]}
        assert by_rev["a1"]["down_revision"] is None
        assert by_rev["d1"]["down_revision"] == ["b1", "c1"]
        assert by_rev["b1"]["branch_labels"] == ["feature_b"]
        assert all(len(r["sha256"]) == 64 for r in locked["revisions"])
        # no absolute paths of the generating machine in the manifest
        text = json.dumps(locked, ensure_ascii=False)
        assert str(tmp_path) not in text
        for r in locked["revisions"]:
            assert not Path(r["file"]).is_absolute()

    def test_roundtrip_check_ok(self, scriptdir, locked):
        result = lockfile.check_manifest(scriptdir, locked)
        assert result["status"] == "ok"
        assert result["errors"] == []
        assert result["differences"] == []

    def test_depends_on_recorded(self, scriptdir, versions):
        _write_script(versions, "a1.py", "a1")
        _write_script(
            versions, "b1.py", "b1", down_revision="a1", depends_on=("x9",)
        )
        _write_script(versions, "x9.py", "x9")
        manifest = lockfile.generate_manifest(scriptdir)
        by_rev = {r["revision"]: r for r in manifest["revisions"]}
        assert by_rev["b1"]["depends_on"] == ["x9"]
        result = lockfile.check_manifest(scriptdir, manifest)
        assert result["status"] == "ok"

    def test_stable_ordering_regardless_of_filename(self, scriptdir, versions):
        _write_script(versions, "zzz_first_file.py", "m2", down_revision="m1")
        _write_script(versions, "aaa_second_file.py", "m1")
        manifest = lockfile.generate_manifest(scriptdir)
        assert [r["revision"] for r in manifest["revisions"]] == ["m1", "m2"]


class TestCheckDifferences(TestBase):
    def test_comment_change_is_content_modified(
        self, scriptdir, versions, locked
    ):
        path = versions / "a1_base.py"
        path.write_bytes(path.read_bytes() + b"# just a comment\n")
        result = lockfile.check_manifest(scriptdir, locked)
        assert result["status"] == "failed"
        assert _kinds(result) == {"a1": "content_modified"}
        assert result["summary"]["content_modified"] == 1

    def test_relation_change_is_relations_modified(
        self, scriptdir, versions, locked
    ):
        _write_script(versions, "c1_branch.py", "c1", down_revision="b1")
        result = lockfile.check_manifest(scriptdir, locked)
        assert _kinds(result) == {"c1": "relations_modified"}
        assert result["summary"]["relations_modified"] == 1

    def test_missing_parent_reports_dangling_and_missing(
        self, scriptdir, versions, locked
    ):
        (versions / "a1_base.py").unlink()
        result = lockfile.check_manifest(scriptdir, locked)
        assert result["status"] == "failed"
        assert _kinds(result) == {"a1": "missing"}
        dangling = [e for e in result["errors"] if "dangling" in e["reason"]]
        # both b1 and c1 refer to the deleted a1
        assert len(dangling) == 2
        assert all(e["file"] for e in dangling)
        assert any("'a1'" in e["reason"] for e in dangling)

    def test_extra_script_rejected(self, scriptdir, versions, locked):
        _write_script(versions, "e1_new.py", "e1", down_revision="d1")
        result = lockfile.check_manifest(scriptdir, locked)
        assert result["status"] == "failed"
        assert _kinds(result) == {"e1": "extra"}
        assert result["summary"]["extra"] == 1

    def test_extra_successor_allowed_explicitly(
        self, scriptdir, versions, locked
    ):
        _write_script(versions, "e1_new.py", "e1", down_revision="d1")
        _write_script(versions, "e2_new.py", "e2", down_revision="e1")
        result = lockfile.check_manifest(
            scriptdir, locked, allow_new_successors=True
        )
        assert result["status"] == "ok"
        assert result["summary"]["extra_allowed"] == 2
        assert all(diff["allowed"] for diff in result["differences"])

    def test_allowed_successors_still_reject_modification(
        self, scriptdir, versions, locked
    ):
        # a new successor is allowed, but tampering with a locked
        # revision is still a hard failure
        _write_script(versions, "e1_new.py", "e1", down_revision="d1")
        path = versions / "a1_base.py"
        path.write_bytes(path.read_bytes() + b"# tampered\n")
        result = lockfile.check_manifest(
            scriptdir, locked, allow_new_successors=True
        )
        assert result["status"] == "failed"
        assert _kinds(result)["a1"] == "content_modified"
        assert _kinds(result)["e1"] == "extra"


class TestStructuralProblems(TestBase):
    def test_dynamic_metadata_not_executed(
        self, scriptdir, versions, tmp_path
    ):
        sentinel = tmp_path / "SENTINEL_RAN"
        _write_script(versions, "a1.py", "a1")
        extra = (
            "import pathlib\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('boom')\n"
            "revision = 'z9_' + 'dynamic'\n"
        )
        # rewrite with a dynamic (non-literal) revision assignment
        _write_script(versions, "z9_dyn.py", "z9", down_revision="a1")
        path = versions / "z9_dyn.py"
        text = path.read_text()
        text = text.replace("revision = 'z9'\n", "")
        path.write_text(text + extra)

        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.generate_manifest(scriptdir)
        assert "z9_dyn.py" in str(err.value)
        assert "not a literal" in str(err.value)
        # the module was parsed, never executed: no side effects
        assert not sentinel.exists()

    def test_duplicate_revision_fails(self, scriptdir, versions):
        _write_script(versions, "a1.py", "a1")
        _write_script(versions, "a1_copy.py", "a1")
        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.generate_manifest(scriptdir)
        assert "duplicate revision id 'a1'" in str(err.value)
        assert "a1_copy.py" in str(err.value)

    def test_cycle_fails(self, scriptdir, versions):
        _write_script(versions, "a1.py", "a1", down_revision="c1")
        _write_script(versions, "b1.py", "b1", down_revision="a1")
        _write_script(versions, "c1.py", "c1", down_revision="b1")
        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.generate_manifest(scriptdir)
        assert "dependency cycle detected" in str(err.value)

    def test_dangling_dependency_fails_generate(self, scriptdir, versions):
        _write_script(versions, "b1.py", "b1", down_revision="missing1")
        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.generate_manifest(scriptdir)
        assert "dangling dependency" in str(err.value)
        assert "b1.py" in str(err.value)

    def test_script_without_revision_fails(self, scriptdir, versions):
        (versions / "not_a_revision.py").write_text("x = 1\n")
        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.generate_manifest(scriptdir)
        assert "not_a_revision.py" in str(err.value)


class TestEncodingsAndPaths(TestBase):
    def test_crlf_vs_lf_is_content_modified(self, scriptdir, versions):
        _write_script(versions, "a1.py", "a1", newline="\n")
        manifest = lockfile.generate_manifest(scriptdir)
        assert lockfile.check_manifest(scriptdir, manifest)["status"] == "ok"
        # rewrite the identical source with CRLF line endings
        _write_script(versions, "a1.py", "a1", newline="\r\n")
        result = lockfile.check_manifest(scriptdir, manifest)
        assert _kinds(result) == {"a1": "content_modified"}

    def test_unicode_paths(self, tmp_path):
        versions = tmp_path / "迁移脚本" / "versions"
        versions.mkdir(parents=True)
        scriptdir = ScriptDirectory(dir=str(tmp_path / "迁移脚本"))
        _write_script(versions, "a1_初始.py", "a1")
        _write_script(versions, "b1_第二版.py", "b1", down_revision="a1")
        manifest = lockfile.generate_manifest(scriptdir)
        by_rev = {r["revision"]: r for r in manifest["revisions"]}
        assert by_rev["a1"]["file"] == "a1_初始.py"
        assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)
        assert lockfile.check_manifest(scriptdir, manifest)["status"] == "ok"


class TestManifestFile(TestBase):
    def test_unknown_format_version_rejected(
        self, scriptdir, locked, tmp_path
    ):
        locked["format_version"] = 999
        path = tmp_path / "future.lock.json"
        lockfile.write_manifest(locked, path)
        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.load_manifest(path)
        assert "unsupported lockfile format version" in str(err.value)

    def test_not_a_lockfile_rejected(self, tmp_path):
        path = tmp_path / "random.json"
        path.write_text('{"hello": "world"}')
        with pytest.raises(lockfile.LockfileError) as err:
            lockfile.load_manifest(path)
        assert "not an Alembic lockfile" in str(err.value)

    def test_write_then_load_roundtrip(self, scriptdir, locked, tmp_path):
        path = tmp_path / "alembic.lock.json"
        lockfile.write_manifest(locked, path)
        loaded = lockfile.load_manifest(path)
        assert loaded == locked
        result = lockfile.check_manifest(scriptdir, loaded)
        assert result["status"] == "ok"


class TestCommands(TestBase):
    def test_lock_generate_and_check(self, scriptdir, versions):
        _branch_merge_scripts(versions)
        cfg = _config(scriptdir)
        command.lock_generate(cfg)
        lockfile_path = Path(scriptdir.dir) / lockfile.LOCKFILE_BASENAME
        assert lockfile_path.exists()
        # unchanged tree verifies cleanly
        command.lock_check(cfg)
        # tamper with a comment: verification fails
        path = versions / "a1_base.py"
        path.write_bytes(path.read_bytes() + b"# tampered\n")
        with pytest.raises(CommandError):
            command.lock_check(cfg)

    def test_lock_generate_custom_output(self, scriptdir, versions, tmp_path):
        _branch_merge_scripts(versions)
        cfg = _config(scriptdir)
        out = str(tmp_path / "custom.lock")
        command.lock_generate(cfg, output=out)
        assert Path(out).exists()
        command.lock_check(cfg, lockfile_path=out)

    def test_lock_check_json_output(self, scriptdir, versions, capsys):
        _branch_merge_scripts(versions)
        cfg = _config(scriptdir, stdout=sys.stdout)
        command.lock_generate(cfg)
        capsys.readouterr()
        command.lock_check(cfg, json=True)
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "ok"
        # tamper: JSON report says failed, exit code is non-zero
        path = versions / "a1_base.py"
        path.write_bytes(path.read_bytes() + b"# tampered\n")
        with pytest.raises(SystemExit):
            command.lock_check(cfg, json=True)
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "failed"
        assert report["summary"]["content_modified"] == 1
        kinds = {d["revision"]: d["kind"] for d in report["differences"]}
        assert kinds == {"a1": "content_modified"}

    def test_lock_check_unknown_version_command(self, scriptdir, versions):
        _branch_merge_scripts(versions)
        cfg = _config(scriptdir)
        command.lock_generate(cfg)
        path = Path(scriptdir.dir) / lockfile.LOCKFILE_BASENAME
        data = json.loads(path.read_text())
        data["format_version"] = 42
        path.write_text(json.dumps(data))
        with pytest.raises(CommandError):
            command.lock_check(cfg)

    def test_lock_check_json_bad_lockfile(self, scriptdir, versions, capsys):
        _branch_merge_scripts(versions)
        cfg = _config(scriptdir, stdout=sys.stdout)
        command.lock_generate(cfg)
        path = Path(scriptdir.dir) / lockfile.LOCKFILE_BASENAME
        data = json.loads(path.read_text())
        data["format_version"] = 42
        path.write_text(json.dumps(data))
        capsys.readouterr()
        with pytest.raises(SystemExit):
            command.lock_check(cfg, json=True)
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "failed"
        assert (
            "unsupported lockfile format version"
            in report["errors"][0]["reason"]
        )
