"""Printable example for ``RevisionMap.select_revisions()``.

The example builds a small in-memory revision history instead of reading
migration files from disk, so it can be run directly::

    python examples/select_revisions/select_revisions_demo.py

History::

    a -> b ---+
    a -> c (c depends_on b)
    b, c -> d          (d merges the two branches)

Selecting the merge revision ``d`` pulls in ``a``, ``b`` and ``c`` as well;
each selected revision is reported together with *why* it was needed
(``down_revision`` or ``depends_on``).
"""

from alembic.script.revision import RequiredRevisionExcluded
from alembic.script.revision import Revision
from alembic.script.revision import RevisionMap


def build_map() -> RevisionMap:
    return RevisionMap(
        lambda: [
            Revision("a", None, branch_labels="rootbranch"),
            Revision("b", "a"),
            Revision("c", "a", dependencies="b"),
            Revision("d", ("b", "c")),
        ]
    )


def main() -> None:
    revision_map = build_map()

    selection = revision_map.select_revisions("d")

    print("Selected revisions (topological order):")
    print(" ", selection.revision_ids)
    print()

    print("Why each revision was selected:")
    selection.print_reasons()
    print()

    # selecting through a branch label resolves to the same concrete
    # revision and yields exactly the same closure
    via_label = revision_map.select_revisions("rootbranch@d")
    assert via_label == selection
    print("Selecting 'rootbranch@d' yields the identical closure.")
    print()

    # explicitly excluding a required ancestor is rejected rather than
    # silently dropping the dependency
    try:
        revision_map.select_revisions("d", exclude="a")
    except RequiredRevisionExcluded as exc:
        print("Excluding a required revision is rejected:")
        print(" ", exc)


if __name__ == "__main__":
    main()
