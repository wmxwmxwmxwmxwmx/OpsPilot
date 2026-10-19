from typing import Any

from app.tools.registry import EmptyInput, Tool


class RedisDiagnosticsTool(Tool[EmptyInput]):
    safety = "read_only"
    checkpoint_fields = ("used_memory_mb", "hit_rate", "connected_clients", "blocked_clients")
    name = "get_redis_metrics"
    description = "获取模拟 Redis 内存、命中率、连接数和阻塞数；全部为 Mock Data。"
    input_schema = EmptyInput

    async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
        return {"used_memory_mb": 312, "hit_rate": 0.96,
                "connected_clients": 84, "blocked_clients": 0}
