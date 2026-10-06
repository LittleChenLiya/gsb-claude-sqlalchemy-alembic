.. change::
    :tags: feature, commands

    Added migration lockfile support: the new ``alembic lock_generate``
    command writes a JSON manifest recording, for every revision script,
    the SHA-256 of its raw source bytes plus its literal
    ``down_revision``, ``depends_on`` and ``branch_labels`` metadata,
    sorted by revision identifier and free of machine-absolute paths.
    The new ``alembic lock_check`` command verifies a script directory
    against the manifest, distinguishing content modifications, relation
    modifications, missing and extra scripts, and reporting dynamic
    metadata, duplicate revisions, dangling dependencies and cycles with
    the offending file and reason.  Revision modules are parsed via AST
    only and are never imported or executed.  See :ref:`lockfile`.
