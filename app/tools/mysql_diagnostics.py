from typing import Any

from app.tools.registry import EmptyInput, Tool


class MySQLDiagnosticsTool(Tool[EmptyInput]):
    name = "get_mysql_slow_queries"
    description = "获取模拟 MySQL 慢 SQL、耗时和扫描行数；全部为 Mock Data。"
    input_schema = EmptyInput

    async def execute(self, arguments: EmptyInput) -> dict[str, Any]:
        return {"slow_queries": [{
            "sql": "SELECT * FROM orders WHERE user_id = ?",
            "duration_ms": 2310,
            "rows_examined": 1700000,
            "rows_returned": 18,
        }]}
