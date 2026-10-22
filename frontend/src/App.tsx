import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Activity,
  ArrowDown,
  ArrowRight,
  Check,
  ChevronDown,
  CircleHelp,
  ExternalLink,
  GitBranch,
  Layers3,
  LoaderCircle,
  MessageSquare,
  Plus,
  Radio,
  RefreshCw,
  Send,
  Settings2,
  ShieldCheck,
  Square,
  Terminal,
  X,
  Zap,
  BookOpen,
  Cable,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { explain, request, stream, traceURL } from "./api";
import type { Capabilities, Workflow } from "./api";
import {
  conversation,
  eventLabel,
  eventUpdate,
  phaseLabels,
  remember,
  restore,
  restoreActive,
} from "./state";
import type { Conversation, Mode } from "./state";

const suggestions = [
  {
    icon: <Cable size={19} />,
    title: "Redis 缓存诊断",
    text: "Redis 缓存命中率下降，帮我排查",
  },
  {
    icon: <Activity size={19} />,
    title: "订单接口排查",
    text: "订单接口突然变慢，请检查系统指标和 MySQL 慢查询",
  },
  {
    icon: <ShieldCheck size={19} />,
    title: "人工审批演示",
    text: "mock remediation",
  },
];
function errorText(error: unknown) {
  return error instanceof TypeError
    ? "无法连接后端，请确认 FastAPI 已启动并检查代理地址。"
    : error instanceof Error
      ? error.message
      : "请求失败，请重试。";
}
function Identifier({ label, value }: { label: string; value?: string }) {
  return (
    <div className="identifier">
      <span>{label}</span>
      <code title={value}>{value || "—"}</code>
    </div>
  );
}
export default function App() {
  const [items, setItems] = useState<Conversation[]>(restore);
  const [active, setActive] = useState(() => restoreActive(items));
  const [cap, setCap] = useState<Capabilities>();
  const [connection, setConnection] = useState("connecting");
  const [mode, setMode] = useState<Mode>("workflow");
  const [token, setToken] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [workflowInput, setWorkflowInput] = useState("");
  const [sessionInput, setSessionInput] = useState("");
  const [eventFilter, setEventFilter] = useState("all");
  const controller = useRef<AbortController | null>(null);
  const lock = useRef(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const chatPane = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const [unread, setUnread] = useState(false);
  const current = items.find((item) => item.id === active) || items[0];
  const workflow = current.workflow;
  const approval = workflow?.pending_approval;
  const terminal =
    workflow && ["completed", "cancelled"].includes(workflow.status);
  const blockedWorkflow =
    workflow &&
    !terminal &&
    !(workflow.status === "failed" && workflow.resumable === false);
  const sessionWarning =
    workflow?.session_status &&
    ["expired", "conflict"].includes(workflow.session_status);
  const canSend =
    !busy &&
    Boolean(draft.trim()) &&
    connection === "online" &&
    !blockedWorkflow &&
    !sessionWarning &&
    (mode !== "workflow" || Boolean(token.trim()));
  const update = (id: string, fn: (value: Conversation) => Conversation) =>
    setItems((old) => old.map((item) => (item.id === id ? fn(item) : item)));

  async function connect(signal?: AbortSignal) {
    setConnection("connecting");
    try {
      const result = await request<Capabilities>("/api/v1/capabilities", {
        signal,
      });
      setCap(result);
      setConnection("online");
      setMode((old) => (!result.workflows ? "diagnose" : old));
    } catch (error) {
      if (signal?.aborted) return;
      setConnection("offline");
      setNotice(errorText(error));
    }
  }
  useEffect(() => {
    const abort = new AbortController();
    void connect(abort.signal);
    return () => {
      abort.abort();
      controller.current?.abort();
    };
  }, []);
  useEffect(() => {
    remember(items, active);
  }, [items, active]);
  useEffect(() => {
    if (nearBottom.current)
      chatPane.current?.scrollTo({
        top: chatPane.current.scrollHeight,
        behavior: "smooth",
      });
    else setUnread(true);
  }, [current.id, current.messages.length, current.phase]);

  async function refresh(id = current.id, workflowId = workflow?.workflow_id) {
    if (!workflowId) return;
    if (!token.trim()) {
      setSettingsOpen(true);
      setNotice("请输入 Workflow 令牌后查询状态。");
      return;
    }
    const result = await request<Workflow>(`/api/v1/workflows/${workflowId}`, {
      token: token.trim(),
    });
    update(id, (value) => ({
      ...value,
      workflow: result,
      phase: result.status,
      error: result.error_code ? explain(result.error_code) : undefined,
    }));
  }
  async function safely(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setNotice("");
    try {
      await action();
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  async function newConversation() {
    await safely(async () => {
      const next = conversation();
      if (cap?.sessions) {
        const result = await request<{ session_id: string }>(
          "/api/v1/sessions",
          { body: {} },
        );
        next.sessionId = result.session_id;
      }
      setItems((old) => [...old, next]);
      setActive(next.id);
      setDraft("");
      setUnread(false);
      nearBottom.current = true;
    });
  }
  async function attachSession() {
    await safely(async () => {
      if (current.messages.length)
        throw new Error("请新建会话后开启多轮对话，以保持上下文一致。");
      const result = await request<{ session_id: string }>("/api/v1/sessions", {
        body: {},
      });
      update(current.id, (value) => ({
        ...value,
        sessionId: result.session_id,
      }));
    });
  }
  async function execute(action: "start" | "resume" | "approve" | "reject") {
    if (lock.current || (action === "start" && !canSend)) return;
    const id = current.id;
    const isWorkflow = action !== "start" || mode === "workflow";
    const text = draft.trim();
    const abort = new AbortController();
    lock.current = true;
    controller.current = abort;
    setBusy(true);
    setNotice("");
    let observedId = action === "start" ? undefined : workflow?.workflow_id;
    let path = "/api/v1/diagnose/stream";
    let body: unknown = {
      message: text,
      ...(current.sessionId ? { session_id: current.sessionId } : {}),
    };
    if (isWorkflow) {
      path =
        action === "start"
          ? "/api/v1/workflows?stream=true&progress=true"
          : `/api/v1/workflows/${workflow!.workflow_id}/${action === "resume" ? "resume" : "approval"}?stream=true&progress=true`;
      if (action !== "start")
        body =
          action === "resume"
            ? {}
            : { approval_id: approval!.approval_id, decision: action };
    }
    if (action === "start") {
      update(id, (value) => ({
        ...value,
        title: value.messages.length ? value.title : text.slice(0, 28),
        messages: [
          ...value.messages,
          { id: crypto.randomUUID(), role: "user", text },
        ],
        events: [],
        workflow: undefined,
        phase: "running",
        error: undefined,
        restored: false,
      }));
      setDraft("");
    } else
      update(id, (value) => ({ ...value, phase: "running", error: undefined }));
    try {
      await stream(
        path,
        body,
        isWorkflow ? token.trim() : undefined,
        abort.signal,
        (event) => {
          if (typeof event.data.workflow_id === "string")
            observedId = event.data.workflow_id;
          update(id, (value) => eventUpdate(value, event));
        },
        (trace) =>
          update(id, (value) => ({
            ...value,
            traces: [...value.traces, trace],
          })),
        isWorkflow,
      );
    } catch (error) {
      const message = abort.signal.aborted
        ? "已断开本次执行连接。工作流请刷新状态后恢复；此操作不会删除工作流。"
        : errorText(error);
      update(id, (value) => ({
        ...value,
        phase: "interrupted",
        error: message,
      }));
    } finally {
      // No automatic resume/retry. Read authoritative status after every segment.
      if (observedId) {
        try {
          await refresh(id, observedId);
        } catch (error) {
          setNotice(`状态尚未确认：${errorText(error)}`);
        }
      }
      controller.current = null;
      lock.current = false;
      setBusy(false);
    }
  }
  async function importWorkflow(event: FormEvent) {
    event.preventDefault();
    await safely(async () => {
      if (
        !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
          workflowInput.trim(),
        )
      )
        throw new Error("请输入有效的 Workflow UUID4。");
      const result = await request<Workflow>(
        `/api/v1/workflows/${workflowInput.trim()}`,
        { token: token.trim() },
      );
      const existing = items.find(
        (item) => item.workflow?.workflow_id === result.workflow_id,
      );
      if (existing) {
        update(existing.id, (value) => ({
          ...value,
          workflow: result,
          phase: result.status,
        }));
        setActive(existing.id);
      } else {
        const next = {
          ...conversation(),
          title: "恢复的工作流",
          workflow: result,
          phase: result.status,
          restored: true,
        };
        setItems((old) => [...old, next]);
        setActive(next.id);
      }
      setSettingsOpen(false);
      setWorkflowInput("");
    });
  }
  function importSession(event: FormEvent) {
    event.preventDefault();
    if (
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        sessionInput.trim(),
      )
    ) {
      setNotice("请输入服务端创建的 Session UUID4。");
      return;
    }
    const existing = items.find(
      (item) => item.sessionId === sessionInput.trim(),
    );
    if (existing) setActive(existing.id);
    else {
      const next = {
        ...conversation(),
        title: "继续已有会话",
        sessionId: sessionInput.trim(),
        restored: true,
      };
      setItems((old) => [...old, next]);
      setActive(next.id);
    }
    setSessionInput("");
    setSettingsOpen(false);
  }
  const visibleEvents = current.events.filter(
    (event) =>
      eventFilter === "all" ||
      (eventFilter === "tools"
        ? event.type.startsWith("tool_")
        : event.type === "node_progress" &&
          event.data.node === "retrieve_runbook"),
  );
  const lastProgress = [...current.events]
    .reverse()
    .find((event) => event.type === "node_progress");
  const activityLabel = busy
    ? lastProgress && lastProgress.data.phase === "started"
      ? eventLabel(lastProgress)
      : "正在等待后端事件…"
    : phaseLabels[current.phase] || current.phase;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="OpsPilot 首页">
          <div className="brand-mark">
            <Activity size={24} />
          </div>
          <span>
            OpsPilot <small>DEMO CONSOLE</small>
          </span>
        </a>
        <div className="workspace">
          <span className="workspace-icon">
            <Terminal size={17} />
          </span>
          <div>
            本地开发空间<small>Development workspace</small>
          </div>
          <span className="tiny-dot" />
        </div>
        <button
          className="new-session"
          onClick={newConversation}
          disabled={busy || connection !== "online"}
        >
          <Plus size={17} />
          {cap?.sessions ? "新建会话" : "新建对话"}
          <span>＋</span>
        </button>
        <div className="nav-heading">工作台</div>
        <div className="nav-active">
          <MessageSquare size={17} />
          诊断对话<span>{items.length.toString().padStart(2, "0")}</span>
        </div>
        <div className="nav-heading recent-heading">
          当前标签页的会话 <span>SESSIONS</span>
        </div>
        <nav className="session-list" aria-label="会话列表">
          {items.map((item) => (
            <button
              key={item.id}
              disabled={busy}
              className={`session-item ${item.id === active ? "selected" : ""}`}
              onClick={() => {
                setActive(item.id);
                setUnread(false);
                nearBottom.current = true;
              }}
            >
              <MessageSquare size={15} />
              <span>
                {item.title}
                <small>
                  {item.sessionId ? "多轮会话" : "独立诊断"} ·{" "}
                  {phaseLabels[item.phase] || "待查询"}
                </small>
              </span>
              {item.workflow?.status === "pending_approval" && (
                <span className="approval-dot" />
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="demo-note">
            <ShieldCheck size={18} />
            <div>
              安全演示环境
              <p>
                工具返回模拟数据
                <br />
                修复操作需要人工确认
              </p>
            </div>
          </div>
          <button
            className="settings-button"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings2 size={17} />
            连接设置<span>↗</span>
          </button>
          <div className="sidebar-footer">
            <span className={`tiny-dot ${connection}`} />
            {connection === "online"
              ? "FastAPI 已连接"
              : connection === "connecting"
                ? "正在连接…"
                : "后端未连接"}
            <span>v0.4</span>
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div>
            <span className="breadcrumb">工作台</span>
            <span className="slash">/</span>诊断对话{" "}
            <span className="demo-badge">DEMO</span>
          </div>
          <button
            className="quiet-button"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings2 size={16} />
            连接设置
          </button>
        </header>
        <section className="page-heading">
          <div>
            <div className="eyebrow">OBSERVE. REASON. RESOLVE.</div>
            <h1>
              诊断工作台<span> / </span>
              <span className="heading-en">Diagnosis</span>
            </h1>
            <p>从问题到证据，让每一步诊断清晰可见。</p>
          </div>
          <span className={`connection-badge ${connection}`}>
            <i />
            {connection === "online"
              ? `${cap?.llm_mode === "openai" ? "真实模型" : "Mock 模型"} · 已连接`
              : connection === "connecting"
                ? "连接中"
                : "未连接"}
          </span>
        </section>
        {notice && !settingsOpen && (
          <div className="notice" role="alert">
            <CircleHelp size={17} />
            <span>{notice}</span>
            <button aria-label="关闭提示" onClick={() => setNotice("")}>
              <X size={15} />
            </button>
          </div>
        )}
        <div className="feature-strip">
          <div>
            <GitBranch size={17} />
            <span>执行引擎</span>
            <strong>
              {mode === "workflow" ? "LangGraph" : "Legacy Agent"}
            </strong>
          </div>
          <div>
            <BookOpen size={17} />
            <span>Runbook</span>
            <strong>
              {cap?.rag && mode === "workflow" ? "已启用" : "未启用"}
            </strong>
          </div>
          <div>
            <Cable size={17} />
            <span>MCP 工具</span>
            <strong>
              {cap?.mcp && mode === "workflow" ? "已启用" : "未启用"}
            </strong>
          </div>
          <div>
            <ShieldCheck size={17} />
            <span>数据来源</span>
            <strong>模拟工具</strong>
          </div>
        </div>
        <div className="console-grid">
          <section className="chat-card panel">
            <div className="panel-heading">
              <h2>
                <MessageSquare size={18} />
                诊断对话
              </h2>
              <label className="mode-select">
                <span className="sr-only">诊断模式</span>
                <select
                  value={mode}
                  disabled={busy || Boolean(blockedWorkflow)}
                  onChange={(e) => setMode(e.target.value as Mode)}
                >
                  <option value="workflow" disabled={!cap?.workflows}>
                    Workflow 模式
                  </option>
                  <option value="diagnose">基础诊断</option>
                </select>
                <ChevronDown size={13} />
              </label>
            </div>
            <div className="session-bar">
              <span
                className={`tiny-dot ${current.sessionId ? "online" : ""}`}
              />
              <span>
                {current.sessionId
                  ? "多轮上下文已开启"
                  : "独立诊断 · 不携带历史上下文"}
              </span>
              {!current.sessionId &&
                cap?.sessions &&
                current.messages.length === 0 && (
                  <button disabled={busy} onClick={attachSession}>
                    开启多轮会话
                  </button>
                )}
            </div>
            <div
              className="chat-scroll"
              ref={chatPane}
              onScroll={() => {
                const el = chatPane.current!;
                nearBottom.current =
                  el.scrollHeight - el.scrollTop - el.clientHeight < 80;
                if (nearBottom.current) setUnread(false);
              }}
              role="log"
              aria-label="诊断对话记录"
              aria-live="polite"
            >
              {current.restored && (
                <p className="inline-note">
                  已恢复标识。历史消息未保存在浏览器；服务端仍会读取有效 Session
                  的上下文。工作流请刷新状态，已完成工作流的历史答案暂不支持回读。
                </p>
              )}
              {current.messages.length === 0 && (
                <div className="welcome">
                  <div className="welcome-mark">
                    <Activity size={31} />
                  </div>
                  <span className="eyebrow">YOUR INCIDENT COPILOT</span>
                  <h2>今天，我们排查什么？</h2>
                  <p>
                    描述现象，OpsPilot 将收集证据、查阅手册，
                    <br />
                    并给出可追溯的诊断建议。
                  </p>
                  <div className="suggestions">
                    {suggestions.map((suggestion, index) => (
                      <button
                        key={suggestion.title}
                        disabled={busy || (index === 2 && mode !== "workflow")}
                        onClick={() => {
                          setDraft(suggestion.text);
                          textarea.current?.focus();
                        }}
                      >
                        <span>{suggestion.icon}</span>
                        <div>
                          {suggestion.title}
                          <small>
                            {index === 2
                              ? "模拟修复 · Mock 模式可确定触发"
                              : suggestion.text}
                          </small>
                        </div>
                        <ArrowRight size={15} />
                      </button>
                    ))}
                  </div>
                  <div className="welcome-foot">
                    <Layers3 size={13} />
                    实时事件 · 工具证据 · 人工决策
                  </div>
                </div>
              )}
              {current.messages.map((message) => (
                <article
                  key={message.id}
                  className={`chat-message ${message.role}`}
                >
                  <div className="avatar">
                    {message.role === "assistant" ? (
                      <Activity size={18} />
                    ) : (
                      "你"
                    )}
                  </div>
                  <div className="message-body">
                    <div className="message-author">
                      {message.role === "assistant" ? "OpsPilot" : "你"}
                      <small>
                        {message.role === "assistant" ? "诊断结果" : "诊断请求"}
                      </small>
                    </div>
                    <div className="markdown">
                      <ReactMarkdown
                        components={{
                          a: (props) => (
                            <a
                              {...props}
                              target="_blank"
                              rel="noreferrer noopener"
                            />
                          ),
                        }}
                      >
                        {message.text}
                      </ReactMarkdown>
                    </div>
                  </div>
                </article>
              ))}
              {busy && (
                <div className="thinking">
                  <LoaderCircle size={16} className="spin" />
                  {activityLabel}
                </div>
              )}
              {current.error && (
                <div className="error-box" role="alert">
                  {current.error}
                </div>
              )}
              {sessionWarning && (
                <div className="error-box">
                  本次诊断已完成，但会话历史未写入（{workflow.session_status}
                  ）。请新建会话后继续。
                </div>
              )}
              <div ref={chatEnd} />
            </div>
            {unread && (
              <button
                className="new-message"
                onClick={() => {
                  chatPane.current?.scrollTo({
                    top: chatPane.current.scrollHeight,
                    behavior: "smooth",
                  });
                  nearBottom.current = true;
                  setUnread(false);
                }}
              >
                <ArrowDown size={14} />
                查看新消息
              </button>
            )}
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                void execute("start");
              }}
            >
              {blockedWorkflow && !busy && (
                <p className="composer-hint">
                  当前工作流尚未结束，请先审批、刷新或恢复；也可以新建会话。
                </p>
              )}
              {mode === "workflow" && !token.trim() && (
                <button
                  type="button"
                  className="token-hint"
                  onClick={() => setSettingsOpen(true)}
                >
                  填写 Workflow 令牌以开始诊断 <ArrowRight size={13} />
                </button>
              )}
              <textarea
                ref={textarea}
                aria-label="诊断问题"
                placeholder="描述你的问题，例如：Redis 缓存命中率下降，帮我排查…"
                value={draft}
                maxLength={4000}
                disabled={busy || Boolean(blockedWorkflow)}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    if (canSend) void execute("start");
                  }
                }}
              />
              <div className="composer-footer">
                <span>
                  <span className="keycap">↵</span> 发送{" "}
                  <span className="separator">·</span> Shift + Enter 换行
                </span>
                <span className="character-count">{draft.length}/4000</span>
                {busy && controller.current ? (
                  <button
                    type="button"
                    className="stop-button"
                    onClick={() => controller.current?.abort()}
                  >
                    <Square size={13} />
                    断开
                  </button>
                ) : (
                  <button
                    className="send-button"
                    type="submit"
                    disabled={!canSend}
                  >
                    <Send size={15} />
                    发送
                  </button>
                )}
              </div>
            </form>
            <div className="chat-footer">
              <ShieldCheck size={12} />
              建议需结合真实环境验证；当前工具证据为模拟数据。
            </div>
          </section>
          <aside className="execution-column">
            <section className="panel run-panel">
              <div className="panel-heading">
                <h2>
                  <GitBranch size={17} />
                  执行状态
                </h2>
                <span className={`status-pill ${current.phase}`}>
                  <i />
                  {phaseLabels[current.phase] || "待查询"}
                </span>
              </div>
              <div className="run-details">
                <Identifier label="WORKFLOW ID" value={workflow?.workflow_id} />
                <Identifier label="SESSION ID" value={current.sessionId} />
                {workflow?.current_step && (
                  <div className="current-step">
                    当前节点 <code>{workflow.current_step}</code>
                  </div>
                )}
                <div className="run-actions">
                  <button
                    disabled={busy || !workflow}
                    onClick={() => safely(() => refresh())}
                  >
                    <RefreshCw size={13} />
                    刷新状态
                  </button>
                  <button
                    disabled={
                      busy ||
                      !workflow ||
                      Boolean(approval) ||
                      !workflow.resumable ||
                      Boolean(terminal)
                    }
                    onClick={() => execute("resume")}
                  >
                    <Zap size={13} />
                    恢复执行
                  </button>
                </div>
                {current.traces.length > 0 ? (
                  <div className="trace-list">
                    {current.traces.map((trace, i) => (
                      <a
                        key={`${trace}-${i}`}
                        href={traceURL(trace)}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        <span>
                          Trace {String(i + 1).padStart(2, "0")}{" "}
                          <code>{trace.slice(0, 12)}…</code>
                        </span>
                        <ExternalLink size={13} />
                      </a>
                    ))}
                  </div>
                ) : (
                  <div className="trace-unavailable">
                    <ExternalLink size={13} />
                    {cap?.tracing ? "执行后显示 Grafana Trace" : "Trace 未启用"}
                  </div>
                )}
              </div>
            </section>
            <section className="panel events-panel">
              <div className="panel-heading">
                <h2>
                  <Radio size={17} />
                  Agent 执行过程
                </h2>
                <span className={`live-label ${busy ? "active" : ""}`}>
                  <i />
                  SSE
                </span>
              </div>
              <div className="event-toolbar">
                <div role="group" aria-label="事件筛选">
                  {[
                    ["all", "全部"],
                    ["tools", "工具"],
                    ["rag", "RAG"],
                  ].map(([value, label]) => (
                    <button
                      className={eventFilter === value ? "chosen" : ""}
                      key={value}
                      onClick={() => setEventFilter(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <span>{visibleEvents.length} events</span>
              </div>
              <div
                className="event-list"
                role="log"
                aria-label="实时执行事件"
                aria-live="polite"
              >
                {visibleEvents.length === 0 ? (
                  <div className="events-empty">
                    <div className="event-orbit">
                      <Radio size={23} />
                    </div>
                    <p>等待执行事件</p>
                    <span>
                      发起诊断后，在这里查看
                      <br />
                      Agent 的每一步执行过程
                    </span>
                  </div>
                ) : (
                  visibleEvents.map((event) => (
                    <details
                      key={event.id}
                      className={`event-row ${event.type === "error" || event.type === "workflow_failed" || event.data.phase === "failed" ? "event-error" : ""}`}
                    >
                      <summary>
                        <span className="event-dot" />
                        <div>
                          <span>{eventLabel(event)}</span>
                          <small>
                            {event.time} <span>{event.type}</span>
                          </small>
                        </div>
                        <ChevronDown size={12} />
                      </summary>
                      <pre>{JSON.stringify(event.data, null, 2)}</pre>
                    </details>
                  ))
                )}
              </div>
              <div className="event-legend">
                <span>
                  <i />
                  实际事件
                </span>
                <span>点击事件查看详情</span>
              </div>
            </section>
            <section
              className={`panel approval-panel ${approval ? "has-approval" : ""}`}
            >
              <div className="panel-heading">
                <h2>
                  <ShieldCheck size={17} />
                  人工审批
                </h2>
                <span className="small-label">HUMAN IN THE LOOP</span>
              </div>
              {approval ? (
                <div className="approval-content">
                  <span className="approval-tag">等待你的决定</span>
                  <h3>模拟修复操作需要人工确认</h3>
                  <p>{approval.description}</p>
                  <Identifier label="TOOL" value={approval.tool} />
                  <Identifier
                    label="APPROVAL ID"
                    value={approval.approval_id}
                  />
                  <details>
                    <summary>查看操作参数与风险</summary>
                    <pre>
                      {JSON.stringify(
                        { risk: approval.risk, arguments: approval.arguments },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                  <div className="approval-actions">
                    <button
                      className="approve-button"
                      disabled={busy || !token.trim()}
                      onClick={() => execute("approve")}
                    >
                      <Check size={15} />
                      批准并继续
                    </button>
                    <button
                      disabled={busy || !token.trim()}
                      onClick={() => execute("reject")}
                    >
                      <X size={15} />
                      拒绝并继续
                    </button>
                  </div>
                </div>
              ) : (
                <div className="approval-empty">
                  <span>
                    <ShieldCheck size={24} />
                  </span>
                  <div>
                    暂无待审批任务<p>需要人工确认时，操作详情会显示在这里。</p>
                  </div>
                </div>
              )}
            </section>
          </aside>
        </div>
        <footer className="page-footer">
          <span>
            OPSPILOT <span> / </span> DEVELOPMENT CONSOLE
          </span>
          <span>可观察 · 可追溯 · 人工可控</span>
        </footer>
      </main>
      {settingsOpen && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setSettingsOpen(false);
          }}
        >
          <section
            className="settings-modal"
            role="dialog"
            aria-modal="true"
            aria-label="连接设置"
            onKeyDown={(e) => {
              if (e.key === "Escape") setSettingsOpen(false);
              if (e.key === "Tab") {
                const nodes = Array.from(
                  e.currentTarget.querySelectorAll<HTMLElement>(
                    "button:not(:disabled), input:not(:disabled), a[href]",
                  ),
                );
                const first = nodes[0],
                  last = nodes[nodes.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                  e.preventDefault();
                  last?.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                  e.preventDefault();
                  first?.focus();
                }
              }
            }}
          >
            <div className="panel-heading">
              <h2>
                <Settings2 size={19} />
                连接设置
              </h2>
              <button
                aria-label="关闭设置"
                onClick={() => setSettingsOpen(false)}
              >
                <X size={18} />
              </button>
            </div>
            <div className="settings-content">
              {notice && (
                <div className="error-box" role="alert">
                  {notice}
                </div>
              )}
              <label>
                Workflow 审批令牌
                <input
                  autoFocus
                  type="password"
                  autoComplete="off"
                  disabled={busy}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="填写后端 WORKFLOW_API_TOKEN"
                />
              </label>
              <p>
                仅保存在当前页面内存，刷新后需重新输入。不要填写 DeepSeek API
                Key。
              </p>
              <button
                className="primary-button"
                disabled={busy}
                onClick={() => {
                  setSettingsOpen(false);
                  setNotice("");
                }}
              >
                完成设置
              </button>
              <hr />
              <form onSubmit={importWorkflow}>
                <label>
                  查找已有工作流
                  <input
                    value={workflowInput}
                    disabled={busy}
                    onChange={(e) => setWorkflowInput(e.target.value)}
                    placeholder="Workflow ID"
                  />
                </label>
                <button
                  disabled={busy || !token.trim() || !workflowInput.trim()}
                >
                  查询并打开
                </button>
              </form>
              <form onSubmit={importSession}>
                <label>
                  继续已有 Session
                  <input
                    value={sessionInput}
                    disabled={busy}
                    onChange={(e) => setSessionInput(e.target.value)}
                    placeholder="服务器创建的 Session ID"
                  />
                </label>
                <button
                  disabled={busy || !cap?.sessions || !sessionInput.trim()}
                >
                  继续会话
                </button>
              </form>
              <p>
                浏览器只保存当前标签页的会话与工作流
                ID；历史问答不写入浏览器存储。Session
                是否有效将在下次请求时校验。
              </p>
              <hr />
              <button disabled={busy} onClick={() => connect()}>
                <RefreshCw size={14} />
                重新检查后端
              </button>
              <p>
                前端通过同源代理访问 FastAPI。后端地址及 Grafana 配置见
                frontend/.env.example。
              </p>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
