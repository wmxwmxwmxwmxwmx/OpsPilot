from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

InputT = TypeVar("InputT", bound=BaseModel)


class EmptyInput(BaseModel):
    """These V0.1 tools accept only an empty JSON object."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Tool(ABC, Generic[InputT]):
    name: str
    description: str
    input_schema: type[InputT]

    @abstractmethod
    async def execute(self, arguments: InputT) -> dict[str, Any]:
        """Execute validated input and return JSON-serializable evidence."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[Any]] = {}

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, tool_name: str) -> Tool[Any] | None:
        return self._tools.get(tool_name)

    def list_tools(self) -> list[Tool[Any]]:
        return list(self._tools.values())


# class Tool:
#     name = ...
#     description = ...
#     input_schema = ...

#     async def execute(self, arguments):
#         raise NotImplementedError


# class ToolRegistry:
#     def __init__(self):
#         self._tools = {}

#     def register(self, tool):
#         if tool.name in self._tools:
#             raise ValueError()

#         self._tools[tool.name] = tool

#     def get(self, name):
#         return self._tools.get(name)

#     def list_tools(self):
#         return list(self._tools.values())