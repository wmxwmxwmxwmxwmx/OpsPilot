"""Real AsyncPostgresSaver and business DB; no in-memory persistence substitute."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.main import build_registry, create_app
from app.tools.mock_remediation import MockRemediationTool
from app.workflow.checkpoint import open_checkpointer
from app.workflow.errors import WorkflowError
from app.workflow.graph import build_graph
from app.workflow.mock import WorkflowMockLLM
from app.workflow.nodes import WorkflowNodes
from app.workflow.privacy import CheckpointPrivacy
from app.workflow.repository import WorkflowRepository
from app.workflow.service import WorkflowService

pytestmark = pytest.mark.postgres_integration
TOKEN = "local-test-token-" + "x" * 32


def checkpoint_url(url):
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def settings(url, **kwargs):
    return Settings(_env_file=None, workflow_enabled=True, workflow_local_demo=True,
        workflow_api_token=TOKEN, database_url=url, checkpoint_database_url=checkpoint_url(url), **kwargs)


@asynccontextmanager
async def service(database, *, registry=None, llm=None, sessions=None):
    engine, url = database
    repository = WorkflowRepository(engine)
    if registry is None:
        registry = build_registry()
        registry.register(MockRemediationTool(repository))
    async with open_checkpointer(checkpoint_url(url)) as saver:
        await saver.setup()
        privacy = CheckpointPrivacy((TOKEN, "configured-secret"))
        graph = build_graph(WorkflowNodes(llm or WorkflowMockLLM(), registry, privacy), saver)
        yield WorkflowService(graph, repository, privacy, sessions=sessions)


async def counts(engine):
    async with engine.connect() as connection:
        return (await connection.scalar(text("SELECT count(*) FROM incident_records")),
                await connection.scalar(text("SELECT count(*) FROM workflow_mock_actions")))


async def test_m2_setup_checkpoint_and_new_connection_recovery(database):
    async with service(database) as svc:
        wid = await svc.create("mock remediation password=private")
        state = await svc.run(wid)
        assert state["status"] == "pending_approval"
        assert await counts(database[0]) == (0, 0)
        snapshot = await svc.snapshot(wid)
        assert snapshot.config["configurable"]["thread_id"] == wid
        assert "private" not in json.dumps(snapshot.values)
    # Fresh saver, pool and compiled graph, same thread in PostgreSQL.
    async with service(database) as svc:
        assert (await svc.status(wid))["status"] == "pending_approval"
        snap = await svc.snapshot(wid)
        assert snap.next == ("human_approval",) and snap.tasks[0].interrupts
        from postgres_support import migrate
        async with database[0].connect() as conn:
            await conn.run_sync(lambda c: migrate(c, "check"))


async def test_m3_failed_node_resume_no_reexecution(database):
    registry = build_registry()
    calls = []
    original = registry.get("get_mysql_slow_queries").execute
    async def broken(args):
        calls.append("broken")
        raise RuntimeError("configured-secret")
    registry.get("get_mysql_slow_queries").execute = broken
    async with service(database, registry=registry) as svc:
        wid = await svc.create("订单慢")
        failed = await svc.run(wid)
        assert failed["status"] == "failed" and failed["current_step"] == "execute_tool"
        assert len((await svc.snapshot(wid)).values["tool_results"]) == 1
    registry.get("get_mysql_slow_queries").execute = original
    async def no_replay(args):
        pytest.fail("First tool must not replay")
    registry.get("get_system_metrics").execute = no_replay
    async with service(database, registry=registry) as svc:
        events = [e async for e in svc.stream(wid)]
        assert (await svc.status(wid))["status"] == "completed"
        assert [e.tool for e in events if e.type == "tool_result"] == ["get_mysql_slow_queries"]
        assert not any(e.type == "tool_call" for e in events)
        with pytest.raises(WorkflowError, match="workflow_terminal"):
            await svc.run(wid)
    assert len(calls) == 1 and await counts(database[0]) == (1, 0)


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_m4_approval_restart_and_duplicate_requests(database, decision):
    async with service(database) as svc:
        wid = await svc.create("mock remediation")
        paused = await svc.run(wid)
        approval = paused["pending_approval"]["approval_id"]
        assert await counts(database[0]) == (0, 0)
    async with service(database) as svc:
        with pytest.raises(WorkflowError, match="invalid_approval"):
            await svc.run(wid, action="approve", approval={"approval_id": "bad", "decision": "approve"})
        events = [e async for e in svc.stream(wid, action="approve", approval={"approval_id": approval, "decision": decision})]
        assert [e.type for e in events] == ["workflow_resumed", "tool_result", "final", "workflow_completed"]
        assert next(e for e in events if e.type == "tool_result").success == (decision == "approve")
        with pytest.raises(WorkflowError, match="workflow_terminal"):
            await svc.run(wid, action="approve", approval={"approval_id": approval, "decision": decision})
    assert await counts(database[0]) == (1, 1 if decision == "approve" else 0)


async def test_action_commit_ack_lost_then_explicit_resume_is_idempotent(database, monkeypatch):
    repository = WorkflowRepository(database[0])
    registry = build_registry()
    registry.register(MockRemediationTool(repository))
    original, calls = repository.record_action, []
    async def lost(*args):
        result = await original(*args)
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("ack lost")
        return result
    monkeypatch.setattr(repository, "record_action", lost)
    async with service(database, registry=registry) as svc:
        wid = await svc.create("mock remediation")
        approval = (await svc.run(wid))["pending_approval"]["approval_id"]
        result = await svc.run(wid, action="approve", approval={"approval_id": approval, "decision": "approve"})
        assert result["status"] == "failed" and result["error_code"] == "action_outcome_unknown"
        assert len(calls) == 1  # Side effects are not automatically retried.
        assert await counts(database[0]) == (0, 1)
    async with service(database, registry=registry) as svc:
        assert (await svc.run(wid))["status"] == "completed"
    assert await counts(database[0]) == (1, 1) and len(calls) == 2


async def test_business_commit_failure_resumes_only_commit(database, monkeypatch):
    async with service(database) as svc:
        wid = await svc.create("订单慢")
        complete = svc.repository.complete
        async def fail(*args):
            raise ConnectionError("database unavailable")
        monkeypatch.setattr(svc.repository, "complete", fail)
        assert (await svc.run(wid))["status"] == "failed"
        snap = await svc.snapshot(wid)
        assert not snap.next and snap.values["status"] == "graph_completed"
        monkeypatch.setattr(svc.repository, "complete", complete)
        async def never(*args, **kwargs):
            pytest.fail("Completed graph must not run for business retry")
        monkeypatch.setattr(svc.graph, "astream", never)
        assert (await svc.run(wid))["status"] == "completed"
    assert await counts(database[0]) == (1, 0)


async def test_lost_final_commit_response_reuses_stable_incident(database, monkeypatch):
    async with service(database) as svc:
        original = svc.repository.complete
        async def lost(*args):
            await original(*args)
            raise ConnectionError("lost acknowledgement")
        monkeypatch.setattr(svc.repository, "complete", lost)
        wid = await svc.create("订单慢")
        assert (await svc.run(wid))["status"] == "completed"
        with pytest.raises(WorkflowError, match="workflow_terminal"):
            await svc.run(wid)
    assert await counts(database[0]) == (1, 0)


async def test_advisory_mutex_across_independent_engines(database):
    from app.db.session import create_database_engine
    engine2 = create_database_engine(database[1])
    try:
        async with service(database) as svc:
            wid = await svc.create("订单慢")
            async with svc.repository.lock(wid):
                with pytest.raises(WorkflowError, match="workflow_busy"):
                    async with WorkflowRepository(engine2).lock(wid):
                        pytest.fail("Concurrent owner entered")
            async with WorkflowRepository(engine2).lock(wid):
                pass
    finally:
        await engine2.dispose()
    assert database[0].pool.checkedout() == 0


async def test_api_auth_sse_and_summary_privacy(database):
    _, url = database
    async with open_checkpointer(checkpoint_url(url)) as saver:
        await saver.setup()
    app = create_app(settings(url))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/v1/workflows", json={"message": "hi"})).status_code == 403
            client.headers["Authorization"] = "Bearer " + TOKEN
            response = await client.post("/api/v1/workflows?stream=true", json={"message": "mock remediation"})
            assert "event: workflow_start" in response.text and "event: approval_required" in response.text
            assert "event: final" not in response.text
            first = json.loads(response.text.split("data: ", 1)[1].split("\n")[0])
            wid = first["workflow_id"]
            status = (await client.get(f"/api/v1/workflows/{wid}")).json()
            assert "llm_messages" not in status and "initial_state" not in status
            approval = status["pending_approval"]["approval_id"]
            resumed = await client.post(f"/api/v1/workflows/{wid}/approval?stream=true",
                                       json={"approval_id": approval, "decision": "approve"})
            assert "event: final" in resumed.text and "event: workflow_completed" in resumed.text
            assert "event: tool_call" not in resumed.text  # no historical replay
            assert (await client.post(f"/api/v1/workflows/{wid}/approval", json={
                "approval_id": approval, "decision": "approve"})).status_code == 409
            assert (await client.post("/api/v1/diagnose", json={"message": "订单慢"})).status_code == 200


async def test_real_process_exit_then_approval_in_new_process(database):
    # Two actual Python processes, separate engines/pools, no Python object reuse.
    _, url = database
    async with open_checkpointer(checkpoint_url(url)) as saver:
        await saver.setup()
    script = r'''
import asyncio, json, os
from app.core.config import Settings
from app.db.session import create_database_engine
from app.workflow.runtime import workflow_service
async def main():
    settings = Settings(_env_file=None)
    engine = create_database_engine(settings.database_url.get_secret_value())
    try:
        async with workflow_service(settings, engine) as svc:
            if "WORKFLOW_ID" not in os.environ:
                wid = await svc.create("mock remediation")
                result = await svc.run(wid)
            else:
                wid = os.environ["WORKFLOW_ID"]
                pending = (await svc.status(wid))["pending_approval"]
                result = await svc.run(wid, action="approve", approval={"approval_id": pending["approval_id"], "decision":"approve"})
            print(json.dumps(result))
    finally:
        await engine.dispose()
asyncio.run(main())
'''
    env = {**os.environ, "WORKFLOW_ENABLED": "true", "WORKFLOW_LOCAL_DEMO": "true", "WORKFLOW_API_TOKEN": TOKEN,
           "DATABASE_URL": url, "CHECKPOINT_DATABASE_URL": checkpoint_url(url), "LLM_MODE": "mock"}
    async def child():
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
        assert proc.returncode == 0, stderr.decode()
        return json.loads(stdout.decode().strip().splitlines()[-1])
    first = await child()
    assert first["status"] == "pending_approval" and await counts(database[0]) == (0, 0)
    env["WORKFLOW_ID"] = first["workflow_id"]
    second = await child()
    assert second["status"] == "completed" and second["workflow_id"] == first["workflow_id"]
    assert await counts(database[0]) == (1, 1)


@pytest.mark.redis_integration
@pytest.mark.parametrize("scenario", ["normal", "expired", "conflict", "unknown", "business_failure"])
async def test_real_session_resume_boundaries(database, scenario, monkeypatch):
    from redis.asyncio import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError
    from app.session.repository import SessionRepository
    from app.workflow.session_commit import receipt_key
    url = os.environ.get("OPSPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set OPSPILOT_TEST_REDIS_URL for dual-store tests")
    redis = Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    sessions = SessionRepository(redis)
    snapshot = await sessions.create()
    wid = None
    try:
        async with service(database, sessions=sessions) as svc:
            wid = await svc.create("mock remediation", snapshot.session_id)
            paused = await svc.run(wid)
        if scenario == "expired":
            await redis.pexpire(sessions.key(snapshot.session_id), 0)
        elif scenario == "conflict":
            await sessions.commit(snapshot, "another question", "newer answer")
        async with service(database, sessions=sessions) as svc:
            with monkeypatch.context() as scoped:
                if scenario == "unknown":
                    from redis.asyncio.client import Pipeline
                    execute = Pipeline.execute
                    async def lost(self, *args, **kwargs):
                        await execute(self, *args, **kwargs)
                        raise RedisConnectionError("lost acknowledgement")
                    scoped.setattr(Pipeline, "execute", lost)
                elif scenario == "business_failure":
                    async def fail(*args):
                        raise ConnectionError("PG unavailable")
                    scoped.setattr(svc.repository, "complete", fail)
                result = await svc.run(wid, action="approve", approval={
                    "approval_id": paused["pending_approval"]["approval_id"], "decision": "approve"})
            if scenario in {"unknown", "business_failure"}:
                assert result["status"] == "failed"
                assert await counts(database[0]) == (0, 1)
                result = await svc.run(wid)
            assert result["status"] == "completed"
            assert result["session_status"] == ({"expired": "expired", "conflict": "conflict"}.get(scenario, "committed"))
            if scenario == "expired":
                assert not await redis.exists(sessions.key(snapshot.session_id))
            else:
                current = await sessions.load(snapshot.session_id)
                assert current.version == 1 and len(current.messages) == 2
                if scenario == "conflict":
                    assert current.messages[-1].content == "newer answer"
            assert await counts(database[0]) == (1, 1)
    finally:
        await redis.delete(sessions.key(snapshot.session_id))
        if wid:
            await redis.delete(receipt_key(wid))
        await redis.aclose()


@pytest.mark.parametrize("phase", ["plan", "tool", "commit"])
async def test_sse_disconnect_releases_io_pool_lock_and_keeps_checkpoint(database, phase, monkeypatch):
    from app.api.sse import SSEResponse
    from app.api.workflows import stream_events
    entered, cleaned = asyncio.Event(), asyncio.Event()
    registry = build_registry()
    class WaitingLLM(WorkflowMockLLM):
        async def generate(self, messages, tools):
            if phase == "plan":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return await super().generate(messages, tools)
    async def waiting(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    if phase == "tool":
        registry.get("get_system_metrics").execute = waiting
    async with service(database, registry=registry, llm=WaitingLLM()) as svc:
        if phase == "commit":
            monkeypatch.setattr(svc.repository, "complete", waiting)
        wid = await svc.create("订单慢")
        sent = []
        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            sent.append(message)
        response = SSEResponse(stream_events(svc, wid))
        await asyncio.wait_for(response({"type": "http"}, receive, send), 5)
        assert cleaned.is_set()
        body = b"".join(item.get("body", b"") for item in sent)
        assert b"event: final" not in body and b"event: workflow_completed" not in body
        assert (await svc.snapshot(wid)).values
        assert (await svc.status(wid))["status"] == "failed"
        async with svc.repository.lock(wid):
            pass
    assert database[0].pool.checkedout() == 0
    async with service(database) as svc:
        assert (await svc.run(wid))["status"] == "completed"
    assert await counts(database[0]) == (1, 0)


async def test_two_approvals_only_one_owner(database):
    async with service(database) as first, service(database) as second:
        wid = await first.create("mock remediation")
        paused = await first.run(wid)
        args = dict(action="approve", approval={"approval_id": paused["pending_approval"]["approval_id"], "decision": "approve"})
        results = await asyncio.gather(first.run(wid, **args), second.run(wid, **args), return_exceptions=True)
        assert sum(isinstance(r, dict) and r["status"] == "completed" for r in results) == 1
        assert sum(isinstance(r, WorkflowError) and r.code in {"workflow_busy", "workflow_terminal"} for r in results) == 1
    assert await counts(database[0]) == (1, 1)


async def test_checkpoint_prune_preserves_incident_and_pending_approval(database):
    from app.cli.checkpoints import CheckpointSettings, run
    async with service(database) as svc:
        done = await svc.create("订单慢")
        await svc.run(done)
        pending = await svc.create("mock remediation")
        await svc.run(pending)
    async with database[0].begin() as conn:
        await conn.execute(text("UPDATE workflows SET updated_at = now() - interval '10 days'"))
    await run("prune", CheckpointSettings(_env_file=None, database_url=database[1],
        checkpoint_database_url=checkpoint_url(database[1])))
    async with service(database) as svc:
        assert (await svc.status(done))["checkpoint_pruned"] is True
        assert not (await svc.snapshot(done)).values
        assert (await svc.status(pending))["status"] == "pending_approval"
    assert await counts(database[0]) == (1, 0)


async def test_hard_process_crash_after_action_before_checkpoint(database):
    _, url = database
    async with open_checkpointer(checkpoint_url(url)) as saver:
        await saver.setup()
    script = r'''
import asyncio, json, os
from app.core.config import Settings
from app.db.session import create_database_engine
from app.workflow.runtime import workflow_service
from app.workflow.repository import WorkflowRepository
original = WorkflowRepository.record_action
async def crash_window(self, *args):
    await original(self, *args)
    print("action-committed", flush=True)
    await asyncio.Event().wait()
WorkflowRepository.record_action = crash_window
async def main():
    settings = Settings(_env_file=None)
    engine = create_database_engine(settings.database_url.get_secret_value())
    async with workflow_service(settings, engine) as svc:
        wid = await svc.create("mock remediation")
        paused = await svc.run(wid)
        print(wid, flush=True)
        await svc.run(wid, action="approve", approval={"approval_id":paused["pending_approval"]["approval_id"], "decision":"approve"})
asyncio.run(main())
'''
    env = {**os.environ, "WORKFLOW_ENABLED": "true", "WORKFLOW_LOCAL_DEMO": "true", "WORKFLOW_API_TOKEN": TOKEN,
           "DATABASE_URL": url, "CHECKPOINT_DATABASE_URL": checkpoint_url(url), "LLM_MODE": "mock"}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        wid = (await asyncio.wait_for(proc.stdout.readline(), 10)).decode().strip()
        assert UUID(wid).version == 4
        assert (await asyncio.wait_for(proc.stdout.readline(), 10)).decode().strip() == "action-committed"
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    assert await counts(database[0]) == (0, 1)
    async with service(database) as svc:
        assert (await svc.repository.get(wid))["status"] == "running"
        assert (await svc.snapshot(wid)).next == ("execute_tool",)
        assert (await svc.run(wid))["status"] == "completed"
    assert await counts(database[0]) == (1, 1)


async def test_resume_uses_none_even_for_checkpoint_with_empty_values(database, monkeypatch):
    async with service(database) as svc:
        wid = await svc.create("订单慢")
        # An input checkpoint can exist before channels contain GraphState. The
        # existence test must use checkpoint metadata, not truthiness of values.
        missing = await svc.snapshot(wid)
        empty_checkpoint = missing._replace(metadata={"source": "input", "step": -1}, next=("__start__",))
        snapshot = svc.snapshot
        reads = 0
        async def first_empty(workflow_id):
            nonlocal reads
            reads += 1
            return empty_checkpoint if reads == 1 else await snapshot(workflow_id)
        monkeypatch.setattr(svc, "snapshot", first_empty)
        inputs = []
        async def capture(graph_input, *args, **kwargs):
            inputs.append(graph_input)
            if False:
                yield {}
        monkeypatch.setattr(svc.graph, "astream", capture)
        await svc.run(wid)
        assert inputs == [None]


async def test_database_checkpoint_payloads_exclude_raw_secrets(database):
    from app.llm.base import LLMResponse, ToolCall
    class SecretLLM:
        async def generate(self, messages, tools):
            return LLMResponse(content="private reasoning configured-secret", tool_calls=[
                ToolCall(id="configured-secret", name="get_system_metrics",
                         arguments='{"password":"private-argument"}')])
    async with service(database, llm=SecretLLM()) as svc:
        wid = await svc.create("question password=private-input configured-secret")
        assert (await svc.run(wid))["status"] == "failed"
        async with database[0].connect() as conn:
            blobs = (await conn.execute(text("SELECT blob FROM checkpoint_blobs WHERE blob IS NOT NULL"))).scalars().all()
            blobs += (await conn.execute(text("SELECT blob FROM checkpoint_writes WHERE blob IS NOT NULL"))).scalars().all()
            assert blobs
            for blob in blobs:
                assert all(secret not in bytes(blob) for secret in
                    (b"configured-secret", b"private-argument", b"private-input", b"private reasoning"))
            initial = await conn.scalar(text("SELECT initial_state::text FROM workflows"))
            assert "configured-secret" not in initial and "private-input" not in initial


async def test_unexpected_schema_version_blocks_resume(database):
    async with service(database) as svc:
        wid = await svc.create("mock remediation")
        await svc.run(wid)
        await svc.graph.aupdate_state(svc.config(wid), {"schema_version": 99})
        with pytest.raises(WorkflowError, match="checkpoint_version_unsupported"):
            await svc.run(wid)
        assert await counts(database[0]) == (0, 0)
