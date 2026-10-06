"""Migration lockfile generation and verification.

A *lockfile* is a JSON manifest that records, for every revision script
in a script directory:

* the SHA-256 of the script's **raw source bytes**
* its ``down_revision``, ``depends_on`` and ``branch_labels`` metadata

The manifest is sorted by revision identifier and contains only paths
relative to the version locations, never absolute paths of the machine
that generated it.

Metadata is extracted by parsing the module AST only.  Migration modules
are **never imported or executed**, so dynamic (non-literal) metadata is
rejected, and side effects embedded in migration scripts cannot run
during lockfile generation or verification.

.. versionadded:: 1.21.0

"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING

from .. import __version__

if TYPE_CHECKING:
    from .base import ScriptDirectory

FORMAT_VERSION = 1
"""lockfile format version understood by this version of Alembic."""

LOCKFILE_BASENAME = "alembic.lock.json"
"""default filename for a generated lockfile."""

_METADATA_NAMES = ("revision", "down_revision", "depends_on", "branch_labels")

_SEQUENCE_NAMES = ("down_revision", "depends_on", "branch_labels")


class LockfileError(Exception):
    """Raised when a lockfile cannot be generated, parsed or verified.

    Carries the offending filename, when applicable, so that callers can
    report *which* script caused the failure and *why*.
    """

    def __init__(self, reason: str, filename: str | None = None) -> None:
        self.reason = reason
        self.filename = filename
        if filename:
            super().__init__(f"{filename}: {reason}")
        else:
            super().__init__(reason)


def _problem(filename: str | None, reason: str) -> dict[str, str]:
    return {"file": filename or "", "reason": reason}


def _literal_sequence(
    node: ast.expr, name: str, filename: str
) -> tuple[str, ...] | None:
    """Interpret an AST node as None, a string, or a tuple of strings.

    Only literals are accepted; anything else (names, calls, expressions)
    is *dynamic metadata* and is rejected.
    """
    if isinstance(node, ast.Constant):
        if node.value is None:
            return None
        if isinstance(node.value, str):
            return (node.value,)
        raise LockfileError(
            f"metadata field {name!r} must be a string, None, or a "
            f"tuple/list of strings; got literal {node.value!r}",
            filename,
        )
    if isinstance(node, (ast.Tuple, ast.List)):
        values = []
        for elt in node.elts:
            if not (
                isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ):
                raise LockfileError(
                    f"metadata field {name!r} must contain only string "
                    "literals; dynamic elements are not allowed",
                    filename,
                )
            values.append(elt.value)
        return tuple(values)
    raise LockfileError(
        f"metadata field {name!r} is not a literal; only string, None, "
        "tuple and list literals may be used in locked revision scripts "
        "(dynamic metadata is not supported)",
        filename,
    )


def parse_module_metadata(source: bytes, filename: str) -> dict[str, Any]:
    """Extract literal revision metadata from module source bytes.

    The source is parsed with :mod:`ast` only; it is never imported,
    compiled to a code object, or executed.

    Returns a dict with normalized keys ``revision`` (a string),
    ``down_revision``, ``depends_on`` and ``branch_labels`` (each a
    sorted tuple of strings, empty when the field is None/absent).

    :raises LockfileError: if the source cannot be parsed, a metadata
     field is missing, duplicated, or not a literal.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as err:
        raise LockfileError(
            f"cannot parse Python source: {err}", filename
        ) from err

    raw: dict[str, Any] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets: tuple[ast.expr, ...] = tuple(stmt.targets)
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = (stmt.target,)
            value = stmt.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in _METADATA_NAMES:
                if target.id in raw:
                    raise LockfileError(
                        f"metadata field {target.id!r} is assigned more "
                        "than once",
                        filename,
                    )
                raw[target.id] = _literal_sequence(value, target.id, filename)

    if "revision" not in raw:
        raise LockfileError(
            "no 'revision' metadata assignment found; file does not look "
            "like an Alembic revision script",
            filename,
        )
    revision = raw["revision"]
    if (
        revision is None
        or not isinstance(revision, tuple)
        or len(revision) != 1
    ):
        raise LockfileError(
            "metadata field 'revision' must be a single string literal",
            filename,
        )

    normalized: dict[str, Any] = {"revision": revision[0]}
    for name in _SEQUENCE_NAMES:
        normalized[name] = tuple(sorted(raw.get(name) or ()))
    return normalized


