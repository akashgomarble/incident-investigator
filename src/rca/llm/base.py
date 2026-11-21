"""Provider-neutral LLM interface.  Agents depend only on this."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None      # set when the model emitted invalid JSON arguments


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    raw_message: dict[str, Any] = field(default_factory=dict)   # assistant message to append to history


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                       role: str = "", meta: dict[str, Any] | None = None) -> LLMResponse: ...


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten $defs/$ref and drop noise so the schema is accepted by every provider."""
    defs = schema.get("$defs", {})

    def walk(node: Any, is_property_map: bool = False) -> Any:
        if isinstance(node, dict):
            if is_property_map:                      # keys here are field names (one is literally 'title')
                return {k: walk(v) for k, v in node.items()}
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {k: walk(v, k == "properties") for k, v in node.items() if k not in ("$defs", "title", "default")}
            if "anyOf" in out:                       # Optional[X] -> X (nullable handled by 'required')
                non_null = [o for o in out["anyOf"] if o.get("type") != "null"]
                if len(non_null) == 1:
                    rest = {k: v for k, v in out.items() if k != "anyOf"}
                    return {**non_null[0], **rest}
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": inline_refs(parameters)}}
