from alembic.script.revision import CycleDetected
from alembic.script.revision import DependencyCycleDetected
from alembic.script.revision import Revision
from alembic.script.revision import RevisionError
from alembic.script.revision import RevisionMap
from alembic.script.revision import SelectionConflictError
from alembic.testing import eq_
from alembic.testing import expect_raises_message
from alembic.testing.fixtures import TestBase


class SelectionFixture:
    """a -> b, a -> c, d merges b (down_revision) and c (depends_on)."""

    @staticmethod
    def build():
        return RevisionMap(
            lambda: [
                Revision("a", (), branch_labels="abase"),
                Revision("b", ("a",)),
                Revision("c", ("a",)),
                Revision(
                    "d",
                    ("b",),
                    dependencies=("c",),
                    branch_labels="dhead",
                ),
            ]
        )


class SelectRevisionsTest(TestBase):
    def test_basic_selection_topological_order(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("d")
        eq_([r.revision for r in sel.revisions], ["a", "c", "b", "d"])

    def test_selection_contains_all_ancestors(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("d")
        eq_(set(r.revision for r in sel.revisions), {"a", "b", "c", "d"})

    def test_reason_edges_down_revision_and_depends_on(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("d")
        edge_set = {(e.referrer, e.referred, e.reason) for e in sel.edges}
        eq_(
            edge_set,
            {
                ("d", "b", "down_revision"),
                ("d", "c", "depends_on"),
                ("b", "a", "down_revision"),
                ("c", "a", "down_revision"),
            },
        )

    def test_reason_for_revision(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("d")
        a_reasons = sel.reason_for("a")
        eq_(len(a_reasons), 2)
        eq_(
            {(e.referrer, e.reason) for e in a_reasons},
            {("b", "down_revision"), ("c", "down_revision")},
        )
        c_reasons = sel.reason_for("c")
        eq_(len(c_reasons), 1)
        eq_(c_reasons[0].referrer, "d")
        eq_(c_reasons[0].reason, "depends_on")
        eq_(sel.reason_for("d"), ())

    def test_format_printable(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("d")
        output = sel.format()
        assert "Targets: d" in output
        assert "Selected revisions (topological order):" in output
        assert "a  (down_revision of c; down_revision of b)" in output
        assert "c  (depends_on of d)" in output
        assert "b  (down_revision of d)" in output
        assert "d  (target)" in output

    def test_duplicate_targets_merged(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions(["d", "d"])
        eq_([r.revision for r in sel.targets], ["d"])
        eq_(set(r.revision for r in sel.revisions), {"a", "b", "c", "d"})

    def test_multiple_targets_merged(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions(["b", "c"])
        eq_(set(r.revision for r in sel.revisions), {"a", "b", "c"})
        eq_([r.revision for r in sel.revisions], ["a", "c", "b"])

    def test_exclude_necessary_ancestor_rejected(self):
        map_ = SelectionFixture.build()
        with expect_raises_message(
            SelectionConflictError,
            r"Excluded revision a is required by target\(s\) d",
        ):
            map_.select_revisions("d", exclude="a")

    def test_exclude_necessary_ancestor_does_not_drop_dependency(self):
        map_ = SelectionFixture.build()
        try:
            map_.select_revisions("d", exclude="a")
        except SelectionConflictError as err:
            eq_(len(err.conflicts), 1)
            excluded, required_by = err.conflicts[0]
            eq_(excluded.revision, "a")
            eq_(required_by, ["d"])
        else:
            assert False, "expected SelectionConflictError"

    def test_exclude_non_necessary_revision_ok(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", ()),
                Revision("b", ("a",)),
                Revision("c", ("a",)),
                Revision("d", ("b",), dependencies=("c",)),
                Revision("x", ()),
                Revision("y", ("x",)),
            ]
        )
        sel = map_.select_revisions("d", exclude="y")
        eq_(set(r.revision for r in sel.revisions), {"a", "b", "c", "d"})

    def test_missing_target_raises(self):
        map_ = SelectionFixture.build()
        with expect_raises_message(
            RevisionError,
            "No such revision or branch 'nonexistent'",
        ):
            map_.select_revisions("nonexistent")

    def test_missing_referenced_revision_raises(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", ()),
                Revision("b", ("missing",)),
            ]
        )
        with expect_raises_message(
            RevisionError,
            "Revision missing referenced from b is not present",
        ):
            map_.select_revisions("b")

    def test_branch_label_same_closure_as_revision_id(self):
        map_ = SelectionFixture.build()
        sel_by_id = map_.select_revisions("d")
        sel_by_label = map_.select_revisions("dhead")
        eq_(
            set(r.revision for r in sel_by_id.revisions),
            set(r.revision for r in sel_by_label.revisions),
        )
        eq_(
            [r.revision for r in sel_by_id.revisions],
            [r.revision for r in sel_by_label.revisions],
        )
        eq_(sel_by_id.edges, sel_by_label.edges)

    def test_branch_label_not_compared_as_string(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("dhead")
        eq_([r.revision for r in sel.targets], ["d"])

    def test_cycle_detected(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", ("b",)),
                Revision("b", ("a",)),
            ]
        )
        with expect_raises_message(
            CycleDetected,
            r"Cycle is detected in revisions \(a, b\)",
        ):
            map_.select_revisions("a")

    def test_dependency_cycle_detected(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", (), dependencies=("b",)),
                Revision("b", (), dependencies=("a",)),
            ]
        )
        with expect_raises_message(
            DependencyCycleDetected,
            r"Dependency cycle is detected in revisions \(a, b\)",
        ):
            map_.select_revisions("a")

    def test_empty_targets_raises(self):
        map_ = SelectionFixture.build()
        with expect_raises_message(RevisionError, "No revisions were selected"):
            map_.select_revisions("base")

    def test_select_target_by_id(self):
        map_ = SelectionFixture.build()
        sel = map_.select_revisions("d")
        eq_([r.revision for r in sel.targets], ["d"])
        eq_(set(r.revision for r in sel.revisions), {"a", "b", "c", "d"})

    def test_read_only_does_not_modify_map(self):
        map_ = SelectionFixture.build()
        before = list(map_._revision_map.keys())
        map_.select_revisions("d")
        after = list(map_._revision_map.keys())
        eq_(before, after)