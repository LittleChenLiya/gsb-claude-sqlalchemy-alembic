.. _manifest:

==========================
Migration Lock Manifests
==========================

.. versionadded:: 1.17

Alembic can record a **lock manifest** of a migration scripts directory at
release time: a JSON document that captures, for every revision script, the
SHA-256 hash of the script's *raw source bytes* together with its literal
``revision``, ``down_revision``, ``depends_on`` and ``branch_labels``
metadata.  The manifest can then be verified against any later checkout to
prove that the released migration scripts were not tampered with.

No database connection is needed for either operation, and migration modules
are **never imported or executed** - metadata is extracted by parsing the
Python source with the :mod:`ast` module only, so scripts that compute their
metadata dynamically are rejected rather than executed.

Generating a manifest
=====================

Use the ``manifest_generate`` command::

    alembic manifest_generate --output alembic-manifest.json

The generated document looks like::

    {
      "format": "alembic-migration-manifest",
      "version": 1,
      "revisions": [
        {
          "revision": "1975ea83b712",
          "down_revision": [],
          "depends_on": [],
          "branch_labels": [],
          "sha256": "b2e3...",
          "path": "1975ea83b712_create_account_table.py"
        }
      ]
    }

Key properties:

* Entries are **sorted by revision identifier**, so the manifest is stable
  across runs and machines.
* ``sha256`` is computed over the **raw bytes** of the source file; changing
  anything - including comments or line endings (CRLF vs. LF) - changes the
  hash.
* ``path`` is relative to the version location; **no absolute paths** of the
  generating machine are recorded.
* The document carries a **format version**.  Manifests with an unknown
  ``format`` or ``version`` are rejected by ``manifest_check``.

Generation **fails** (reporting the offending file and the reason) when:

* a script assigns **dynamic (non-literal) metadata**, e.g.
  ``revision = compute_id()`` - such scripts are never executed, so their
  metadata cannot be captured;
* two scripts declare the **same revision id**;
* a ``down_revision`` or ``depends_on`` refers to a revision that does not
  exist (**dangling dependency**);
* revisions form a **dependency cycle**.

Verifying a manifest
====================

Use the ``manifest_check`` command::

    alembic manifest_check --manifest alembic-manifest.json

The check re-scans the scripts directory and reports, distinguishing:

* **CONTENT MODIFIED** - the source bytes of a known revision changed
  (including comment-only or CRLF/LF changes);
* **RELATIONSHIP MODIFIED** - ``down_revision``, ``depends_on`` or
  ``branch_labels`` of a known revision changed;
* **MISSING** - a revision recorded in the manifest is gone from disk
  (if other revisions still refer to it, a *dangling dependency* error is
  reported as well);
* **EXTRA** - a script exists on disk that is not in the manifest.

Any difference, or any structural error found while scanning, exits the
command with a non-zero status.

New revisions that were legitimately added *after* the manifest was
generated can be allowed explicitly::

    alembic manifest_check --allow-new-successors

With this flag, an extra revision is accepted only if every revision it
builds upon (via ``down_revision`` / ``depends_on``) is already part of the
manifest (or is itself an accepted new successor); anything else is still
rejected.

Machine-readable output
=======================

Pass ``--json`` to emit the full report as JSON on stdout, suitable for CI
pipelines::

    alembic manifest_check --json

The report contains the keys ``ok``, ``content_modified``,
``relationship_modified``, ``missing``, ``extra``, ``added`` and ``errors``.

A ninety second demo
====================

The following local commands demonstrate generate / tamper / reject without
any database::

    # set up a scratch environment (alembic.ini is written to the cwd)
    cd /tmp
    alembic init manifest_demo
    alembic revision -m "one" --rev-id a1
    alembic revision -m "two" --rev-id b2

    # generate the lock manifest and verify it passes as-is
    alembic manifest_generate -o manifest.json
    alembic manifest_check --manifest manifest.json
    # -> Manifest check passed: 2 revision(s) verified

    # tamper with a released script (a comment is enough)
    echo "# changed after release" >> manifest_demo/versions/a1_one.py
    alembic manifest_check --manifest manifest.json
    # -> CONTENT MODIFIED: a1 (a1_one.py): sha256 ...
    # -> FAILED: manifest check failed ...   (non-zero exit)

    # machine-readable report for automation
    alembic manifest_check --manifest manifest.json --json

Programmatic use
================

The same functionality is available as
:func:`alembic.command.manifest_generate` and
:func:`alembic.command.manifest_check`, and the underlying machinery lives
in the :mod:`alembic.script.manifest` module.
