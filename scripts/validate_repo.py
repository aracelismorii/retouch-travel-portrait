#!/usr/bin/env python3
"""Validate the release repository without importing the retouch scripts."""

from __future__ import annotations

import argparse
import ast
import json
import re
import stat
import sys
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional, Sequence

import yaml


SKILL_DIRECTORY = "retouch-travel-portrait"
RELEASE_VERSION = "v0.7.0-beta"
MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
ALLOWED_FRONTMATTER_KEYS = {
    "allowed-tools",
    "description",
    "license",
    "metadata",
    "name",
}

REQUIRED_ROOT_FILES = (
    "README.md",
    "LICENSE",
    "VALIDATION.md",
    "RELEASE_NOTES.md",
    "REAL_IMAGE_VALIDATION.md",
    "validation-summary.json",
    "requirements-validation.txt",
)

REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "requirements.txt",
    "references/audit-gates.json",
    "references/manual-review-template.json",
    "references/model-parameters.json",
    "references/model-prompts.md",
    "references/parameters.json",
    "references/pipeline-v6.json",
    "references/quality-gates.md",
    "references/stage-contracts.md",
)

FORBIDDEN_ARCHIVE_DIRECTORIES = frozenset(
    {
        ".git",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "candidates",
        "checkpoints",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "outputs",
        "proof",
        "runs",
        "venv",
        "work",
    }
)
FORBIDDEN_ARCHIVE_FILENAMES = frozenset(
    {
        ".coverage",
        ".pipeline.lock",
        "audit.json",
        "manifest.json",
        "manual-review.json",
        "queue.json",
        "run.json",
    }
)
FORBIDDEN_IMAGE_SUFFIXES = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)
REQUIRED_ARCHIVE_FILES = frozenset(
    {
        "LICENSE",
        "README.md",
        "RELEASE_NOTES.md",
        "VALIDATION.md",
        "requirements-validation.txt",
        "scripts/validate_repo.py",
        f"{SKILL_DIRECTORY}/SKILL.md",
        f"{SKILL_DIRECTORY}/requirements.txt",
    }
)
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

# Assemble these patterns from fragments so the validator does not flag its own
# source when it is included in the archive being inspected.
WORKSTATION_PATH_PATTERNS = (
    re.compile(rb"/" + b"Users" + rb"/[^/\s\"']+/"),
    re.compile(rb"/" + b"home" + rb"/[^/\s\"']+/"),
    re.compile(rb"/" + b"private" + rb"/(?:tmp|var)/"),
    re.compile(rb"[A-Za-z]:\\\\" + b"Users" + rb"\\\\"),
)


