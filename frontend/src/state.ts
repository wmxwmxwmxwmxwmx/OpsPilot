import type { Approval, StreamEvent, Workflow } from "./api";
import { explain } from "./api";
export type Mode = "workflow" | "diagnose";
export type Entry = StreamEvent & { id: string; time: string };
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
};
export type Conversation = {
  id: string;
  title: string;
  sessionId?: string;
  messages: ChatMessage[];
  events: Entry[];
  workflow?: Workflow;
  phase: string;
  traces: string[];
  error?: string;
  restored?: boolean;
};
export function conversation(): Conversation {
  return {
    id: crypto.randomUUID(),
    title: "新的诊断",
    messages: [],
    events: [],
    phase: "idle",
    traces: [],
  };
}
export function eventUpdate(
  current: Conversation,
  event: StreamEvent,
): Conversation {
  const data = event.data;
  const next = {
    ...current,
    events: [
      ...current.events.slice(-499),
      {
        ...event,
        id: crypto.randomUUID(),
        time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
      },
    ],
  };
  if (typeof data.workflow_id === "string")
    next.workflow = {
      ...current.workflow,
      workflow_id: data.workflow_id,
      status: String(data.status || "running"),
    };
  switch (event.type) {
    case "workflow_start":
    case "workflow_resumed":
    case "agent_start":
      next.phase = "running";
      break;
    case "approval_required":
      next.phase = "pending_approval";
      if (next.workflow)
        next.workflow = {
          ...next.workflow,
          pending_approval: data.approval as Approval,
          resumable: true,
        };
      break;
    case "workflow_failed":
      next.phase = "failed";
      next.error = explain(String(data.code));
      if (next.workflow)
        next.workflow = {
          ...next.workflow,
          error_code: String(data.code),
          pending_approval: null,
        };
      break;
    case "error":
      next.phase = "failed";
      next.error = explain(
        String(data.code),
        String(data.message || data.code),
      );
      break;
    case "final":
      if (typeof data.answer === "string")
        next.messages = [
          ...next.messages,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            text: data.answer,
          },
        ];
      // Workflow final is not terminal; wait for workflow_completed (business commit).
      next.phase = next.workflow ? "committing" : "completed";
      break;
    case "workflow_completed":
      next.phase = "completed";
      if (next.workflow)
        next.workflow = {
          ...next.workflow,
          status: "completed",
          pending_approval: null,
          resumable: false,
          session_status: String(data.session_status || "not_requested"),
        };
      break;
  }
  return next;
}
export const phaseLabels: Record<string, string> = {
  idle: "等待提问",
  running: "执行中",
  committing: "确认执行结果",
  completed: "已完成",
  failed: "执行失败",
  interrupted: "连接已中断",
  pending_approval: "等待人工审批",
  cancelled: "已取消",
};
export function eventLabel(event: StreamEvent): string {
  const d = event.data;
  if (event.type === "node_progress") {
    const label =
      {
        retrieve_runbook: "RAG · Runbook 检索",
        plan: "LLM · 分析与规划",
        execute_tool: "工具执行",
      }[String(d.node)] || String(d.node);
    if (d.retrieval_status)
      return `${label} · ${{ found: `命中 ${d.chunk_count} 个片段`, empty: "未找到相关手册", unavailable: "检索不可用，已降级", disabled_or_not_needed: "未启用或无需检索" }[String(d.retrieval_status)] || d.retrieval_status}`;
    return `${label} · ${{ started: "进行中", completed: "完成", failed: "本次尝试失败" }[String(d.phase)] || d.phase}`;
  }
  if (event.type === "tool_call" || event.type === "tool_result") {
    const kind = String(d.tool).startsWith("mcp_") ? "MCP" : "Tool";
    return `${kind} · ${d.tool} · ${event.type === "tool_call" ? "请求调用" : d.success ? "已返回" : "失败 / 已拒绝"}`;
  }
  return (
    {
      agent_start: "Agent 开始诊断",
      workflow_start: "Workflow 已创建",
      workflow_resumed: "Workflow 已恢复",
      approval_required: "HITL · 等待人工确认",
      final: "诊断回答已返回",
      workflow_completed: "Workflow 已完成",
      workflow_failed: "Workflow 执行失败",
      error: "执行发生错误",
    }[event.type] || event.type
  );
}
const storageKey = "opspilot.demo.ids.v1";
export function restore(): Conversation[] {
  try {
    const values = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (!Array.isArray(values)) return [conversation()];
    const uuid =
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    const items = values.slice(0, 30).flatMap((value) => {
      if (!value || !uuid.test(value.id)) return [];
      const workflowId = uuid.test(value.workflowId)
        ? value.workflowId
        : undefined;
      return [
        {
          ...conversation(),
          id: value.id,
          title: "已保存的会话",
          restored: true,
          sessionId: uuid.test(value.sessionId) ? value.sessionId : undefined,
          workflow: workflowId
            ? { workflow_id: workflowId, status: "unknown" }
            : undefined,
        },
      ];
    });
    return items.length ? items : [conversation()];
  } catch {
    return [conversation()];
  }
}
export function restoreActive(items: Conversation[]): string {
  try {
    const id = sessionStorage.getItem(`${storageKey}.active`);
    return items.find((item) => item.id === id)?.id || items[0].id;
  } catch {
    return items[0].id;
  }
}
export function remember(items: Conversation[], active: string) {
  try {
    sessionStorage.setItem(
      storageKey,
      JSON.stringify(
        items
          .filter((c) => c.id === active)
          .concat(items.filter((c) => c.id !== active).slice(-29))
          .map((c) => ({
            id: c.id,
            sessionId: c.sessionId,
            workflowId: c.workflow?.workflow_id,
          })),
      ),
    );
    sessionStorage.setItem(`${storageKey}.active`, active);
  } catch {
    /* Private browsing/storage limits must not break a running diagnosis. */
  }
}
