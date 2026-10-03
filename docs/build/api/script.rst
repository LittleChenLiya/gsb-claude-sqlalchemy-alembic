.. _alembic.script.toplevel:

================
Script Directory
================

The :class:`.ScriptDirectory` object provides programmatic access
to the Alembic version files present in the filesystem.

.. automodule:: alembic.script
    :members:

Revision
========

The :class:`.RevisionMap` object serves as the basis for revision
management, used exclusively by :class:`.ScriptDirectory`.

.. automodule:: alembic.script.revision
    :members:

Selecting Revisions
===================

The :meth:`.RevisionMap.select_revisions` method provides a read-only way
to select a set of revisions and their ancestors.  Given one or more
target revision identifiers, it computes the full dependency closure
(ancestors reachable via ``down_revision`` or ``depends_on``) and returns
the revisions in a stable topological order along with reason edges that
explain why each revision was pulled into the selection.

The result is a :class:`.RevisionSelection` object which holds the
selected revisions, the reason edges, and the resolved targets.  The
:meth:`.RevisionSelection.format` method returns a printable string
showing the selection and the reasons each revision was selected::

    from alembic.script.revision import Revision, RevisionMap

    map_ = RevisionMap(lambda: [
        Revision("a", ()),
        Revision("b", ("a",)),
        Revision("c", ("a",)),
        Revision("d", ("b",), dependencies=("c",)),
    ])

    selection = map_.select_revisions("d")
    print(selection.format())

The above prints::

    Targets: d
    Selected revisions (topological order):
      a  (down_revision of c; down_revision of b)
      c  (depends_on of d)
      b  (down_revision of d)
      d  (target)

Each :class:`.SelectionEdge` records the ``referrer`` (the revision that
references another), the ``referred`` (the revision being referenced), and
the ``reason`` which is either ``"down_revision"`` or ``"depends_on"``.

Branch labels are resolved through the existing resolution machinery, so
selecting by a branch label produces the exact same closure as selecting
by the revision id::

    by_id = map_.select_revisions("d")
    by_label = map_.select_revisions("dhead")
    assert by_id.revisions == by_label.revisions

Targets may be given as a single identifier or a sequence; duplicate
targets are merged automatically.  Use the ``exclude`` parameter to name
revisions that must not appear in the selection.  If an excluded revision
is a necessary ancestor of any target, :class:`.SelectionConflictError`
is raised rather than silently dropping the dependency::

    map_.select_revisions("d", exclude="a")
    # raises SelectionConflictError: Excluded revision a is required by
    # target(s) d

Write Hooks
===========

.. automodule:: alembic.script.write_hooks
    :members:
