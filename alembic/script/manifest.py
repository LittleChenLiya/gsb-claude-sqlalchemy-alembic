# mypy: allow-untyped-defs, allow-untyped-calls
"""Migration manifest generation and verification.

This module implements a *migration lock manifest*: a JSON document that
captures, for every revision script in a :class:`.ScriptDirectory`, the
SHA-256 hash of the script's raw source bytes together with its literal
metadata (``revision``, ``down_revision``, ``depends_on`` and
``branch_labels``).

The manifest can be generated at release time and later verified against
a checkout, distinguishing between:

* **content modifications** - the source bytes of a script changed
  (including comment-only or line-ending changes),
* **relationship modifications** - ``down_revision``, ``depends_on`` or
  ``branch_labels`` of an existing revision changed,
* **missing** scripts - present in the manifest but not on disk,
* **extra** scripts - present on disk but not in the manifest.

Metadata is extracted by parsing the Python source with :mod:`ast`;
migration modules are **never imported or executed**, so dynamic
metadata (non-literal assignments) is reported as an error naming the
offending file, and no side effects of the scripts can take place.

"""

from __future__ import annotations

import ast
from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from typing import NamedTuple
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ScriptDirectory

#: value of the ``format`` key in a manifest document
FORMAT_NAME = "alembic-migration-manifest"

#: manifest format version understood by this module
FORMAT_VERSION = 1

_METADATA_FIELDS = ("revision", "down_revision", "depends_on", "branch_labels")

_only_source_rev_file = re.compile(r"(?!\.\#|__init__)(.*\.py)$")


class ManifestError(Exception):
    """A manifest could not be generated, read or verified.

    Carries the offending ``filename`` (when applicable) and a human
    readable ``reason``.
    """

    def __init__(self, reason: str, filename: str | None = None) -> None:
        self.reason = reason
        self.filename = filename
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.filename:
            return f"{self.filename}: {self.reason}"
        return self.reason


class RevisionEntry(NamedTuple):
    """One revision script as recorded in a manifest."""

    revision: str
    down_revision: tuple[str, ...]
    depends_on: tuple[str, ...]
    branch_labels: tuple[str, ...]
    sha256: str
    #: path relative to the version location, using forward slashes.
    #: Informational only; never an absolute path.
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "down_revision": list(self.down_revision),
            "depends_on": list(self.depends_on),
            "branch_labels": list(self.branch_labels),
            "sha256": self.sha256,
            "path": self.path,
        }


def _literal_value(node: ast.expr, filename: str, field: str) -> Any:
    """Return the literal value of a metadata assignment.

    Only ``str``, ``None`` and tuples/lists of strings are accepted;
    anything else means the script computes its metadata dynamically,
    which cannot be captured without executing the module.
    """
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, str):
            return node.value
        raise ManifestError(
            f"metadata field {field!r} must be a string, None, or a "
            f"tuple/list of strings; got {node.value!r}",
            filename,
        )
    if isinstance(node, (ast.Tuple, ast.List)):
        items = []
        for element in node.elts:
            value = _literal_value(element, filename, field)
            if value is None:
                raise ManifestError(
                    f"metadata field {field!r} may not contain None elements",
                    filename,
                )
            items.append(value)
        return tuple(items)
    raise ManifestError(
        f"dynamic metadata: {field!r} is assigned a non-literal "
        "expression; manifest generation requires static literals",
        filename,
    )


def parse_revision_metadata(source: bytes, filename: str) -> dict[str, Any]:
    """Parse literal revision metadata from a script's source bytes.

    The source is parsed with :mod:`ast` only; the module is never
    imported or executed.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as err:
        raise ManifestError(f"cannot parse Python source: {err}", filename)

    metadata: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in _METADATA_FIELDS:
            if target.id in metadata:
                raise ManifestError(
                    f"metadata field {target.id!r} is assigned more than once",
                    filename,
                )
            metadata[target.id] = _literal_value(value, filename, target.id)

    if "revision" not in metadata:
        raise ManifestError("no 'revision' identifier declared", filename)
    if not isinstance(metadata["revision"], str):
        raise ManifestError("'revision' must be a string literal", filename)
    return metadata


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _iter_version_files(
    script_directory: ScriptDirectory,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(version_location, file_path)`` for every revision script.

    Mirrors the file selection rules of
    :meth:`.ScriptDirectory._load_revisions` without importing anything.
    """
    recursive = script_directory.recursive_version_locations
    for location in script_directory._version_locations:
        if not location.is_dir():
            continue
        if recursive:
            for root, dirs, files in os.walk(location):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for filename in sorted(files):
                    if _only_source_rev_file.match(filename):
                        yield location, Path(root) / filename
        else:
            for path in sorted(location.iterdir()):
                if path.is_file() and _only_source_rev_file.match(path.name):
                    yield location, path


