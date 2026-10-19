from typing import Any

from app.tools.registry import EmptyInput, Tool


class SystemMetricsTool(Tool[EmptyInput]):
    safety = "read_only"
    checkpoint_fields = ("cpu_percent", "memory_percent", "load_average")
    name = "get_system_metrics"
    description = "获取模拟服务器 CPU、内存使用率和 Load；全部为 Mock Data。"
    input_schema = EmptyInput

    async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
        return {"cpu_percent": 35.2, "memory_percent": 61.4, "load_average": 1.28}
