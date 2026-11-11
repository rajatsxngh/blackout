"""Strict JSON schemas for structured output and strict tools (docs/PRD.md 8.8).

Strict mode (``strict: true`` on a tool, or ``output_config.format``) needs every object to
set ``additionalProperties: false`` and list all its properties in ``required``. Pydantic's
schemas also use ``$defs``/``$ref``, which ``strict_schema`` inlines so each schema stands
alone, and carry ``title``/``default`` annotations, which it drops (an optional field stays
optional by allowing ``null``). Strict mode also rejects numeric, string-length and array-size
constraints (``minimum``, ``maxLength``, ``minItems``...), so those are dropped too: the caller
validates them client-side (the agents build the Pydantic action from the input, M11).
"""

import copy
from typing import Any

from pydantic import BaseModel

_DROP = frozenset(
    {
        "title",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)


def strict_schema(source: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
    """A strict, self-contained copy of a JSON schema (or of a Pydantic model's)."""
    schema = (
        source.model_json_schema()
        if isinstance(source, type) and issubclass(source, BaseModel)
        else copy.deepcopy(source)
    )
    defs = schema.pop("$defs", {})
    return _strict(schema, defs, ())


def _strict(node: Any, defs: dict[str, Any], seen: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_strict(item, defs, seen) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.removeprefix("#/$defs/")
        if name in seen:
            raise ValueError(f"recursive schema through {name!r} can't be inlined")
        if name not in defs:
            raise ValueError(f"unresolved $ref {ref!r}")
        merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
        return _strict(merged, defs, (*seen, name))
    out = {
        k: _strict(v, defs, seen) for k, v in node.items() if k not in _DROP and k != "properties"
    }
    if node.get("type") == "object" or "properties" in node:
        # Property names are data, not schema keywords: never drop one.
        props = {k: _strict(v, defs, seen) for k, v in node.get("properties", {}).items()}
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def strict_tool(name: str, description: str, source: type[BaseModel] | dict[str, Any]) -> dict:
    """A strict tool definition (schema-valid inputs guaranteed)."""
    return {
        "name": name,
        "description": description,
        "input_schema": strict_schema(source),
        "strict": True,
    }


def output_format(source: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
    """An ``output_config.format`` value for structured output."""
    return {"type": "json_schema", "schema": strict_schema(source)}