class ValidationError(Exception):
    """Raised when one or more release checks fail."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path}: file is not valid UTF-8") from exc
    except OSError as exc:
        raise ValidationError(f"{path}: cannot read file: {exc}") from exc


def _reject_duplicate_json_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _parse_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(_read_text(path))
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path}: invalid YAML: {exc}") from exc


def _parse_json(path: Path) -> Any:
    try:
        return json.loads(
            _read_text(path), object_pairs_hook=_reject_duplicate_json_keys
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except ValidationError as exc:
        raise ValidationError(f"{path}: {exc}") from exc


def _parse_skill_frontmatter(path: Path) -> dict[str, Any]:
    content = _read_text(path)
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
    _require(match is not None, f"{path}: missing or malformed YAML frontmatter")

    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path}: invalid frontmatter YAML: {exc}") from exc

    _require(isinstance(value, dict), f"{path}: frontmatter must be a mapping")
    return value


def _validate_frontmatter(skill_dir: Path) -> str:
    path = skill_dir / "SKILL.md"
    frontmatter = _parse_skill_frontmatter(path)

    unexpected = sorted(set(frontmatter) - ALLOWED_FRONTMATTER_KEYS)
    _require(
        not unexpected,
        f"{path}: unexpected frontmatter keys: {', '.join(unexpected)}",
    )

    name = frontmatter.get("name")
    _require(isinstance(name, str), f"{path}: 'name' must be a string")
    name = name.strip()
    _require(bool(name), f"{path}: 'name' must not be empty")
    _require(
        re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is not None,
        f"{path}: 'name' must use lowercase hyphen-case",
    )
    _require(
        len(name) <= MAX_SKILL_NAME_LENGTH,
        f"{path}: 'name' exceeds {MAX_SKILL_NAME_LENGTH} characters",
    )
    _require(
        name == skill_dir.name,
        f"{path}: frontmatter name {name!r} must match directory {skill_dir.name!r}",
    )

    description = frontmatter.get("description")
    _require(
        isinstance(description, str), f"{path}: 'description' must be a string"
    )
    description = description.strip()
    _require(bool(description), f"{path}: 'description' must not be empty")
    _require(
        len(description) <= MAX_DESCRIPTION_LENGTH,
        f"{path}: 'description' exceeds {MAX_DESCRIPTION_LENGTH} characters",
    )
    _require(
        "<" not in description and ">" not in description,
        f"{path}: 'description' must not contain angle brackets",
    )
    return name


def _validate_openai_yaml(skill_dir: Path, skill_name: str) -> None:
    path = skill_dir / "agents" / "openai.yaml"
    document = _parse_yaml(path)
    _require(isinstance(document, dict), f"{path}: document must be a mapping")

    interface = document.get("interface")
    _require(isinstance(interface, dict), f"{path}: 'interface' must be a mapping")

    for field in ("display_name", "short_description", "default_prompt"):
        value = interface.get(field)
        _require(
            isinstance(value, str) and bool(value.strip()),
            f"{path}: interface.{field} must be a non-empty string",
        )

    default_prompt = interface["default_prompt"]
    _require(
        f"${skill_name}" in default_prompt,
        f"{path}: interface.default_prompt must mention ${skill_name}",
    )

    short_description = interface["short_description"].strip()
    _require(
        len(short_description) <= 64,
        f"{path}: interface.short_description exceeds 64 characters",
    )


def _validate_json_files(skill_dir: Path) -> int:
    paths = sorted(skill_dir.rglob("*.json"))
    _require(bool(paths), f"{skill_dir}: no JSON reference files found")
    for path in paths:
        _parse_json(path)
    return len(paths)


def _validate_python_files(root: Path, skill_dir: Path) -> int:
    paths = sorted((skill_dir / "scripts").glob("*.py"))
    paths.extend(sorted((skill_dir / "tests").glob("test_*.py")))
    validator = root / "scripts" / "validate_repo.py"
    if validator.exists():
        paths.append(validator)

    unique_paths = sorted(set(paths))
    _require(bool(unique_paths), f"{skill_dir}: no Python source files found")

    for path in unique_paths:
        source = _read_text(path)
        _require(bool(source.strip()), f"{path}: Python file is empty")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise ValidationError(
                f"{path}:{exc.lineno}:{exc.offset}: invalid Python syntax: {exc.msg}"
            ) from exc
    return len(unique_paths)


def _validate_required_files(root: Path, skill_dir: Path) -> None:
    for relative in REQUIRED_ROOT_FILES:
        path = root / relative
        _require(path.is_file(), f"missing required repository file: {path}")
        _require(bool(_read_text(path).strip()), f"required file is empty: {path}")

    for relative in REQUIRED_SKILL_FILES:
        path = skill_dir / relative
        _require(path.is_file(), f"missing required Skill file: {path}")
        _require(bool(_read_text(path).strip()), f"required file is empty: {path}")

    scripts = sorted((skill_dir / "scripts").glob("*.py"))
    tests = sorted((skill_dir / "tests").glob("test_*.py"))
    _require(bool(scripts), f"{skill_dir}: scripts directory has no Python scripts")
    _require(bool(tests), f"{skill_dir}: tests directory has no test_*.py files")


def _validate_release_version(root: Path, skill_dir: Path) -> None:
    paths = (
        root / "README.md",
        root / "RELEASE_NOTES.md",
        root / "VALIDATION.md",
        skill_dir / "SKILL.md",
    )
    for path in paths:
        _require(
            RELEASE_VERSION in _read_text(path),
            f"{path}: must identify the release as {RELEASE_VERSION}",
        )


def _archive_parts(name: str, archive: Path) -> tuple[str, ...]:
    _require(bool(name), f"{archive}: archive contains an empty member name")
    _require(
        "\\" not in name,
        f"{archive}: archive member must use POSIX separators: {name!r}",
    )
    path = PurePosixPath(name)
    _require(
        not path.is_absolute() and re.match(r"^[A-Za-z]:", name) is None,
        f"{archive}: archive member has an absolute path: {name!r}",
    )
    parts = path.parts
    _require(
        bool(parts) and all(part not in {"", ".", ".."} for part in parts),
        f"{archive}: archive member has an unsafe path: {name!r}",
    )
    return parts


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_string_values(child)


def _validate_archived_json_paths(
    payload: bytes, canonical_name: str, archive: Path
) -> None:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"{archive}: invalid JSON member {canonical_name}: {exc}"
        ) from exc
    for value in _iter_string_values(document):
        _require(
            not value.startswith(("/", "\\"))
            and re.match(r"^[A-Za-z]:[\\/]", value) is None,
            f"{archive}: absolute path in JSON member {canonical_name}",
        )


def _validate_archive(path: Path) -> int:
    archive = path.resolve()
    _require(archive.is_file(), f"release archive does not exist: {archive}")
    try:
        handle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError(f"{archive}: unreadable ZIP archive: {exc}") from exc

    with handle:
        members = handle.infolist()
        _require(bool(members), f"{archive}: release archive is empty")
        seen_names: set[str] = set()
        top_levels: set[str] = set()
        relative_files: set[str] = set()
        total_size = 0

        for info in members:
            parts = _archive_parts(info.filename, archive)
            canonical_name = "/".join(parts)
            _require(
                canonical_name not in seen_names,
                f"{archive}: duplicate archive member: {canonical_name}",
            )
            seen_names.add(canonical_name)
            top_levels.add(parts[0])
            lower_parts = tuple(part.casefold() for part in parts)

            _require(
                not any(part in FORBIDDEN_ARCHIVE_DIRECTORIES for part in lower_parts),
                f"{archive}: generated, cache, VCS, or run directory is forbidden: {canonical_name}",
            )
            mode = info.external_attr >> 16
            _require(
                stat.S_IFMT(mode) != stat.S_IFLNK,
                f"{archive}: symbolic links are forbidden: {canonical_name}",
            )
            _require(
                info.flag_bits & 0x1 == 0,
                f"{archive}: encrypted members are forbidden: {canonical_name}",
            )

            if info.is_dir():
                continue
            _require(
                len(parts) > 1,
                f"{archive}: every release file must live below one versioned root",
            )
            relative_name = "/".join(parts[1:])
            relative_files.add(relative_name)
            filename = parts[-1].casefold()
            suffix = PurePosixPath(filename).suffix
            _require(
                filename not in FORBIDDEN_ARCHIVE_FILENAMES
                and not filename.endswith(".review.json")
                and not filename.endswith((".pyc", ".pyo"))
                and not filename.startswith(".coverage.")
                and not any(part.endswith(".egg-info") for part in lower_parts),
                f"{archive}: generated run evidence is forbidden: {canonical_name}",
            )
            _require(
                suffix not in FORBIDDEN_IMAGE_SUFFIXES,
                f"{archive}: image assets are forbidden: {canonical_name}",
            )

            total_size += info.file_size
            _require(
                total_size <= MAX_ARCHIVE_UNCOMPRESSED_BYTES,
                f"{archive}: uncompressed archive exceeds the release size limit",
            )
            try:
                payload = handle.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ValidationError(
                    f"{archive}: cannot read member {canonical_name}: {exc}"
                ) from exc
            for pattern in WORKSTATION_PATH_PATTERNS:
                _require(
                    pattern.search(payload) is None,
                    f"{archive}: leaked workstation absolute path in {canonical_name}",
                )
            if suffix == ".json":
                _validate_archived_json_paths(payload, canonical_name, archive)

        _require(
            len(top_levels) == 1,
            f"{archive}: release files must share one versioned root directory",
        )
        missing = sorted(REQUIRED_ARCHIVE_FILES - relative_files)
        _require(
            not missing,
            f"{archive}: missing required archive files: {', '.join(missing)}",
        )
    return len(relative_files)


def validate(root: Path) -> tuple[int, int]:
    root = root.resolve()
    _require(root.is_dir(), f"repository root does not exist: {root}")

    skill_dir = root / SKILL_DIRECTORY
    _require(skill_dir.is_dir(), f"missing Skill directory: {skill_dir}")

    _validate_required_files(root, skill_dir)
    _validate_release_version(root, skill_dir)
    skill_name = _validate_frontmatter(skill_dir)
    _validate_openai_yaml(skill_dir, skill_name)
    json_count = _validate_json_files(skill_dir)
    python_count = _validate_python_files(root, skill_dir)
    return json_count, python_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the retouch-travel-portrait release repository."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        type=Path,
        help="repository root (default: current directory)",
    )
    parser.add_argument(
        "--archive",
        action="append",
        default=[],
        type=Path,
        help="also inspect a release ZIP for generated or sensitive content",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        json_count, python_count = validate(args.root)
        archive_counts = [_validate_archive(path) for path in args.archive]
    except ValidationError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1

    archive_summary = ""
    if archive_counts:
        archive_summary = (
            f", and {sum(archive_counts)} files across "
            f"{len(archive_counts)} release archive(s)"
        )
    print(
        "VALIDATION PASSED: Skill structure, frontmatter, agent metadata, "
        f"{json_count} JSON files, and {python_count} Python files are valid"
        f"{archive_summary}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
