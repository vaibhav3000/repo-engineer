"""Tool schemas: what a model is told about a tool, and checking arguments against it.

Built-in tools derive their JSON Schema from the same ToolSpec fields that
ToolSpec.validate enforces, so the two cannot drift apart. External tools (from
MCP servers) bring their own schema; validate_json_schema checks arguments
against the subset of JSON Schema that tool schemas use (type, properties,
required, additionalProperties, enum, const, numeric and length bounds, array
items). Keywords outside that subset are ignored here; the external server
validates its own input as well.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSchema:
    """A tool as a model sees it: name, description and a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def validate_json_schema(value: Any, schema: dict[str, Any]) -> str | None:
    """Return a description of the first violation, or None when value conforms."""
    if schema.get("type") == "object" and not isinstance(value, dict):
        return f"arguments must be object, got {type(value).__name__}"
    return _check(value, schema, "")


def _check(value: Any, schema: dict[str, Any], where: str) -> str | None:
    label = f"'{where}'" if where else "value"
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, name) for name in names):
            return f"{label} must be {' or '.join(names)}, got {type(value).__name__}"
    if "const" in schema and value != schema["const"]:
        return f"{label} must be {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{label} must be one of {schema['enum']!r}"
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{label} is longer than {schema['maxLength']} characters"
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{label} is shorter than {schema['minLength']} characters"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{label} is below the minimum {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{label} is above the maximum {schema['maximum']}"
    if isinstance(value, list):
        return _check_array(value, schema, where, label)
    if isinstance(value, dict):
        return _check_object(value, schema, where)
    return None


def _check_array(value: list[Any], schema: dict[str, Any], where: str, label: str) -> str | None:
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        return f"{label} has more than {schema['maxItems']} items"
    if "minItems" in schema and len(value) < schema["minItems"]:
        return f"{label} has fewer than {schema['minItems']} items"
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            error = _check(item, items, f"{where}[{index}]")
            if error:
                return error
    return None


def _check_object(value: dict[str, Any], schema: dict[str, Any], where: str) -> str | None:
    properties: dict[str, Any] = schema.get("properties", {})
    prefix = f"{where}." if where else ""
    for name in schema.get("required", []):
        if name not in value:
            return f"missing required property '{prefix}{name}'"
    for name, item in value.items():
        if name in properties:
            error = _check(item, properties[name], f"{prefix}{name}")
            if error:
                return error
        elif schema.get("additionalProperties") is False:
            return f"unknown property '{prefix}{name}'"
    return None


def _is_type(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    if name == "integer" and isinstance(value, float):
        return False  # 2.0 is a number, not an integer argument
    return isinstance(value, _TYPES.get(name, (object,)))