class RevisionEntry:
    """One revision script's contribution to the lockfile."""

    __slots__ = (
        "revision",
        "file",
        "sha256",
        "down_revision",
        "depends_on",
        "branch_labels",
    )

    def __init__(
        self,
        revision: str,
        file: str,
        sha256: str,
        down_revision: tuple[str, ...],
        depends_on: tuple[str, ...],
        branch_labels: tuple[str, ...],
    ) -> None:
        self.revision = revision
        self.file = file
        self.sha256 = sha256
        self.down_revision = down_revision
        self.depends_on = depends_on
        self.branch_labels = branch_labels

    @property
    def _all_down_revisions(self) -> tuple[str, ...]:
        return self.down_revision + tuple(
            dep for dep in self.depends_on if dep not in self.down_revision
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "file": self.file,
            "sha256": self.sha256,
            "down_revision": list(self.down_revision) or None,
            "depends_on": list(self.depends_on) or None,
            "branch_labels": list(self.branch_labels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RevisionEntry:
        return cls(
            revision=data["revision"],
            file=data.get("file", ""),
            sha256=data.get("sha256", ""),
            down_revision=tuple(sorted(data.get("down_revision") or ())),
            depends_on=tuple(sorted(data.get("depends_on") or ())),
            branch_labels=tuple(sorted(data.get("branch_labels") or ())),
        )

    def _relation_diffs(self, other: RevisionEntry) -> list[str]:
        diffs = []
        for name in _SEQUENCE_NAMES:
            mine = getattr(self, name)
            theirs = getattr(other, name)
            if mine != theirs:
                diffs.append(
                    f"{name}: lockfile has "
                    f"{list(theirs) or None}, script has "
                    f"{list(mine) or None}"
                )
        return diffs


def scan_entries(
    scriptdir: ScriptDirectory,
) -> tuple[list[RevisionEntry], list[dict[str, str]]]:
    """Scan all version locations of a script directory.

    Returns a tuple of (entries, problems).  Files whose metadata cannot
    be literally parsed are reported in ``problems`` and excluded from
    ``entries``; parsing never imports or executes the modules.
    """
    from .base import Script

    entries: list[RevisionEntry] = []
    problems: list[dict[str, str]] = []
    seen: set[Path] = set()
    for location in scriptdir._version_locations:
        if not location.exists():
            continue
        for file_path in Script._list_py_dir(scriptdir, location):
            if file_path.suffix != ".py":
                continue
            real_path = file_path.resolve()
            if real_path in seen:
                continue
            seen.add(real_path)
            try:
                relative = file_path.relative_to(location).as_posix()
            except ValueError:
                relative = file_path.name
            source = real_path.read_bytes()
            sha256 = hashlib.sha256(source).hexdigest()
            try:
                metadata = parse_module_metadata(source, relative)
            except LockfileError as err:
                problems.append(_problem(relative, err.reason))
                continue
            entries.append(
                RevisionEntry(
                    revision=metadata["revision"],
                    file=relative,
                    sha256=sha256,
                    down_revision=metadata["down_revision"],
                    depends_on=metadata["depends_on"],
                    branch_labels=metadata["branch_labels"],
                )
            )
    return entries, problems


def validate_entries(entries: list[RevisionEntry]) -> list[dict[str, str]]:
    """Validate structural integrity of scanned entries.

    Reports duplicate revision identifiers, dangling dependencies and
    dependency cycles.  Returns a list of problems, each carrying the
    offending file and the reason.
    """
    problems: list[dict[str, str]] = []
    by_rev: dict[str, RevisionEntry] = {}
    for entry in entries:
        existing = by_rev.get(entry.revision)
        if existing is not None:
            problems.append(
                _problem(
                    entry.file,
                    f"duplicate revision id {entry.revision!r}; "
                    f"also defined in {existing.file!r}",
                )
            )
        else:
            by_rev[entry.revision] = entry

    for entry in entries:
        for target in entry._all_down_revisions:
            if target not in by_rev:
                problems.append(
                    _problem(
                        entry.file,
                        f"revision {entry.revision!r} refers to "
                        f"{target!r}, which is not present "
                        "(dangling dependency)",
                    )
                )
    problems.extend(_detect_cycles(by_rev))
    return problems


def _detect_cycles(
    by_rev: dict[str, RevisionEntry],
) -> list[dict[str, str]]:
    """Iterative cycle detection over down_revision + depends_on edges."""
    problems: list[dict[str, str]] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rev: WHITE for rev in by_rev}

    def _known_parents(rev: str) -> Any:
        # dangling references are reported separately; skip them here
        return (
            parent
            for parent in by_rev[rev]._all_down_revisions
            if parent in by_rev
        )

    for start in by_rev:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, Any]] = [(start, _known_parents(start))]
        color[start] = GRAY
        path = [start]
        while stack:
            rev, parents = stack[-1]
            advanced = False
            for parent in parents:
                if color[parent] == GRAY:
                    cycle = path[path.index(parent) :] + [parent]
                    problems.append(
                        _problem(
                            by_rev[parent].file,
                            "dependency cycle detected: " + " -> ".join(cycle),
                        )
                    )
                    # avoid reporting the same cycle repeatedly
                    color[parent] = BLACK
                elif color[parent] == WHITE:
                    color[parent] = GRAY
                    path.append(parent)
                    stack.append((parent, _known_parents(parent)))
                    advanced = True
                    break
            if not advanced:
                color[rev] = BLACK
                path.pop()
                stack.pop()
    return problems