def _validate_graph(entries: list[RevisionEntry]) -> list[ManifestError]:
    """Check for dangling dependencies and dependency cycles."""
    errors: list[ManifestError] = []
    ids = {entry.revision for entry in entries}
    graph: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        targets = entry.down_revision + entry.depends_on
        graph[entry.revision] = targets
        for target in targets:
            if target not in ids:
                errors.append(
                    ManifestError(
                        f"dangling dependency: revision {entry.revision!r} "
                        f"refers to unknown revision {target!r}",
                        entry.path,
                    )
                )

    # iterative DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    for start in graph:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, Any]] = [(start, iter(graph[start]))]
        color[start] = GRAY
        path = [start]
        while stack:
            node, edges = stack[-1]
            advanced = False
            for target in edges:
                if target not in color:
                    continue
                if color[target] == GRAY:
                    cycle = path[path.index(target) :] + [target]
                    errors.append(
                        ManifestError(
                            "dependency cycle detected: "
                            + " -> ".join(repr(r) for r in cycle)
                        )
                    )
                elif color[target] == WHITE:
                    color[target] = GRAY
                    path.append(target)
                    stack.append((target, iter(graph[target])))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                path.pop()
                color[node] = BLACK
    return errors


def scan_revisions(
    script_directory: ScriptDirectory,
) -> tuple[list[RevisionEntry], list[ManifestError]]:
    """Scan all revision scripts without importing them.

    Returns a tuple of ``(entries, errors)``.  Entries are sorted by
    revision identifier for a stable manifest ordering.  Structural
    problems (dynamic metadata, duplicate revision ids, dangling
    dependencies, cycles) are reported as :class:`ManifestError`
    instances carrying the offending file and the reason.
    """
    entries: list[RevisionEntry] = []
    errors: list[ManifestError] = []
    seen: dict[str, RevisionEntry] = {}

    for location, file_path in _iter_version_files(script_directory):
        relative = file_path.relative_to(location).as_posix()
        raw = file_path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        try:
            metadata = parse_revision_metadata(raw, str(file_path))
        except ManifestError as err:
            errors.append(err)
            continue
        entry = RevisionEntry(
            revision=metadata["revision"],
            down_revision=_as_tuple(metadata.get("down_revision")),
            depends_on=_as_tuple(metadata.get("depends_on")),
            branch_labels=_as_tuple(metadata.get("branch_labels")),
            sha256=sha256,
            path=relative,
        )
        if entry.revision in seen:
            errors.append(
                ManifestError(
                    f"duplicate revision id {entry.revision!r} "
                    f"(already defined in {seen[entry.revision].path})",
                    str(file_path),
                )
            )
            continue
        seen[entry.revision] = entry
        entries.append(entry)

    errors.extend(_validate_graph(entries))
    entries.sort(key=lambda entry: entry.revision)
    return entries, errors


def generate_manifest(script_directory: ScriptDirectory) -> dict[str, Any]:
    """Generate a manifest document for a script directory.

    :raises ManifestError: if any revision script has dynamic metadata,
     a duplicate revision id, a dangling dependency, or participates in
     a dependency cycle.  Generation fails as a whole in these cases.
    """
    entries, errors = scan_revisions(script_directory)
    if errors:
        raise ManifestError(
            "cannot generate manifest:\n"
            + "\n".join(f"  {err}" for err in errors)
        )
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "revisions": [entry.as_dict() for entry in entries],
    }


