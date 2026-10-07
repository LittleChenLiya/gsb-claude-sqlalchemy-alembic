import io
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import manifest as manifest_mod
from alembic.script import ScriptDirectory
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import is_false
from alembic.testing import is_true
from alembic.testing.fixtures import TestBase
from alembic.util import CommandError


def _script_body(
    rev,
    down=None,
    depends=None,
    labels=None,
    comment="a migration",
    newline="\n",
):
    def fmt(value):
        if value is None:
            return "None"
        if isinstance(value, tuple):
            inner = ", ".join(repr(v) for v in value)
            if len(value) == 1:
                inner += ","
            return "(" + inner + ")"
        return repr(value)

    lines = [
        '"""%s"""' % comment,
        "",
        "# comment line",
        "revision = %r" % rev,
        "down_revision = %s" % fmt(down),
        "depends_on = %s" % fmt(depends),
        "branch_labels = %s" % fmt(labels),
        "",
        "",
        "def upgrade():",
        "    pass",
        "",
        "",
        "def downgrade():",
        "    pass",
        "",
    ]
    return newline.join(lines)


def _write(versions, name, body):
    path = versions / name
    path.write_bytes(body.encode("utf-8"))
    return path


def _branch_merge_fixture(versions):
    """a -> b, a -> c (branch), b + c -> m (merge)"""
    _write(versions, "a_one.py", _script_body("a"))
    _write(versions, "b_two.py", _script_body("b", down="a", labels="billing"))
    _write(versions, "c_three.py", _script_body("c", down="a"))
    _write(
        versions,
        "m_merge.py",
        _script_body("m", down=("b", "c"), depends=("a",)),
    )


def _script_dir(tmp_path, versions_name="versions"):
    script_root = tmp_path / "scripts"
    versions = script_root / versions_name
    versions.mkdir(parents=True)
    if versions_name == "versions":
        return ScriptDirectory(str(script_root)), versions
    return (
        ScriptDirectory(str(script_root), version_locations=[str(versions)]),
        versions,
    )


