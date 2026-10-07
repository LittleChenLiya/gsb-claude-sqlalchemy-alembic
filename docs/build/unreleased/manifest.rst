.. change::
    :tags: feature, commands

    Added migration lock manifests: the new ``manifest_generate`` and
    ``manifest_check`` commands record the SHA-256 hash of every revision
    script's raw source bytes together with its literal ``down_revision``,
    ``depends_on`` and ``branch_labels`` metadata in a versioned JSON
    manifest, and later verify a checkout against it, distinguishing content
    modifications, relationship modifications, missing and extra scripts.
    Metadata is parsed with :mod:`ast` only - migration modules are never
    imported or executed - and no database connection is required.  See
    :ref:`manifest` for details.
