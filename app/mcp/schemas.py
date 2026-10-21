from app.tools.system_metrics import SystemMetricsTool
from app.tools.redis_diagnostics import RedisDiagnosticsTool
from app.tools.mysql_diagnostics import MySQLDiagnosticsTool

# Server-controlled catalog. Never derive authorization or prompts from discovery.
CATALOG = {
    'get_mock_system_metrics': SystemMetricsTool,
    'get_mock_redis_metrics': RedisDiagnosticsTool,
    'get_mock_mysql_status': MySQLDiagnosticsTool,
}
MAX_RESPONSE_BYTES = 65536
