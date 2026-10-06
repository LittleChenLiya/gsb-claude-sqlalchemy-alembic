.. _lockfile:

===================
Migration Lockfiles
===================

.. versionadded:: 1.21.0

Alembic can record a **lockfile** for a migration environment: a JSON
manifest that pins the exact content and wiring of every revision script.
For each revision, the lockfile records:

* the SHA-256 of the script's **raw source bytes**
* its ``down_revision``, ``depends_on`` and ``branch_labels`` metadata

The manifest carries a ``format_version`` field, is sorted stably by
revision identifier, and contains only paths relative to the version
locations -- never absolute paths of the machine that generated it.  It
is intended to be checked into source control and shipped alongside
release artifacts, so that the exact set of migrations that was tested
and released can be verified later, on another machine, without a
database.

Metadata is extracted by parsing each module's AST only.  Revision
modules are **never imported or executed**, so scripts with side effects
cannot run during generation or verification, and metadata must be
literal values (strings, ``None``, tuples/lists of strings).  Scripts
that compute ``revision`` or ``down_revision`` dynamically are rejected.

Generating a lockfile
=====================

.. code-block:: console

    $ alembic lock_generate
    Generated lockfile migrations/alembic.lock.json (12 revisions)

By default the lockfile is written to ``alembic.lock.json`` inside the
script directory; pass ``--output``/``-o`` to choose another path.
Generation **fails** (reporting the file and the reason) if any script:

* uses dynamic (non-literal) metadata
* duplicates another script's revision id
* refers to a revision that does not exist (dangling dependency)
* participates in a dependency cycle

Verifying against a lockfile
============================

.. code-block:: console

    $ alembic lock_check
    Lockfile verification passed.

Verification distinguishes four kinds of differences:

* ``content_modified`` -- the source bytes changed but the metadata is
  identical (e.g. an edited comment, or a CRLF/LF line-ending change)
* ``relations_modified`` -- ``down_revision``, ``depends_on`` or
  ``branch_labels`` changed
* ``missing`` -- a revision recorded in the lockfile is gone
* ``extra`` -- a script exists that is not recorded in the lockfile

Structural problems (dynamic metadata, duplicate revisions, dangling
dependencies, cycles) are reported as errors with the offending file and
reason.  Any error or difference makes the command exit with a non-zero
status.  A lockfile with an unknown ``format_version`` is rejected.

Pass ``--json`` for a machine-readable report on stdout (the exit status
still indicates success or failure)::

    $ alembic lock_check --json
    {
      "status": "failed",
      "errors": [],
      "differences": [
        {
          "revision": "a1",
          "kind": "content_modified",
          "allowed": false,
          "detail": "source SHA-256 changed (metadata unchanged; file a1_base.py)"
        }
      ],
      "summary": { ... }
    }

Allowing new successors
=======================

By default any script not present in the lockfile is rejected.  When a
release is being prepared and new migrations have been added on top of a
locked baseline, ``--allow-new-successors`` tolerates **new** revisions
whose ``down_revision``/``depends_on`` links attach only to revisions
already recorded in the lockfile (or to other such new successors).
Modifications to locked revisions are still rejected.

.. code-block:: console

    $ alembic lock_check --allow-new-successors
    extra: e1 (allowed) - not recorded in lockfile (file e1_new.py)
    Lockfile verification passed.

Ninety-second demo
==================

The following transcript exercises generation, tampering and rejection
end to end; no database is needed::

    # set up a scratch environment with a branch and a merge
    mkdir -p demo/migrations/versions && cd demo
    printf '[alembic]\nscript_location = migrations\n' > alembic.ini
    printf 'revision = "a1"\ndown_revision = None\n' > migrations/versions/a1.py
    printf 'revision = "b1"\ndown_revision = "a1"\n' > migrations/versions/b1.py
    printf 'revision = "c1"\ndown_revision = "a1"\n' > migrations/versions/c1.py
    printf 'revision = "d1"\ndown_revision = ("b1", "c1")\n' > migrations/versions/d1.py

    # generate the lockfile, then verify the untouched tree passes
    alembic lock_generate
    alembic lock_check                       # -> Lockfile verification passed.

    # tamper: edit a comment; verification reports a content change
    printf '# touched\n' >> migrations/versions/a1.py
    alembic lock_check                       # -> content_modified: a1 ... (exit 1)
    alembic lock_check --json                # same report, machine-readable

    # remove the parent of b1/c1: dangling dependencies are reported
    rm migrations/versions/a1.py
    alembic lock_check            # -> ERROR: b1.py: ... dangling dependency ...

Programmatic use
================

The same functionality is available from Python via
:mod:`alembic.script.lockfile`, e.g. :func:`.lockfile.generate_manifest`,
:func:`.lockfile.check_manifest` and :func:`.lockfile.load_manifest`, and
via the :func:`.command.lock_generate` and :func:`.command.lock_check`
command functions.
