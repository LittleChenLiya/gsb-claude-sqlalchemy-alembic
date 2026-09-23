import os
import tempfile
import textwrap

from alembic import command
from alembic.config import Config
from alembic import util
from alembic.script.base import ScriptDirectory
from alembic.script.revision import CycleDetected
from alembic.script.revision import Revision
from alembic.script.revision import RevisionMap
from alembic.script.revision import RevisionPlanError
from alembic.testing import eq_
from alembic.testing import expect_raises_message
from alembic.testing.fixtures import TestBase

A = "aaaa0001"
B = "bbbb0002"
C = "cccc0003"
D = "dddd0004"
X = "xxxx0005"
E = "eeee0006"


def _fixture_map():
    """a branches into b and c; d merges b/c; e depends on the
    independent branch rooted at x."""
    return RevisionMap(
        lambda: [
            Revision(A, None),
            Revision(B, A),
            Revision(C, A),
            Revision(D, (B, C)),
            Revision(X, None),
            Revision(E, D, dependencies=X),
        ]
    )


def _apply_plan_to_state(map_, current, plan):
    """Apply a preview plan step by step to an abstract revision
    state (a set of heads), validating each step as it is applied."""
    state = set(current)
    for step in plan.steps:
        eq_(set(step.remove_heads) <= state, True)
        if step.direction == "upgrade":
            closure = (
                {
                    rev.revision
                    for rev in map_._get_ancestor_nodes(
                        [map_._revision_map[rev_id] for rev_id in state]
                    )
                }
                if state
                else set()
            )
            eq_(set(step.requires) <= closure, True)
        else:
            eq_(step.revision in state, True)
        state -= set(step.remove_heads)
        state |= set(step.add_heads)
    return state


class PreviewPlanTest(TestBase):
    def test_merge_applies_once(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([B, C], [D])
        eq_(len(plan.steps), 1)
        step = plan.steps[0]
        eq_(step.direction, "upgrade")
        eq_(step.revision, D)
        eq_(step.requires, (B, C))
        eq_(step.remove_heads, (B, C))
        eq_(step.add_heads, (D,))
        eq_(plan.final_heads, (D,))
        eq_(_apply_plan_to_state(map_, [B, C], plan), {D})

    def test_merge_splits_into_two_heads(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([D], [B, C])
        eq_(len(plan.steps), 1)
        step = plan.steps[0]
        eq_(step.direction, "downgrade")
        eq_(step.revision, D)
        eq_(step.remove_heads, (D,))
        eq_(step.add_heads, (B, C))
        eq_(plan.final_heads, (B, C))
        eq_(_apply_plan_to_state(map_, [D], plan), {B, C})

    def test_depends_on_dependency_is_retained(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([X, D], [E])
        eq_([s.direction for s in plan.steps], ["upgrade"])
        eq_([s.revision for s in plan.steps], [E])
        eq_(plan.steps[0].requires, (D, X))
        # x is consumed into e's head, never downgraded
        eq_(plan.final_heads, (E,))
        eq_(_apply_plan_to_state(map_, [X, D], plan), {E})

    def test_depends_on_dependency_restored_on_downgrade(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([E], [X, D])
        eq_([s.direction for s in plan.steps], ["downgrade"])
        eq_([s.revision for s in plan.steps], [E])
        eq_(plan.steps[0].add_heads, (D, X))
        eq_(plan.final_heads, (D, X))
        eq_(_apply_plan_to_state(map_, [E], plan), {D, X})

    def test_cross_branch_switch(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([X], [C])
        eq_(
            [(s.direction, s.revision) for s in plan.steps],
            [("downgrade", X), ("upgrade", A), ("upgrade", C)],
        )
        eq_(plan.final_heads, (C,))
        eq_(_apply_plan_to_state(map_, [X], plan), {C})

    def test_shared_ancestor_is_preserved(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([B], [C])
        eq_(
            [(s.direction, s.revision) for s in plan.steps],
            [("downgrade", B), ("upgrade", C)],
        )
        eq_(_apply_plan_to_state(map_, [B], plan), {C})

    def test_ties_sorted_by_revision_string(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([B, C], [])
        eq_(
            [(s.direction, s.revision) for s in plan.steps],
            [("downgrade", B), ("downgrade", C), ("downgrade", A)],
        )
        eq_(plan.final_heads, ())
        eq_(_apply_plan_to_state(map_, [B, C], plan), set())

    def test_base_to_merge_head(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([], [D])
        eq_(
            [s.revision for s in plan.steps],
            [A, B, C, D],
        )
        eq_(_apply_plan_to_state(map_, [], plan), {D})

    def test_noop_when_heads_match(self):
        map_ = _fixture_map()
        plan = map_.preview_plan([D], [D])
        eq_(plan.steps, ())
        eq_(plan.final_heads, (D,))

    def test_partial_identifier_resolves(self):
        map_ = _fixture_map()
        plan = map_.preview_plan(["bbbb"], ["dddd"])
        eq_(plan.current_heads, (B,))
        eq_(plan.target_heads, (D,))

    def test_unknown_revision_rejected_with_position(self):
        map_ = _fixture_map()
        with expect_raises_message(
            RevisionPlanError,
            r"current\[0\]: cannot resolve 'zzzz9999'",
        ):
            map_.preview_plan(["zzzz9999"], [D])
        with expect_raises_message(
            RevisionPlanError,
            r"target\[1\]: cannot resolve 'zzzz9999'",
        ):
            map_.preview_plan([D], [B, "zzzz9999"])

    def test_ambiguous_partial_identifier_rejected(self):
        map_ = RevisionMap(
            lambda: [
                Revision("abc123aa", None),
                Revision("abc123bb", None),
            ]
        )
        with expect_raises_message(
            RevisionPlanError,
            r"current\[0\]: cannot resolve 'abc123'.*Multiple revisions",
        ):
            map_.preview_plan(["abc123"], ["abc123aa"])

    def test_ancestor_mixed_into_head_set_rejected(self):
        map_ = _fixture_map()
        with expect_raises_message(
            RevisionPlanError,
            r"current\[1\]: revision '%s' may not be combined with its "
            r"own ancestor\(s\) '%s'" % (D, A),
        ):
            map_.preview_plan([A, D], [B])
        with expect_raises_message(
            RevisionPlanError,
            r"target\[1\]: revision '%s' may not be combined" % D,
        ):
            map_.preview_plan([B], [A, D])

    def test_duplicate_rejected(self):
        map_ = _fixture_map()
        with expect_raises_message(
            RevisionPlanError,
            r"current\[1\]: revision '%s' duplicates current\[0\]" % B,
        ):
            map_.preview_plan([B, "bbbb0002"], [D])

    def test_dependency_cycle_rejected(self):
        map_ = RevisionMap(
            lambda: [
                Revision("cyc00001", "cyc00002"),
                Revision("cyc00002", "cyc00001"),
            ]
        )
        with expect_raises_message(CycleDetected, "Cycle is detected"):
            map_.preview_plan(["cyc00001"], [])


class PreviewScriptDirectoryTest(TestBase):
    def setup_method(self):
        self._tmpdirs = []

    def teardown_method(self):
        import shutil

        for tmpdir in self._tmpdirs:
            shutil.rmtree(tmpdir, True)

    def _make_script_dir(self):
        tmpdir = tempfile.mkdtemp()
        self._tmpdirs.append(tmpdir)
        versions = os.path.join(tmpdir, "versions")
        os.mkdir(versions)

        def emit(rev, down, depends=None):
            with open(os.path.join(versions, "%s.py" % rev), "w") as f:
                f.write(textwrap.dedent("""\
                        revision = %r
                        down_revision = %r
                        depends_on = %r


                        def upgrade():
                            raise RuntimeError(
                                "sentinel: upgrade() must not be invoked")


                        def downgrade():
                            raise RuntimeError(
                                "sentinel: downgrade() must not be invoked")
                        """) % (rev, down, depends))

        emit(A, None)
        emit(B, A)
        emit(C, A)
        emit(D, (B, C))
        emit(X, None)
        emit(E, D, depends=X)
        return ScriptDirectory(dir=tmpdir)

    def test_preview_with_sentinel_scripts(self):
        script = self._make_script_dir()
        plan = script.preview_revisions([B, C], [D])
        eq_([s.revision for s in plan.steps], [D])
        eq_(plan.final_heads, (D,))

        plan = script.preview_revisions([D], [B, C])
        eq_([s.revision for s in plan.steps], [D])
        eq_(plan.steps[0].direction, "downgrade")
        eq_(plan.steps[0].add_heads, (B, C))

        plan = script.preview_revisions([X], [E])
        eq_(
            [s.revision for s in plan.steps],
            [A, B, C, D, E],
        )
        eq_(plan.final_heads, (E,))

    def test_preview_rejects_bad_head_set(self):
        script = self._make_script_dir()
        with expect_raises_message(
            RevisionPlanError, r"current\[0\]: cannot resolve 'nope0000'"
        ):
            script.preview_revisions(["nope0000"], [D])

    def test_preview_command(self):
        import io

        script = self._make_script_dir()
        buf = io.StringIO()
        cfg = Config(stdout=buf)
        cfg.set_main_option("script_location", script.dir)

        command.preview(cfg, [B, C], target=[D])
        output = buf.getvalue()
        assert "Current heads: bbbb0002, cccc0003" in output
        assert "Step 1: upgrade dddd0004" in output
        assert "Final heads: dddd0004" in output

        buf.seek(0)
        buf.truncate()
        command.preview(cfg, [D], target=[B, C])
        assert "Step 1: downgrade dddd0004" in buf.getvalue()

        with expect_raises_message(
            util.CommandError, r"current\[0\]: cannot resolve 'nope0000'"
        ):
            command.preview(cfg, ["nope0000"], target=[D])
