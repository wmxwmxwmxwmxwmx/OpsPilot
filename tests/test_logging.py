import io
import json
import logging

import httpx

from app.core.logging import JsonFormatter, logger


async def test_structured_events_and_secret_exclusion(client: httpx.AsyncClient) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    try:
        response = await client.post("/api/v1/diagnose?token=query-secret", headers={
            "Authorization": "Bearer auth-secret",
        }, json={"message": "订单接口慢 password=body-secret"})
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert all(secret not in output for secret in ["query-secret", "auth-secret", "body-secret"])
    events = [json.loads(line) for line in output.splitlines()]
    names = [event["event"] for event in events]
    assert names[0] == "request_start" and names[-1] == "request_finish"
    assert names.count("agent_step") == 3 and names.count("tool_success") == 2
    assert "agent_start" in names and "agent_finish" in names
    assert all(event["request_id"] == response.headers["x-request-id"] for event in events)
    assert all(event["latency_ms"] >= 0 for event in events if "latency_ms" in event)
