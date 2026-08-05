"""Strict, data-only SKILL.md frontmatter parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from aiharness.skills.errors import SkillManifestError

SKILL_FILENAME = "SKILL.md"
_SKILL_ID = re.compile(r"^[a-z][a-z0-9][a-z0-9._-]{0,62}$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
_KNOWN_FIELDS = frozenset(
    {"name", "description", "version", "allowed_tools", "required_permissions", "tags"}
)


@dataclass(frozen=True, slots=True)
class SkillFrontmatter:
    """Metadata exposed during discovery without exposing the Markdown body."""

    name: str
    description: str
    version: str = "1.0.0"
    allowed_tools: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _SKILL_ID.fullmatch(self.name) is None:
            raise SkillManifestError(f"Invalid Skill name: {self.name!r}")
        if not self.description.strip() or len(self.description) > 2_000:
            raise SkillManifestError(
                "Skill description must be non-empty and at most 2000 characters"
            )
        _validate_version(self.version)
        for field_name, values in (
            ("allowed_tools", self.allowed_tools),
            ("required_permissions", self.required_permissions),
            ("tags", self.tags),
        ):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise SkillManifestError(f"Skill {field_name} must contain non-empty strings")
            if len(set(values)) != len(values):
                raise SkillManifestError(f"Skill {field_name} must contain unique values")

    @classmethod
    def from_mapping(cls, value: object) -> SkillFrontmatter:
        if not isinstance(value, dict):
            raise SkillManifestError("Skill frontmatter must be a mapping")
        if any(not isinstance(key, str) for key in value):
            raise SkillManifestError("Skill frontmatter keys must be strings")
        unknown = set(value) - _KNOWN_FIELDS
        if unknown:
            raise SkillManifestError(f"Unsupported Skill frontmatter fields: {sorted(unknown)}")
        name = value.get("name")
        description = value.get("description")
        version = value.get("version", "1.0.0")
        if not isinstance(name, str) or not isinstance(description, str):
            raise SkillManifestError("Skill frontmatter requires string name and description")
        if not isinstance(version, str):
            raise SkillManifestError("Skill version must be a string")
        return cls(
            name=name,
            description=description,
            version=version,
            allowed_tools=_string_tuple(value.get("allowed_tools", ()), "allowed_tools"),
            required_permissions=_string_tuple(
                value.get("required_permissions", ()), "required_permissions"
            ),
            tags=_string_tuple(value.get("tags", ()), "tags"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "allowed_tools": list(self.allowed_tools),
            "required_permissions": list(self.required_permissions),
            "tags": list(self.tags),
        }


def parse_skill_document(
    raw: bytes, *, max_frontmatter_bytes: int = 64 * 1024
) -> tuple[SkillFrontmatter, str]:
    """Parse frontmatter and return metadata plus body only for an explicit caller."""

    try:
        document = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillManifestError("SKILL.md must be valid UTF-8") from exc
    lines = document.splitlines(keepends=True)
    if not lines or _line_value(lines[0]) != "---":
        raise SkillManifestError("SKILL.md must start with a YAML frontmatter delimiter")
    closing_index: int | None = None
    frontmatter_length = 0
    for index, line in enumerate(lines[1:], start=1):
        if _line_value(line) == "---":
            closing_index = index
            break
        frontmatter_length += len(line.encode("utf-8"))
        if frontmatter_length > max_frontmatter_bytes:
            raise SkillManifestError("SKILL.md frontmatter exceeds the size limit")
    if closing_index is None:
        raise SkillManifestError("SKILL.md frontmatter is missing its closing delimiter")
    frontmatter = SkillFrontmatter.from_mapping(_parse_yaml_subset(lines[1:closing_index]))
    body = "".join(lines[closing_index + 1 :])
    return frontmatter, body


def _parse_yaml_subset(lines: list[str]) -> dict[str, object]:
    """Parse a deliberately small YAML subset without executing YAML tags."""

    result: dict[str, object] = {}
    index = 0
    while index < len(lines):
        text = _line_value(lines[index])
        index += 1
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        if text[0].isspace():
            raise SkillManifestError("Skill frontmatter only supports top-level fields")
        match = _KEY.fullmatch(text)
        if match is None:
            raise SkillManifestError(f"Invalid Skill frontmatter line: {text!r}")
        key, raw_value = match.group(1), match.group(2) or ""
        if key in result:
            raise SkillManifestError(f"Duplicate Skill frontmatter field: {key}")
        scalar_value = _strip_inline_comment(raw_value.strip())
        if scalar_value:
            result[key] = _parse_scalar(scalar_value)
            continue
        values: list[object] = []
        while index < len(lines):
            item_text = _line_value(lines[index])
            if not item_text.strip():
                index += 1
                continue
            item_match = re.fullmatch(r"[ \t]+-[ \t]*(.*)", item_text)
            if item_match is None:
                break
            values.append(_parse_scalar(_strip_inline_comment(item_match.group(1).strip())))
            index += 1
        result[key] = values if values else ""
    return result


def _parse_scalar(value: str) -> object:
    value = _strip_inline_comment(value)
    if value.startswith("[") or value.endswith("]"):
        if not (value.startswith("[") and value.endswith("]")):
            raise SkillManifestError(f"Invalid Skill frontmatter array: {value!r}")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [_parse_scalar(item.strip()) for item in _split_inline_items(value[1:-1])]
        if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
            raise SkillManifestError("Skill frontmatter arrays must contain strings")
        return parsed
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SkillManifestError(f"Invalid quoted Skill value: {value!r}") from exc
        if not isinstance(parsed, str):
            raise SkillManifestError("Quoted Skill values must be strings")
        return parsed
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if value in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[value]
    return value


def _split_inline_items(value: str) -> list[str]:
    if not value.strip():
        return []
    items: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {'"', "'"}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif character == "," and quote is None:
            item = value[start:index].strip()
            if not item:
                raise SkillManifestError("Skill frontmatter arrays cannot contain empty items")
            items.append(item)
            start = index + 1
    if quote is not None:
        raise SkillManifestError("Unterminated quote in Skill frontmatter array")
    item = value[start:].strip()
    if not item:
        raise SkillManifestError("Skill frontmatter arrays cannot contain empty items")
    items.append(item)
    return items


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {'"', "'"}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SkillManifestError(f"Skill {field_name} must be an array of strings")
    values = tuple(value)
    if any(not isinstance(item, str) for item in values):
        raise SkillManifestError(f"Skill {field_name} must be an array of strings")
    return values


def _validate_version(value: str) -> None:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise SkillManifestError(f"Invalid Skill semantic version: {value!r}")
    prerelease = match.group(4) or ""
    if prerelease:
        identifiers = prerelease.split(".")
        if any(
            not identifier
            or (identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"))
            for identifier in identifiers
        ):
            raise SkillManifestError(f"Invalid Skill semantic version: {value!r}")
    build = match.group(5) or ""
    if build and any(not identifier for identifier in build.split(".")):
        raise SkillManifestError(f"Invalid Skill semantic version: {value!r}")


def _line_value(line: str) -> str:
    return line.rstrip("\r\n")


__all__ = ["SKILL_FILENAME", "SkillFrontmatter", "parse_skill_document"]
