import pytest
from pydantic import ValidationError

from app.tools.mysql_diagnostics import MySQLDiagnosticsTool
from app.tools.redis_diagnostics import RedisDiagnosticsTool
from app.tools.registry import EmptyInput
from app.tools.system_metrics import SystemMetricsTool


async def test_system_metrics() -> None:
    assert await SystemMetricsTool().execute(EmptyInput()) == {
        "cpu_percent": 35.2, "memory_percent": 61.4, "load_average": 1.28,
    }


async def test_mysql_slow_queries() -> None:
    assert await MySQLDiagnosticsTool().execute(EmptyInput()) == {"slow_queries": [{
        "sql": "SELECT * FROM orders WHERE user_id = ?", "duration_ms": 2310,
        "rows_examined": 1700000, "rows_returned": 18,
    }]}


async def test_redis_metrics() -> None:
    assert await RedisDiagnosticsTool().execute(EmptyInput()) == {
        "used_memory_mb": 312, "hit_rate": 0.96, "connected_clients": 84, "blocked_clients": 0,
    }


def test_tools_reject_unexpected_arguments() -> None:
    with pytest.raises(ValidationError):
        EmptyInput.model_validate({"password": "do-not-echo"})