def write_manifest(manifest: dict[str, Any], destination: str) -> None:
    """Write a manifest document to a JSON file."""
    Path(destination).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_manifest(source: str) -> dict[str, Any]:
    """Read and validate a manifest document.

    :raises ManifestError: if the file is not an Alembic migration
     manifest or carries an unknown format version.
    """
    try:
        data = json.loads(Path(source).read_bytes().decode("utf-8"))
    except (OSError, ValueError) as err:
        raise ManifestError(f"cannot read manifest: {err}", source)
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        raise ManifestError(
            "not an Alembic migration manifest "
            f"(expected format {FORMAT_NAME!r})",
            source,
        )
    version = data.get("version")
    if version != FORMAT_VERSION:
        raise ManifestError(
            f"unsupported manifest format version {version!r}; "
            f"this Alembic version supports version {FORMAT_VERSION}",
            source,
        )
    if not isinstance(data.get("revisions"), list):
        raise ManifestError("manifest has no 'revisions' list", source)
    return data


def check_manifest(
    manifest: dict[str, Any],
    entries: list[RevisionEntry],
    errors: list[ManifestError],
    allow_new_successors: bool = False,
) -> dict[str, Any]:
    """Compare a manifest against freshly scanned revision entries.

    Returns a machine-readable report dictionary.  ``report["ok"]`` is
    True only when no differences and no structural errors were found.
    """
    manifest_entries = {r["revision"]: r for r in manifest["revisions"]}
    current = {entry.revision: entry for entry in entries}

    report: dict[str, Any] = {
        "ok": True,
        "content_modified": [],
        "relationship_modified": [],
        "missing": [],
        "extra": [],
        "added": [],
        "errors": [str(err) for err in errors],
    }

    for rev_id in sorted(manifest_entries):
        recorded = manifest_entries[rev_id]
        entry = current.get(rev_id)
        if entry is None:
            report["missing"].append(
                {"revision": rev_id, "path": recorded.get("path")}
            )
            continue
        field_diffs = {}
        for field in ("down_revision", "depends_on", "branch_labels"):
            if list(recorded[field]) != list(getattr(entry, field)):
                field_diffs[field] = {
                    "manifest": list(recorded[field]),
                    "current": list(getattr(entry, field)),
                }
        if field_diffs:
            report["relationship_modified"].append(
                {"revision": rev_id, "path": entry.path, "fields": field_diffs}
            )
        elif entry.sha256 != recorded["sha256"]:
            report["content_modified"].append(
                {
                    "revision": rev_id,
                    "path": entry.path,
                    "manifest_sha256": recorded["sha256"],
                    "current_sha256": entry.sha256,
                }
            )

    extras = [e for e in entries if e.revision not in manifest_entries]
    if extras and allow_new_successors:
        # an extra revision is acceptable only when every revision it
        # builds upon is already part of the manifest (or is itself an
        # accepted new successor)
        known = set(manifest_entries)
        remaining = list(extras)
        progressed = True
        while progressed:
            progressed = False
            for entry in list(remaining):
                targets = set(entry.down_revision) | set(entry.depends_on)
                if targets <= known:
                    known.add(entry.revision)
                    report["added"].append(
                        {"revision": entry.revision, "path": entry.path}
                    )
                    remaining.remove(entry)
                    progressed = True
        extras = remaining
    report["extra"].extend(
        {"revision": entry.revision, "path": entry.path} for entry in extras
    )

    report["ok"] = not (
        report["errors"]
        or report["content_modified"]
        or report["relationship_modified"]
        or report["missing"]
        or report["extra"]
    )
    return report


def format_report(report: dict[str, Any]) -> list[str]:
    """Render a check report as human-readable lines."""
    lines: list[str] = []
    for err in report["errors"]:
        lines.append(f"ERROR: {err}")
    for item in report["content_modified"]:
        lines.append(
            f"CONTENT MODIFIED: {item['revision']} ({item['path']}): "
            f"sha256 {item['manifest_sha256'][:12]}... -> "
            f"{item['current_sha256'][:12]}..."
        )
    for item in report["relationship_modified"]:
        fields = ", ".join(sorted(item["fields"]))
        lines.append(
            f"RELATIONSHIP MODIFIED: {item['revision']} "
            f"({item['path']}): {fields}"
        )
    for item in report["missing"]:
        lines.append(f"MISSING: {item['revision']} ({item['path']})")
    for item in report["extra"]:
        lines.append(f"EXTRA: {item['revision']} ({item['path']})")
    for item in report["added"]:
        lines.append(
            f"ADDED (allowed new successor): {item['revision']} "
            f"({item['path']})"
        )
    if not lines:
        lines.append("no differences")
    return lines
