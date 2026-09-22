from io import StringIO

import pytest

from alembic import command
from alembic import util
from alembic.config import CommandLine
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import PreviewStep
from alembic.script.revision import Revision
from alembic.script.revision import RevisionError
from alembic.script.revision import RevisionMap
from alembic.testing.fixtures import TestBase


def _branchy_map():
    # a -> b, a -> c, d merges b/c, x is an independent base,
    # e follows d and depends on x
    a = Revision("a", None)
    b = Revision("b", "a")
    c = Revision("c", "a")
    d = Revision("d", ("b", "c"))
    x = Revision("x", None)
    e = Revision("e", "d", dependencies="x")
    return RevisionMap(lambda: (a, b, c, d, x, e))


def _apply_plan_to_state(rmap, current, plan):
    """Apply a preview plan step by step to an abstract revision
    state, checking each step's head deltas, and return the resulting
    heads."""
    state = rmap._preview_closure(
        rmap._resolve_preview_head_set(current, "current")
    )
    heads = rmap._preview_state_heads(set(state))
    for step in plan.steps:
        if step.direction == "upgrade":
            assert step.revision not in state
            state.add(step.revision)
        else:
            assert step.revision in state
            state.discard(step.revision)
        new_heads = rmap._preview_state_heads(state)
        assert step.adds_heads == tuple(
            head for head in new_heads if head not in heads
        )
        assert step.removes_heads == tuple(
            head for head in heads if head not in new_heads
        )
        heads = new_heads
    return heads


