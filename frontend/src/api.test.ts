import { afterEach, describe, expect, it, vi } from "vitest";
import { readSSE, stream, traceURL } from "./api";
import { conversation, eventUpdate } from "./state";

const body = (parts: Uint8Array[]) =>
  new ReadableStream<Uint8Array>({
    start(c) {
      parts.forEach((p) => c.enqueue(p));
      c.close();
    },
  });
const bytes = (text: string) => new TextEncoder().encode(text);
afterEach(() => vi.unstubAllGlobals());

describe("POST SSE protocol", () => {
  it("handles each UTF-8 byte, split CRLF, comments and multiline JSON", async () => {
    const events: unknown[] = [];
    const encoded = bytes(
      ': heartbeat\r\nevent: final\r\ndata: {"answer":\r\ndata: "缓存诊断完成"}\r\n\r\n',
    );
    await readSSE(
      body(Array.from(encoded, (byte) => new Uint8Array([byte]))),
      (event) => events.push(event),
    );
    expect(events).toEqual([
      { type: "final", data: { answer: "缓存诊断完成" } },
    ]);
  });
  it("does not dispatch incomplete frames", async () => {
    const callback = vi.fn();
    await readSSE(
      body([bytes('event: final\ndata: {"answer":"incomplete"}')]),
      callback,
    );
    expect(callback).not.toHaveBeenCalled();
  });
  it("does not treat final as workflow completion and captures response trace", async () => {
    const trace = "a".repeat(32);
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        body([
          bytes(
            'event: final\ndata: {"answer":"答案"}\n\nevent: workflow_completed\ndata: {"status":"completed"}\n\n',
          ),
        ]),
        {
          headers: {
            "Content-Type": "text/event-stream",
            "X-Trace-ID": trace,
          },
        },
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const events = vi.fn(),
      traced = vi.fn();
    await stream(
      "/test",
      { message: "redis" },
      "test-token",
      new AbortController().signal,
      events,
      traced,
      true,
    );
    expect(events).toHaveBeenCalledTimes(2);
    expect(traced).toHaveBeenCalledWith(trace);
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe(
      "Bearer test-token",
    );
    expect(fetch.mock.calls[0][1].method).toBe("POST");
  });
  it("detects HTTP 200 truncation after final before workflow_completed", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            body([bytes('event: final\ndata: {"answer":"答案"}\n\n')]),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
        ),
    );
    await expect(
      stream(
        "/test",
        {},
        "",
        new AbortController().signal,
        vi.fn(),
        vi.fn(),
        true,
      ),
    ).rejects.toThrow("提前结束");
  });
  it("reports HTTP auth failures and does not retry approval", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ error: { code: "workflow_unauthorized" } }),
          { status: 403 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(
      stream(
        "/approval",
        {},
        "bad",
        new AbortController().signal,
        vi.fn(),
        vi.fn(),
        true,
      ),
    ).rejects.toThrow("令牌");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("rejects malformed event JSON", async () => {
    await expect(
      readSSE(body([bytes("event: final\ndata: not-json\n\n")]), vi.fn()),
    ).rejects.toThrow();
  });
});

describe("execution state", () => {
  it("keeps a pending approval distinct from final completion", () => {
    let value = conversation();
    value = eventUpdate(value, {
      type: "workflow_start",
      data: { workflow_id: "workflow", status: "running" },
    });
    value = eventUpdate(value, {
      type: "approval_required",
      data: {
        workflow_id: "workflow",
        status: "pending_approval",
        approval: { approval_id: "approval" },
      },
    });
    expect(value.phase).toBe("pending_approval");
    expect(value.messages).toHaveLength(0);
    expect(value.workflow?.pending_approval?.approval_id).toBe("approval");
    value = eventUpdate(value, {
      type: "final",
      data: { answer: "已拒绝模拟操作" },
    });
    expect(value.phase).toBe("committing");
    value = eventUpdate(value, {
      type: "workflow_completed",
      data: {
        workflow_id: "workflow",
        status: "completed",
        session_status: "conflict",
      },
    });
    expect(value.phase).toBe("completed");
    expect(value.workflow?.session_status).toBe("conflict");
    expect(value.workflow?.pending_approval).toBeNull();
  });
  it("keeps SSE business errors visible even with HTTP 200", () => {
    const value = eventUpdate(conversation(), {
      type: "error",
      data: { code: "llm_timeout" },
    });
    expect(value.phase).toBe("failed");
    expect(value.error).toContain("超时");
  });
  it("constructs a Tempo trace query and rejects unsafe URLs", () => {
    const trace = "a".repeat(32);
    const url = new URL(
      traceURL(trace, "http://localhost:3000/grafana", "opspilot-tempo")!,
    );
    expect(url.pathname).toBe("/grafana/explore");
    const pane = JSON.parse(url.searchParams.get("panes")!).trace;
    expect(pane.queries[0].query).toBe(trace);
    expect(pane.datasource).toBe("opspilot-tempo");
    expect(traceURL(trace, "javascript:alert(1)")).toBeUndefined();
    expect(traceURL("no-trace")).toBeUndefined();
  });
});
