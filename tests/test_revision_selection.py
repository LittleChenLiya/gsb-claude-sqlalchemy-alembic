from alembic.script.revision import CycleDetected
from alembic.script.revision import DependencyCycleDetected
from alembic.script.revision import RequiredRevisionExcluded
from alembic.script.revision import ResolutionError
from alembic.script.revision import Revision
from alembic.script.revision import RevisionError
from alembic.script.revision import RevisionMap
from alembic.script.revision import RevisionReferenceNotFound
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import expect_raises_message
from alembic.testing.fixtures import TestBase


class SelectRevisionsTest(TestBase):
    """Tests for :meth:`.RevisionMap.select_revisions`."""

    def _merge_map(self):
        # a -> b; a -> c with c depends_on b; d merges b and c.
        return RevisionMap(
            lambda: [
                Revision("a", None, branch_labels="rootbranch"),
                Revision("b", "a"),
                Revision("c", "a", dependencies="b"),
                Revision("d", ("b", "c")),
            ]
        )

    def _assert_merge_selection(self, selection):
        eq_(selection.revision_ids, ("a", "b", "c", "d"))
        eq_(
            [tuple(edge) for edge in selection.reasons],
            [
                ("a", "b", "down_revision"),
                ("a", "c", "down_revision"),
                ("b", "c", "depends_on"),
                ("b", "d", "down_revision"),
                ("c", "d", "down_revision"),
            ],
        )
        eq_([r.revision for r in selection.targets], ["d"])

    def test_select_merge_target_revisions_and_reasons(self):
        selection = self._merge_map().select_revisions("d")
        self._assert_merge_selection(selection)

    def test_select_includes_both_reason_kinds(self):
        selection = self._merge_map().select_revisions("d")
        eq_(
            {edge.reason for edge in selection.reasons},
            {"down_revision", "depends_on"},
        )
        eq_(
            [tuple(edge) for edge in selection.reasons_for("c")],
            [
                ("a", "c", "down_revision"),
                ("b", "c", "depends_on"),
            ],
        )

    def test_select_duplicate_targets_merged(self):
        map_ = self._merge_map()
        selection = map_.select_revisions(["d", "d"])
        self._assert_merge_selection(selection)
        eq_([rev.revision for rev in selection.targets], ["d"])

    def test_select_target_revision_objects(self):
        map_ = self._merge_map()
        selection = map_.select_revisions(map_.get_revision("d"))
        self._assert_merge_selection(selection)

    def test_select_branch_label_same_closure(self):
        map_ = self._merge_map()
        by_id = map_.select_revisions("d")
        by_label = map_.select_revisions("rootbranch@d")

        # resolves the label to the same Revision objects, not via direct
        # string comparison against the concrete revision id
        eq_(
            [id(rev) for rev in by_label.revisions],
            [id(rev) for rev in by_id.revisions],
        )
        eq_(by_label, by_id)
        eq_(by_label.reasons, by_id.reasons)

    def test_select_exclude_required_ancestor_rejected(self):
        map_ = self._merge_map()
        with expect_raises_message(
            RequiredRevisionExcluded,
            r"'a' is required by target 'd' \(d -> b -> a\)",
        ) as exc_info:
            map_.select_revisions("d", exclude="a")
        eq_(
            exc_info.error.conflicts,
            (("d", "a", ("d", "b", "a")),),
        )
        # read-only: the map itself is untouched
        eq_(map_.heads, ("d",))

    def test_select_exclude_required_ancestor_via_label_rejected(self):
        map_ = self._merge_map()
        assert_raises_message(
            RequiredRevisionExcluded,
            "Cannot exclude revision",
            map_.select_revisions,
            "d",
            "rootbranch@a",
        )

    def test_select_exclude_unrelated_revision_allowed(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", "a"),
                Revision("x", None),
            ]
        )
        selection = map_.select_revisions("b", exclude="x")
        eq_(selection.revision_ids, ("a", "b"))

    def test_select_missing_target_raises(self):
        map_ = self._merge_map()
        assert_raises_message(
            ResolutionError,
            "No such revision or branch 'nope'",
            map_.select_revisions,
            "nope",
        )

    def test_select_missing_exclude_raises(self):
        map_ = self._merge_map()
        assert_raises_message(
            ResolutionError,
            "No such revision or branch 'nope'",
            map_.select_revisions,
            "d",
            "nope",
        )

    def test_missing_down_revision_referenced_raises(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", "missing_rev"),
            ]
        )
        assert_raises_message(
            RevisionReferenceNotFound,
            "Revision 'missing_rev' referenced from revision 'b' via "
            "down_revision is not present in the revision map",
            map_.select_revisions,
            "b",
        )

    def test_missing_depends_on_referenced_raises(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", None, dependencies="missing_rev"),
            ]
        )
        assert_raises_message(
            RevisionReferenceNotFound,
            "Revision 'missing_rev' referenced from revision 'b' via "
            "depends_on is not present in the revision map",
            map_.select_revisions,
            "b",
        )

    def test_missing_reference_from_unrelated_revision_raises(self):
        # the broken reference is reported even when the target does not
        # depend on it, because the whole map must be valid
        map_ = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", "a"),
                Revision("c", "ghost"),
            ]
        )
        assert_raises_message(
            RevisionReferenceNotFound,
            "'ghost' referenced from revision 'c'",
            map_.select_revisions,
            "b",
        )

    def test_down_revision_cycle_raises(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", "c"),
                Revision("b", "a"),
                Revision("c", "b"),
            ]
        )
        assert_raises_message(
            CycleDetected,
            r"Cycle is detected in revisions \(a, b, c\)",
            map_.select_revisions,
            "b",
        )

    def test_depends_on_cycle_raises(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", None, dependencies="b"),
                Revision("b", None, dependencies="a"),
            ]
        )
        assert_raises_message(
            DependencyCycleDetected,
            r"Dependency cycle is detected in revisions \(a, b\)",
            map_.select_revisions,
            "a",
        )

    def test_select_multiple_targets_union(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", "a"),
                Revision("x", None),
                Revision("y", "x"),
            ]
        )
        selection = map_.select_revisions(["b", "y"])
        eq_(set(selection.revision_ids), {"a", "b", "x", "y"})
        ids = selection.revision_ids
        assert ids.index("a") < ids.index("b")
        assert ids.index("x") < ids.index("y")

    def test_select_symbolic_identifiers_rejected(self):
        map_ = self._merge_map()
        for identifier in ("base", "heads"):
            assert_raises_message(
                RevisionError,
                "symbolic identifier '%s'" % identifier,
                map_.select_revisions,
                identifier,
            )

    def test_select_empty_targets_rejected(self):
        assert_raises_message(
            RevisionError,
            "at least one revision",
            self._merge_map().select_revisions,
            [],
        )

    def test_select_does_not_mutate_map(self):
        map_ = self._merge_map()
        map_.select_revisions("d", exclude=[])
        eq_(map_.heads, ("d",))
        eq_(map_.bases, ("a",))

    def test_exclude_conflict_for_depends_on_path(self):
        map_ = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", None),
                Revision("c", None, dependencies="b"),
            ]
        )
        assert_raises_message(
            RequiredRevisionExcluded,
            r"'b' is required by target 'c' \(c -> b\)",
            map_.select_revisions,
            "c",
            "b",
        )

    def test_format_reasons_printable(self):
        selection = self._merge_map().select_revisions("d")
        expected = (
            "a\n"
            "b (requires a via down_revision)\n"
            "c (requires a via down_revision; requires b via depends_on)\n"
            "d (requires b via down_revision; requires c via "
            "down_revision; target)"
        )
        eq_(selection.format_reasons(), expected)
        eq_(str(selection), expected)
