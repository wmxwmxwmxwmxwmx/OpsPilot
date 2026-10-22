export type Capabilities = {
  llm_mode: "mock" | "openai";
  sessions: boolean;
  workflows: boolean;
  rag: boolean;
  mcp: boolean;
  tracing: boolean;
  tool_data_source: string;
};
export type Approval = {
  approval_id: string;
  workflow_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  risk: string;
  description: string;
};
export type Workflow = {
  workflow_id: string;
  status: string;
  current_step?: string | null;
  pending_approval?: Approval | null;
  error_code?: string | null;
  resumable?: boolean;
  session_status?: string;
  checkpoint_pruned?: boolean;
  incident_id?: string | null;
};
export type StreamEvent = { type: string; data: Record<string, unknown> };
const explanations: Record<string, string> = {
  workflow_disabled:
    "Workflow 未启用，请切换基础诊断，或按 README 启用工作流。",
  workflow_unauthorized: "Workflow 令牌不正确，请在连接设置中重新填写。",
  workflow_busy: "工作流正在被其他请求执行，请稍后刷新状态。",
  workflow_terminal: "工作流已经结束，请刷新状态。",
  workflow_not_found: "找不到该工作流，请核对 ID。",
  workflow_not_resumable: "该失败不可恢复，请新建诊断。",
  approval_required: "工作流正在等待人工审批，请先批准或拒绝。",
  invalid_approval: "审批信息已变化，请刷新工作流状态。",
  session_disabled: "Session 未启用。基础诊断仍可使用；多轮对话需要 Redis。",
  session_not_found: "会话已过期或不存在，请新建会话。",
  session_conflict: "会话已被其他请求更新，请新建会话后继续。",
  llm_timeout: "模型请求超时。工作流请刷新状态后恢复；基础诊断可重新发送。",
  llm_error: "模型调用失败，请检查后端模型配置、额度与网络。",
  connection_interrupted: "执行连接已中断，请刷新状态后恢复原工作流。",
};
export function explain(code: string, fallback?: string) {
  return explanations[code] || fallback || code;
}
export class ApiError extends Error {
  constructor(
    public code: string,
    message?: string,
  ) {
    super(explain(code, message));
    this.name = "ApiError";
  }
}
function headers(token?: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}
async function check(response: Response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      body.error?.code || `http_${response.status}`,
      body.error?.message || `请求失败（HTTP ${response.status}）`,
    );
  }
}
export async function request<T>(
  path: string,
  options: { token?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const response = await fetch(path, {
    method: options.body === undefined ? "GET" : "POST",
    headers: headers(options.token),
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal
      ? AbortSignal.any([options.signal, AbortSignal.timeout(15_000)])
      : AbortSignal.timeout(15_000),
  });
  await check(response);
  return response.json();
}

// POST + Authorization require fetch; native EventSource supports neither.
// Line parser tolerates split UTF-8, split CRLF, comments and multiline data.
export async function readSSE(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = "",
    eventType = "",
    data: string[] = [],
    frameSize = 0;
  function line(value: string) {
    if (value === "") {
      if (data.length) {
        const parsed: unknown = JSON.parse(data.join("\n"));
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
          throw new Error("SSE 数据格式错误。");
        onEvent({
          type: eventType || "message",
          data: parsed as Record<string, unknown>,
        });
      }
      eventType = "";
      data = [];
      frameSize = 0;
      return;
    }
    if (value.startsWith(":")) return;
    frameSize += value.length;
    if (frameSize > 1_048_576) throw new Error("SSE 事件超过大小限制。");
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "event") eventType = content;
    if (field === "data") data.push(content);
  }
  function consume(final = false) {
    let i = 0,
      start = 0;
    while (i < pending.length) {
      if (pending[i] === "\r" || pending[i] === "\n") {
        if (pending[i] === "\r" && i === pending.length - 1 && !final) break;
        line(pending.slice(start, i));
        if (pending[i] === "\r" && pending[i + 1] === "\n") i++;
        start = i + 1;
      }
      i++;
    }
    pending = pending.slice(start);
    if (pending.length > 1_048_576) throw new Error("SSE 事件超过大小限制。");
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      consume(done);
      if (done) break;
    }
    // An incomplete frame is not an acknowledged event.
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
export async function stream(
  path: string,
  body: unknown,
  token: string | undefined,
  signal: AbortSignal,
  onEvent: (event: StreamEvent) => void,
  onTrace: (id: string) => void,
  workflow: boolean,
) {
  const response = await fetch(path, {
    method: "POST",
    headers: headers(token),
    body: JSON.stringify(body),
    signal,
  });
  await check(response);
  const trace = response.headers.get("X-Trace-ID");
  if (trace && /^[0-9a-f]{32}$/i.test(trace)) onTrace(trace);
  if (
    !response.headers.get("content-type")?.includes("text/event-stream") ||
    !response.body
  ) {
    throw new Error("服务未返回 SSE，请检查后端与代理配置。");
  }
  let terminal = false;
  await readSSE(response.body, (event) => {
    if (terminal) return;
    onEvent(event);
    terminal =
      event.type === "error" ||
      (workflow
        ? [
            "workflow_completed",
            "workflow_failed",
            "approval_required",
          ].includes(event.type)
        : event.type === "final");
  });
  if (!terminal)
    throw new Error(
      "事件流提前结束，执行结果未确认。请刷新工作流状态后再操作。",
    );
}
export function traceURL(
  traceId: string,
  base = import.meta.env.VITE_GRAFANA_URL || "http://127.0.0.1:3000",
  uid = import.meta.env.VITE_TEMPO_DATASOURCE_UID || "opspilot-tempo",
) {
  if (!/^[a-f0-9]{32}$/i.test(traceId)) return undefined;
  try {
    const root = new URL(base);
    if (!["http:", "https:"].includes(root.protocol)) return undefined;
    const url = new URL(
      `${root.pathname.replace(/\/$/, "")}/explore`,
      root.origin,
    );
    url.searchParams.set("schemaVersion", "1");
    url.searchParams.set(
      "panes",
      JSON.stringify({
        trace: {
          datasource: uid,
          queries: [
            {
              refId: "A",
              queryType: "traceId",
              query: traceId,
              datasource: { type: "tempo", uid },
            },
          ],
          range: { from: "now-1h", to: "now" },
        },
      }),
    );
    return url.toString();
  } catch {
    return undefined;
  }
}