class GenerateAndCheckTest(TestBase):
    def test_roundtrip_branch_merge(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)

        manifest = manifest_mod.generate_manifest(script_dir)
        eq_(manifest["format"], manifest_mod.FORMAT_NAME)
        eq_(manifest["version"], manifest_mod.FORMAT_VERSION)
        eq_(
            [r["revision"] for r in manifest["revisions"]],
            ["a", "b", "c", "m"],
        )
        merge = manifest["revisions"][3]
        eq_(merge["down_revision"], ["b", "c"])
        eq_(merge["depends_on"], ["a"])
        eq_(manifest["revisions"][1]["branch_labels"], ["billing"])

        dest = tmp_path / "manifest.json"
        manifest_mod.write_manifest(manifest, str(dest))

        loaded = manifest_mod.read_manifest(str(dest))
        entries, errors = manifest_mod.scan_revisions(script_dir)
        eq_(errors, [])
        report = manifest_mod.check_manifest(loaded, entries, errors)
        is_true(report["ok"], msg=json.dumps(report, indent=2))

    def test_no_absolute_paths_in_manifest(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        dest = tmp_path / "manifest.json"
        manifest_mod.write_manifest(
            manifest_mod.generate_manifest(script_dir), str(dest)
        )
        text = dest.read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        assert "scripts" not in text
        for entry in json.loads(text)["revisions"]:
            assert not Path(entry["path"]).is_absolute()

    def test_comment_change_is_content_modified(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        # change only a comment in one script
        target = versions / "c_three.py"
        target.write_bytes(
            target.read_bytes().replace(
                b"# comment line", b"# comment line, edited"
            )
        )

        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(manifest, entries, errors)
        is_false(report["ok"])
        eq_([i["revision"] for i in report["content_modified"]], ["c"])
        eq_(report["relationship_modified"], [])

    def test_relationship_change_detected(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        target = versions / "b_two.py"
        target.write_bytes(
            target.read_bytes().replace(
                b"down_revision = 'a'", b"down_revision = 'c'"
            )
        )

        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(manifest, entries, errors)
        is_false(report["ok"])
        eq_([i["revision"] for i in report["relationship_modified"]], ["b"])
        eq_(
            report["relationship_modified"][0]["fields"]["down_revision"],
            {"manifest": ["a"], "current": ["c"]},
        )
        eq_(report["content_modified"], [])

    def test_missing_revision_detected(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        (versions / "c_three.py").unlink()
        # fix the merge so the graph itself is consistent
        target = versions / "m_merge.py"
        target.write_bytes(
            target.read_bytes().replace(
                b"down_revision = ('b', 'c')", b"down_revision = 'b'"
            )
        )

        entries, errors = manifest_mod.scan_revisions(script_dir)
        eq_(errors, [])
        report = manifest_mod.check_manifest(manifest, entries, errors)
        is_false(report["ok"])
        eq_([i["revision"] for i in report["missing"]], ["c"])

    def test_delete_parent_shows_dangling_dependency(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        (versions / "a_one.py").unlink()

        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(manifest, entries, errors)
        is_false(report["ok"])
        eq_([i["revision"] for i in report["missing"]], ["a"])
        dangling = [e for e in report["errors"] if "dangling" in e]
        assert dangling, report["errors"]
        assert any("'a'" in e for e in dangling)

    def test_extra_revision_rejected_by_default(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        _write(versions, "n_new.py", _script_body("n", down="m"))

        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(manifest, entries, errors)
        is_false(report["ok"])
        eq_([i["revision"] for i in report["extra"]], ["n"])

    def test_extra_successor_allowed_explicitly(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        _write(versions, "n_new.py", _script_body("n", down="m"))
        _write(versions, "o_newer.py", _script_body("o", down="n"))

        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(
            manifest, entries, errors, allow_new_successors=True
        )
        is_true(report["ok"], msg=json.dumps(report, indent=2))
        eq_([i["revision"] for i in report["added"]], ["n", "o"])

    def test_extra_non_successor_still_rejected(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        manifest = manifest_mod.generate_manifest(script_dir)

        # 'x' depends on a revision unknown to the manifest
        _write(versions, "x_new.py", _script_body("x", down="zzz"))

        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(
            manifest, entries, errors, allow_new_successors=True
        )
        is_false(report["ok"])
        eq_([i["revision"] for i in report["extra"]], ["x"])
        assert any("dangling" in e for e in report["errors"])


class StructuralErrorTest(TestBase):
    def test_dynamic_metadata_fails_and_executes_nothing(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _write(versions, "a_one.py", _script_body("a"))
        sentinel = tmp_path / "sentinel.txt"
        body = (
            "from pathlib import Path\n"
            "Path(%r).write_text('side effect')\n"
            "_rev = 'b'\n"
            "revision = _rev\n"
            "down_revision = 'a'\n" % str(sentinel)
        )
        _write(versions, "b_dyn.py", body)

        assert_raises_message(
            manifest_mod.ManifestError,
            "dynamic metadata",
            manifest_mod.generate_manifest,
            script_dir,
        )
        # the module was never executed: no sentinel side effect
        assert not sentinel.exists()

        # the error names the offending file
        entries, errors = manifest_mod.scan_revisions(script_dir)
        eq_(len(errors), 1)
        assert "b_dyn.py" in str(errors[0])

    def test_duplicate_revision_fails(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _write(versions, "a_one.py", _script_body("a"))
        _write(versions, "a_two.py", _script_body("a"))

        assert_raises_message(
            manifest_mod.ManifestError,
            "duplicate revision id 'a'",
            manifest_mod.generate_manifest,
            script_dir,
        )

    def test_dangling_dependency_fails_generation(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _write(versions, "b_two.py", _script_body("b", down="a"))

        assert_raises_message(
            manifest_mod.ManifestError,
            "dangling dependency",
            manifest_mod.generate_manifest,
            script_dir,
        )

    def test_cycle_fails_generation(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _write(versions, "a_one.py", _script_body("a", down="b"))
        _write(versions, "b_two.py", _script_body("b", down="a"))

        assert_raises_message(
            manifest_mod.ManifestError,
            "dependency cycle detected",
            manifest_mod.generate_manifest,
            script_dir,
        )

    def test_unknown_format_version_rejected(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _write(versions, "a_one.py", _script_body("a"))
        manifest = manifest_mod.generate_manifest(script_dir)
        manifest["version"] = 999
        dest = tmp_path / "manifest.json"
        manifest_mod.write_manifest(manifest, str(dest))

        assert_raises_message(
            manifest_mod.ManifestError,
            "unsupported manifest format version 999",
            manifest_mod.read_manifest,
            str(dest),
        )

    def test_not_a_manifest_rejected(self, tmp_path):
        dest = tmp_path / "other.json"
        dest.write_text('{"format": "something-else", "version": 1}')
        assert_raises_message(
            manifest_mod.ManifestError,
            "not an Alembic migration manifest",
            manifest_mod.read_manifest,
            str(dest),
        )


class EncodingAndPathTest(TestBase):
    def test_crlf_lf_difference_detected(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _write(versions, "a_one.py", _script_body("a", newline="\n"))
        manifest = manifest_mod.generate_manifest(script_dir)

        # identical content, but CRLF line endings
        path = versions / "a_one.py"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

        entries, errors = manifest_mod.scan_revisions(script_dir)
        eq_(errors, [])
        report = manifest_mod.check_manifest(manifest, entries, errors)
        is_false(report["ok"])
        eq_([i["revision"] for i in report["content_modified"]], ["a"])

    def test_unicode_path(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path, versions_name="版本")
        _write(versions, "a_第一版.py", _script_body("a"))
        _write(versions, "b_two.py", _script_body("b", down="a"))

        manifest = manifest_mod.generate_manifest(script_dir)
        eq_([r["revision"] for r in manifest["revisions"]], ["a", "b"])
        eq_(manifest["revisions"][0]["path"], "a_第一版.py")

        dest = tmp_path / "清单.json"
        manifest_mod.write_manifest(manifest, str(dest))
        loaded = manifest_mod.read_manifest(str(dest))
        entries, errors = manifest_mod.scan_revisions(script_dir)
        report = manifest_mod.check_manifest(loaded, entries, errors)
        is_true(report["ok"], msg=json.dumps(report, indent=2))


class CommandTest(TestBase):
    def _config(self, tmp_path):
        script_dir, versions = _script_dir(tmp_path)
        _branch_merge_fixture(versions)
        cfg = Config()
        cfg.set_main_option("script_location", str(tmp_path / "scripts"))
        return cfg, versions

    def test_command_roundtrip(self, tmp_path):
        cfg, versions = self._config(tmp_path)
        manifest_path = str(tmp_path / "manifest.json")
        command.manifest_generate(cfg, output=manifest_path)
        assert Path(manifest_path).exists()
        # unchanged: passes
        command.manifest_check(cfg, manifest=manifest_path)

    def test_command_check_failure_raises(self, tmp_path):
        cfg, versions = self._config(tmp_path)
        manifest_path = str(tmp_path / "manifest.json")
        command.manifest_generate(cfg, output=manifest_path)

        target = versions / "c_three.py"
        target.write_bytes(target.read_bytes() + b"# tampered\n")

        assert_raises_message(
            CommandError,
            "manifest check failed",
            command.manifest_check,
            cfg,
            manifest=manifest_path,
        )

    def test_command_json_output(self, tmp_path):
        cfg, versions = self._config(tmp_path)
        manifest_path = str(tmp_path / "manifest.json")
        command.manifest_generate(cfg, output=manifest_path)

        target = versions / "c_three.py"
        target.write_bytes(target.read_bytes() + b"# tampered\n")

        buf = io.StringIO()
        cfg.stdout = buf
        assert_raises_message(
            CommandError,
            "manifest check failed",
            command.manifest_check,
            cfg,
            manifest=manifest_path,
            json_output=True,
        )
        report = json.loads(buf.getvalue())
        is_false(report["ok"])
        eq_([i["revision"] for i in report["content_modified"]], ["c"])
