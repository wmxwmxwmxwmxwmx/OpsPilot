import pytest

from app.main import build_registry
from app.tools.registry import ToolRegistry
from app.tools.system_metrics import SystemMetricsTool


def test_register_get_and_list() -> None:
    registry = ToolRegistry()
    tool = SystemMetricsTool()
    registry.register(tool)
    assert registry.get(tool.name) is tool
    assert registry.get("missing") is None
    assert registry.list_tools() == [tool]
    snapshot = registry.list_tools()
    snapshot.clear()
    assert registry.get(tool.name) is tool


def test_duplicate_registration_rejected() -> None:
    registry = ToolRegistry()
    registry.register(SystemMetricsTool())
    with pytest.raises(ValueError, match="Duplicate tool"):
        registry.register(SystemMetricsTool())


def test_all_default_tools_have_schemas() -> None:
    tools = build_registry().list_tools()
    assert {tool.name for tool in tools} == {
        "get_system_metrics", "get_mysql_slow_queries", "get_redis_metrics",
    }
    for tool in tools:
        assert tool.description
        assert tool.input_schema.model_json_schema()["additionalProperties"] is False