class PreviewPlanTest(TestBase):
    def test_merge_applied_once(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions(["b", "c"], ["d"])
        assert plan.steps == (
            PreviewStep(
                revision="d",
                direction="upgrade",
                requires=("b", "c"),
                adds_heads=("d",),
                removes_heads=("b", "c"),
            ),
        )
        assert plan.final_heads == ("d",)
        assert _apply_plan_to_state(rmap, ["b", "c"], plan) == ("d",)

    def test_merge_split_into_two_heads(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions(["d"], ["b", "c"])
        assert plan.steps == (
            PreviewStep(
                revision="d",
                direction="downgrade",
                requires=(),
                adds_heads=("b", "c"),
                removes_heads=("d",),
            ),
        )
        assert plan.final_heads == ("b", "c")
        assert _apply_plan_to_state(rmap, ["d"], plan) == ("b", "c")

    def test_depends_on_dependency_retained(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions(["e"], ["x"])
        assert [(s.revision, s.direction) for s in plan.steps] == [
            ("e", "downgrade"),
            ("d", "downgrade"),
            ("b", "downgrade"),
            ("c", "downgrade"),
            ("a", "downgrade"),
        ]
        # the depended-upon branch is never touched
        assert "x" not in {step.revision for step in plan.steps}
        assert plan.final_heads == ("x",)
        assert _apply_plan_to_state(rmap, ["e"], plan) == ("x",)

    def test_cross_branch_switch_keeps_shared_ancestor(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions(["b"], ["c"])
        assert [(s.revision, s.direction) for s in plan.steps] == [
            ("b", "downgrade"),
            ("c", "upgrade"),
        ]
        # shared ancestor "a" is retained, not rebuilt
        assert "a" not in {step.revision for step in plan.steps}
        assert plan.final_heads == ("c",)
        assert _apply_plan_to_state(rmap, ["b"], plan) == ("c",)

    def test_base_to_head_includes_dependencies(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions(None, "e")
        assert [s.revision for s in plan.steps] == [
            "a",
            "b",
            "c",
            "d",
            "x",
            "e",
        ]
        assert all(s.direction == "upgrade" for s in plan.steps)
        # e requires both its down revision and its dependency
        assert plan.steps[-1].requires == ("d", "x")
        assert plan.final_heads == ("e", "x")
        assert _apply_plan_to_state(rmap, None, plan) == ("e", "x")

    def test_noop_when_sets_match(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions(["d"], ["d"])
        assert plan.steps == ()
        assert plan.final_heads == ("d",)

    def test_downgrade_to_base(self):
        rmap = _branchy_map()
        plan = rmap.preview_revisions("e", "base")
        assert [s.revision for s in plan.steps] == [
            "e",
            "d",
            "b",
            "c",
            "a",
            "x",
        ]
        assert plan.final_heads == ()
        assert _apply_plan_to_state(rmap, "e", plan) == ()


class PreviewValidationTest(TestBase):
    def test_ancestor_within_current_set_rejected(self):
        rmap = _branchy_map()
        with pytest.raises(RevisionError, match="revision 'a' is an ancestor"):
            rmap.preview_revisions(["a", "b"], ["d"])

    def test_ancestor_within_target_set_rejected(self):
        rmap = _branchy_map()
        with pytest.raises(RevisionError, match="Invalid target revision set"):
            rmap.preview_revisions(["b"], ["a", "d"])

    def test_unknown_revision_rejected_with_position(self):
        rmap = _branchy_map()
        with pytest.raises(
            RevisionError,
            match=r"Cannot resolve current revision 'zzzz' \(position 1\)",
        ):
            rmap.preview_revisions(["zzzz"], ["d"])

    def test_unknown_revision_in_target_rejected(self):
        rmap = _branchy_map()
        with pytest.raises(
            RevisionError,
            match=r"Cannot resolve target revision 'qq' \(position 2\)",
        ):
            rmap.preview_revisions(["b"], ["c", "qq"])

    def test_ambiguous_partial_id_rejected(self):
        rmap = RevisionMap(
            lambda: (Revision("abc1", None), Revision("abc2", None))
        )
        with pytest.raises(RevisionError, match="Multiple revisions start"):
            rmap.preview_revisions(["abc"], ["abc1"])

    def test_duplicate_revision_rejected(self):
        rmap = _branchy_map()
        with pytest.raises(RevisionError, match="Duplicate revision 'b'"):
            rmap.preview_revisions(["b", "b"], ["d"])

    def test_dependency_cycle_rejected(self):
        rmap = RevisionMap(lambda: (Revision("p", "q"), Revision("q", "p")))
        with pytest.raises(RevisionError, match="Cycle is detected"):
            rmap.preview_revisions(["p"], ["q"])

    def test_no_partial_plan_on_error(self):
        rmap = _branchy_map()
        # an invalid target set rejects the whole plan; no steps are
        # produced and no partial result is returned
        with pytest.raises(RevisionError):
            rmap.preview_revisions(["b", "c"], ["d", "a"])


def _write_sentinel_script(versions, rev, down, deps=None):
    body = [
        "revision = %r" % rev,
        "down_revision = %r" % (down,),
    ]
    if deps:
        body.append("depends_on = %r" % (deps,))
    body += [
        "",
        "def upgrade():",
        "    raise RuntimeError('sentinel: upgrade() must not run')",
        "",
        "",
        "def downgrade():",
        "    raise RuntimeError('sentinel: downgrade() must not run')",
        "",
    ]
    (versions / ("%s_rev.py" % rev)).write_text(
        "\n".join(body), encoding="utf-8"
    )


@pytest.fixture
def script_dir(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    _write_sentinel_script(versions, "a", None)
    _write_sentinel_script(versions, "b", "a")
    _write_sentinel_script(versions, "c", "a")
    _write_sentinel_script(versions, "d", ("b", "c"))
    _write_sentinel_script(versions, "x", None)
    _write_sentinel_script(versions, "e", "d", deps="x")
    return ScriptDirectory(dir=str(tmp_path))


class ScriptDirectoryPreviewTest(TestBase):
    def test_preview_does_not_execute_scripts(self, script_dir):
        # the upgrade()/downgrade() bodies of every script raise if
        # called; a successful preview proves they were not executed
        plan = script_dir.preview_revisions("b,c", "d")
        assert [s.revision for s in plan.steps] == ["d"]
        assert plan.final_heads == ("d",)

    def test_preview_split_via_script_directory(self, script_dir):
        plan = script_dir.preview_revisions("d", "b,c")
        assert [(s.revision, s.direction) for s in plan.steps] == [
            ("d", "downgrade")
        ]
        assert plan.final_heads == ("b", "c")

    def test_preview_rejects_invalid_head_set(self, script_dir):
        with pytest.raises(util.CommandError, match="ancestor"):
            script_dir.preview_revisions("a,b", "d")


class CommandPreviewTest(TestBase):
    def _config(self, script_dir):
        cfg = Config()
        cfg.set_main_option("script_location", script_dir.dir)
        cfg.stdout = StringIO()
        return cfg

    def test_command_prints_merge_plan(self, script_dir):
        cfg = self._config(script_dir)
        command.preview(cfg, "b,c", "d")
        output = cfg.stdout.getvalue()
        assert "Current heads: b, c" in output
        assert "Target heads: d" in output
        assert "Step 1: upgrade d" in output
        assert "requires: b, c" in output
        assert "removes head(s): b, c" in output
        assert "adds head(s): d" in output
        assert "Final heads: d" in output

    def test_command_prints_split_plan(self, script_dir):
        cfg = self._config(script_dir)
        command.preview(cfg, "d", "b,c")
        output = cfg.stdout.getvalue()
        assert "Step 1: downgrade d" in output
        assert "removes head(s): d" in output
        assert "adds head(s): b, c" in output
        assert "Final heads: b, c" in output

    def test_command_rejection_diagnostic(self, script_dir):
        cfg = self._config(script_dir)
        with pytest.raises(
            util.CommandError, match="revision 'a' is an ancestor"
        ):
            command.preview(cfg, "a,b", "d")

    def test_command_line_registration(self):
        opts = CommandLine().parser.parse_args(["preview", "b,c", "d"])
        fn, positional, kwarg = opts.cmd
        assert fn is command.preview
        assert positional == ["current", "target"]
