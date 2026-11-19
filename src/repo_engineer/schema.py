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


def validate_json_schema(value: Any, schema: Any) -> str | None:
    """Return a description of the first violation, or None when value conforms.

    Schemas come from external servers, so any shape is tolerated: a boolean
    schema accepts (true) or refuses (false) everything, and a keyword whose
    value is not of the expected kind is ignored rather than trusted.
    """
    if isinstance(schema, dict) and schema.get("type") == "object" and not isinstance(value, dict):
        return f"arguments must be object, got {type(value).__name__}"
    return _check(value, schema, "")


def _check(value: Any, schema: Any, where: str) -> str | None:
    label = f"'{where}'" if where else "value"
    if schema is False:
        return f"{label} is not allowed"
    if not isinstance(schema, dict):
        return None  # true, or a shape this validator does not interpret
    expected = schema.get("type")
    if expected is not None:
        names = [
            n
            for n in (expected if isinstance(expected, list) else [expected])
            if isinstance(n, str)
        ]
        if names and not any(_is_type(value, name) for name in names):
            return f"{label} must be {' or '.join(names)}, got {type(value).__name__}"
    if "const" in schema and value != schema["const"]:
        return f"{label} must be {schema['const']!r}"
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{label} must be one of {enum!r}"
    if isinstance(value, str):
        longest, shortest = _number(schema, "maxLength"), _number(schema, "minLength")
        if longest is not None and len(value) > longest:
            return f"{label} is longer than {schema['maxLength']} characters"
        if shortest is not None and len(value) < shortest:
            return f"{label} is shorter than {schema['minLength']} characters"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = _number(schema, "minimum"), _number(schema, "maximum")
        if low is not None and value < low:
            return f"{label} is below the minimum {schema['minimum']}"
        if high is not None and value > high:
            return f"{label} is above the maximum {schema['maximum']}"
    if isinstance(value, list):
        return _check_array(value, schema, where, label)
    if isinstance(value, dict):
        return _check_object(value, schema, where)
    return None


def _number(schema: dict[str, Any], key: str) -> float | None:
    value = schema.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _check_array(value: list[Any], schema: dict[str, Any], where: str, label: str) -> str | None:
    most, fewest = _number(schema, "maxItems"), _number(schema, "minItems")
    if most is not None and len(value) > most:
        return f"{label} has more than {schema['maxItems']} items"
    if fewest is not None and len(value) < fewest:
        return f"{label} has fewer than {schema['minItems']} items"
    if "items" in schema:
        for index, item in enumerate(value):
            error = _check(item, schema["items"], f"{where}[{index}]")
            if error:
                return error
    return None


def _check_object(value: dict[str, Any], schema: dict[str, Any], where: str) -> str | None:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    prefix = f"{where}." if where else ""
    for name in required if isinstance(required, list) else []:
        if isinstance(name, str) and name not in value:
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
