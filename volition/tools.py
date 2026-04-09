from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def catalog_text(self, allowed: list[str] | None = None) -> str:
        names = allowed or self.names()
        lines: list[str] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            lines.append(f"- {tool.name}: {tool.description}")
        return "\n".join(lines)

    def specs(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        names = allowed or self.names()
        return [self._tools[name].spec() for name in names if name in self._tools]

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self._tools:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = self._tools[name].handler(args or {})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            return result
        return {"ok": True, "result": json.loads(json.dumps(result, ensure_ascii=False, default=str))}
