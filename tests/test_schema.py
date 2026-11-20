"""Tool schemas: generated from ToolSpec, and the subset validator for external tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from repo_engineer.schema import validate_json_schema
from repo_engineer.state import ToolCall
from repo_engineer.tools import Permission, ToolRegistry, ToolSpec


def test_the_schema_says_what_validate_enforces(workspace: Path) -> None:
    spec = ToolRegistry(workspace).specs["read_file"]
    schema = spec.json_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["required"] == ["path"]
    assert schema["properties"]["path"]["type"] == "string"
    assert schema["properties"]["start_line"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 10_000_000,
        "description": spec.arg_docs["start_line"],
    }


def test_every_registered_tool_documents_every_argument(workspace: Path) -> None:
    registry = ToolRegistry(workspace)
    for spec in registry.specs.values():
        names = set(spec.required_args) | set(spec.optional_args)
        assert set(spec.arg_docs) == names, spec.name
        assert spec.description


def test_string_caps_become_max_length(workspace: Path) -> None:
    schema = ToolRegistry(workspace).specs["search"].json_schema()
    assert schema["properties"]["pattern"]["maxLength"] == 512


def test_registry_publishes_schemas_for_its_tools(workspace: Path) -> None:
    schemas = ToolRegistry(workspace).tool_schemas()
    assert {s.name for s in schemas} >= {"list_tree", "read_file", "run_tests"}
    read = next(s for s in schemas if s.name == "read_file")
    assert read.parameters["required"] == ["path"]


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 10},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "flag": {"type": "boolean"},
        "ratio": {"type": "number"},
        "nested": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"path": "a.py"}, None),
        ({"path": "a.py", "count": 5, "mode": "b", "tags": ["x"], "flag": True}, None),
        ({"path": "a.py", "ratio": 1, "nested": {"x": 1}}, None),
        ({}, "missing required property 'path'"),
        ({"path": 3}, "'path' must be string"),
        ({"path": "a" * 11}, "'path' is longer than 10"),
        ({"path": "a", "count": 0}, "'count' is below the minimum 1"),
        ({"path": "a", "count": True}, "'count' must be integer"),
        ({"path": "a", "count": 2.5}, "'count' must be integer"),
        ({"path": "a", "mode": "c"}, "'mode' must be one of"),
        ({"path": "a", "tags": ["x", "y", "z"]}, "'tags' has more than 2 items"),
        ({"path": "a", "tags": [1]}, "'tags[0]' must be string"),
        ({"path": "a", "nested": {}}, "missing required property 'nested.x'"),
        ({"path": "a", "extra": 1}, "unknown property 'extra'"),
        ([], "arguments must be object"),
    ],
)
def test_subset_validator(value: Any, problem: str | None) -> None:
    error = validate_json_schema(value, SCHEMA)
    if problem is None:
        assert error is None
    else:
        assert error is not None and problem in error


def test_a_spec_with_its_own_schema_validates_with_it(workspace: Path) -> None:
    registry = ToolRegistry(workspace)
    calls: list[dict[str, Any]] = []
    registry.register(
        ToolSpec(
            name="ext",
            description="external",
            permission=Permission.READ_ONLY,
            required_args={},
            run=lambda **kwargs: calls.append(kwargs) or "done",
            schema={
                "type": "object",
                "properties": {"n": {"type": "integer", "maximum": 3}},
                "required": ["n"],
            },
        )
    )
    rejected = registry.execute(ToolCall("ext", {"n": 9}))
    assert rejected.rejected and "maximum" in (rejected.error or "")
    accepted = registry.execute(ToolCall("ext", {"n": 2}))
    assert accepted.ok and calls == [{"n": 2}]


@pytest.mark.parametrize(
    ("schema", "args"),
    [
        ({"type": "object", "properties": {"note": True}}, {"note": "x"}),
        ({"type": "object", "properties": {"note": False}}, {"note": "x"}),
        ({"type": "object", "required": "note"}, {}),
        ({"type": "object", "properties": {"n": {"minimum": "zero"}}}, {"n": 1}),
        ({"type": "object", "properties": {"n": {"items": True, "type": "array"}}}, {"n": [1]}),
    ],
)
def test_odd_external_schemas_never_crash_execute(
    workspace: Path, schema: dict[str, Any], args: dict[str, Any]
) -> None:
    """Found by the Step 2 review: a boolean subschema crashed the runtime loop."""
    registry = ToolRegistry(workspace)
    registry.register(
        ToolSpec(
            name="ext",
            description="external",
            permission=Permission.READ_ONLY,
            required_args={},
            run=lambda **kwargs: "ran",
            schema=schema,
        )
    )
    obs = registry.execute(ToolCall("ext", args))
    assert isinstance(obs.ok, bool)
    if not obs.ok:
        assert obs.rejected


def test_a_false_subschema_rejects_the_property() -> None:
    error = validate_json_schema({"note": "x"}, {"type": "object", "properties": {"note": False}})
    assert error is not None and "'note'" in error