def _format_problems(problems: list[dict[str, str]]) -> str:
    return "; ".join(
        f"{p['file']}: {p['reason']}" if p["file"] else p["reason"]
        for p in problems
    )


def generate_manifest(scriptdir: ScriptDirectory) -> dict[str, Any]:
    """Generate a lockfile manifest for a script directory.

    :raises LockfileError: if any script has unparseable or dynamic
     metadata, duplicated revision ids, dangling dependencies, or
     participates in a dependency cycle.
    """
    entries, problems = scan_entries(scriptdir)
    problems.extend(validate_entries(entries))
    if problems:
        raise LockfileError(
            "cannot generate lockfile: " + _format_problems(problems)
        )
    return {
        "format_version": FORMAT_VERSION,
        "generator": f"alembic {__version__}",
        "revisions": [
            entry.to_dict()
            for entry in sorted(entries, key=lambda e: e.revision)
        ],
    }


def write_manifest(manifest: dict[str, Any], path: str | Path) -> None:
    """Write a manifest to disk as UTF-8 JSON."""
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    Path(path).write_text(text, encoding="utf-8")


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a lockfile manifest.

    :raises LockfileError: if the file is not a lockfile, or carries an
     unknown ``format_version``.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise LockfileError(f"cannot read lockfile: {err}", str(path)) from err
    except ValueError as err:
        raise LockfileError(
            f"lockfile is not valid JSON: {err}", str(path)
        ) from err
    if not isinstance(data, dict) or "format_version" not in data:
        raise LockfileError(
            "not an Alembic lockfile (missing 'format_version')", str(path)
        )
    if data["format_version"] != FORMAT_VERSION:
        raise LockfileError(
            f"unsupported lockfile format version "
            f"{data['format_version']!r}; this version of Alembic only "
            f"supports format version {FORMAT_VERSION}",
            str(path),
        )
    if not isinstance(data.get("revisions"), list):
        raise LockfileError(
            "lockfile is malformed ('revisions' must be a list)", str(path)
        )
    return data


def check_manifest(
    scriptdir: ScriptDirectory,
    manifest: dict[str, Any],
    allow_new_successors: bool = False,
) -> dict[str, Any]:
    """Verify a script directory against a lockfile manifest.

    Returns a machine-readable result dictionary with keys ``status``
    (``"ok"`` or ``"failed"``), ``errors`` (structural problems such as
    dynamic metadata, duplicates, dangling dependencies or cycles, each
    with the offending file and reason), ``differences`` (classified as
    ``content_modified``, ``relations_modified``, ``missing`` or
    ``extra``) and a ``summary`` of counts.

    :param allow_new_successors: when True, revision scripts not present
     in the lockfile are tolerated as long as their ``down_revision`` /
     ``depends_on`` links only refer to revisions already recorded in
     the lockfile or to other such new successors, and no locked entry
     is otherwise modified.
    """
    result: dict[str, Any] = {
        "status": "ok",
        "errors": [],
        "differences": [],
        "summary": {},
    }

    entries, problems = scan_entries(scriptdir)
    problems.extend(validate_entries(entries))
    result["errors"] = problems

    locked = {
        item["revision"]: RevisionEntry.from_dict(item)
        for item in manifest["revisions"]
    }
    current = {entry.revision: entry for entry in entries}

    differences: list[dict[str, Any]] = result["differences"]

    for rev in sorted(locked):
        locked_entry = locked[rev]
        current_entry = current.get(rev)
        if current_entry is None:
            differences.append(
                {
                    "revision": rev,
                    "kind": "missing",
                    "allowed": False,
                    "detail": "recorded in lockfile "
                    f"({locked_entry.file}) but not present on disk",
                }
            )
            continue
        relation_diffs = current_entry._relation_diffs(locked_entry)
        if relation_diffs:
            differences.append(
                {
                    "revision": rev,
                    "kind": "relations_modified",
                    "allowed": False,
                    "detail": "; ".join(relation_diffs),
                }
            )
        elif current_entry.sha256 != locked_entry.sha256:
            differences.append(
                {
                    "revision": rev,
                    "kind": "content_modified",
                    "allowed": False,
                    "detail": "source SHA-256 changed "
                    f"(metadata unchanged; file {current_entry.file})",
                }
            )

    extra_revs = set(current).difference(locked)
    known = set(locked)
    for rev in sorted(extra_revs):
        entry = current[rev]
        unresolved = [
            target
            for target in entry._all_down_revisions
            if target not in known and target not in extra_revs
        ]
        allowed = bool(allow_new_successors) and not unresolved
        if allow_new_successors and unresolved:
            detail = (
                "new revision does not attach to locked revisions only; "
                "unresolved references: " + ", ".join(sorted(unresolved))
            )
        else:
            detail = f"not recorded in lockfile (file {entry.file})"
        differences.append(
            {
                "revision": rev,
                "kind": "extra",
                "allowed": allowed,
                "detail": detail,
            }
        )

    summary: dict[str, int] = {
        "content_modified": 0,
        "relations_modified": 0,
        "missing": 0,
        "extra": 0,
        "extra_allowed": 0,
        "errors": len(problems),
    }
    failed = bool(problems)
    for diff in differences:
        if diff["kind"] == "extra" and diff["allowed"]:
            summary["extra_allowed"] += 1
            continue
        summary[diff["kind"]] += 1
        failed = True
    result["summary"] = summary
    result["status"] = "failed" if failed else "ok"
    return result


def default_lockfile_path(scriptdir: ScriptDirectory) -> str:
    """Return the default lockfile path for a script directory."""
    return str(Path(scriptdir.dir) / LOCKFILE_BASENAME)


def render_text_report(result: dict[str, Any]) -> str:
    """Render a human-readable verification report."""
    lines = []
    for error in result["errors"]:
        location = f"{error['file']}: " if error["file"] else ""
        lines.append(f"ERROR: {location}{error['reason']}")
    for diff in result["differences"]:
        suffix = " (allowed)" if diff.get("allowed") else ""
        lines.append(
            f"{diff['kind']}: {diff['revision']}{suffix} - {diff['detail']}"
        )
    if result["status"] == "ok":
        lines.append("Lockfile verification passed.")
    else:
        lines.append("Lockfile verification failed.")
    return "\n".join(lines)
