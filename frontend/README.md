# OpsPilot Demo Console

React + TypeScript + Vite 本地开发控制台。连接现有 FastAPI，展示诊断对话、SSE 执行事件、Redis Session、Workflow 人工审批与恢复，以及 Grafana Trace。前端不提供假进度或预置诊断答案。

## 启动

需要 **Linux/WSL 内的 Node.js 22.12+ 与 npm**。不要在 WSL 中混用 Windows 的 npm。

先在仓库根目录启动已配置好的后端：

```bash
conda run --no-capture-output -n opspilot \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

另开终端：

```bash
cd frontend
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173>。开发代理将 `/api` 与 `/health` 转发到 `http://127.0.0.1:8000`；不需要开放 CORS。修改根目录 `.env` 后重启后端。

需要改端口或 Grafana 地址时，将 `frontend/.env.example` 复制为 `frontend/.env` 并修改：

```dotenv
OPSPILOT_API_TARGET=http://127.0.0.1:8000
VITE_GRAFANA_URL=http://127.0.0.1:3000
VITE_TEMPO_DATASOURCE_UID=opspilot-tempo
```

前端不会读取根目录的后端 `.env`。任何 `VITE_*` 值都会公开到浏览器，**不要在其中放置模型 Key、数据库密码或审批令牌**。Workflow 令牌通过页面「连接设置」输入，仅保存在内存，刷新后需重新输入。

## 功能与后端要求

| 功能 | 要求与行为 |
| --- | --- |
| 基础诊断 | 默认可用，调用 `POST /api/v1/diagnose/stream`；跟随后端的真实模型 / Mock 模式 |
| 多轮会话 | 后端 `SESSION_ENABLED=true`，Redis 可用；点击「开启多轮会话」或「新建会话」获得服务端 ID，每轮携带同一 ID |
| Workflow | `WORKFLOW_ENABLED=true`、`WORKFLOW_LOCAL_DEMO=true`、至少 32 字符的 `WORKFLOW_API_TOKEN`，并完成 PostgreSQL 迁移和 Checkpoint 初始化，见根 README 的 V0.3 启动章节 |
| RAG / MCP | 仅 Workflow 模式，后端分别启用 `RAG_ENABLED` / `MCP_ENABLED` 并准备对应依赖；基础诊断不会显示为已启用 |
| HITL | 只有收到 `approval_required` 或查询得到真实 `pending_approval` 才出现按钮，展示工具、参数、风险与审批 ID |
| Trace | 后端 `TRACING_ENABLED=true` 且 OTLP/Tempo/Grafana 正常。链接使用执行响应的 `X-Trace-ID` 和 Tempo datasource UID；每次恢复 / 审批保留独立 Trace 链接 |

Workflow 模式下，批准和拒绝都通过 `/approval?stream=true&progress=true` 继续原工作流。断线不会自动重试写操作：刷新状态后，只有后端 `resumable=true` 且未等待审批 / 未结束时允许恢复。恢复使用原 Workflow ID，不会重新创建工作流。

前端把 `final` 当作回答，把 `workflow_completed` 当作工作流完成；等待审批不是完成。会话提交返回 `expired` 或 `conflict` 时会明确提示历史没有写入。HTTP 200 中的 SSE `error` / `workflow_failed` 也会显示错误。当前 SSE 是执行事件流，模型答案在 `final` 时整体显示，并非逐 token 输出。

浏览器 `sessionStorage` 只保存当前标签页的会话与工作流 ID，不保存令牌和问答正文。刷新后可重新填写令牌、查询 Workflow，或继续有效的服务端 Session。后端尚无历史消息读取 / 会话列表接口，因此刷新不会还原旧聊天气泡；服务端仍会为有效 Session 提供多轮上下文。也可以在设置中手动输入已有 Session / Workflow ID。连接状态仅表示 FastAPI 可访问，不代表模型供应商健康。

## 快速演示

1. 后端用 `LLM_MODE=mock`，启用并初始化 Workflow；在页面输入 Workflow 令牌。
2. 发送 Redis 问题，查看实际工具事件。准备好 RAG/MCP 后再启用对应开关。
3. 点击「人工审批演示」，发送 `mock remediation`；选择「批准并继续」或「拒绝并继续」。Mock 模式保证该触发词产生审批；真实模型自主选工具，不能保证每次都会触发。
4. 在等待审批时刷新页面，再输入令牌并刷新状态；审批应仍可处理。
5. 运行中点击「断开」，刷新状态后点击「恢复执行」，观察原 Workflow ID 和新的 Trace。

模型切为 DeepSeek 后继续使用同一界面。工具当前仍返回模拟数据，修复动作仍只登记模拟记录。

## 验证与构建

```bash
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

Vitest 验证 SSE 分片、UTF-8、终止事件、错误和状态转换。Playwright 使用明确的测试接口替身验证批准 / 拒绝、恢复、多轮会话、Trace 和移动布局，不消耗真实模型额度；测试替身不进入应用代码。后端新增测试 `tests/test_demo_console.py` 使用实际 LangGraph 与 SSE 路由验证可选进度事件及隐私边界。

构建产物为 `dist/`。`npm run preview` 可在 <http://127.0.0.1:4173> 预览并代理本地后端。独立静态托管时需要为 `/api` 配置同源反向代理；Vite 代理仅用于开发 / 预览。本任务交付本地源码，不部署外网控制台。

## 接口补充

- `GET /api/v1/capabilities`：仅返回 `llm_mode`、功能开关与 `tool_data_source`，不包含密钥、模型端点或数据库连接信息。
- 三个 Workflow SSE 执行接口增加可选 `progress=true`；默认关闭，保持原事件序列。
- 开启后增加 `node_progress` 事件，字段为 `node`（`retrieve_runbook` / `plan` / `execute_tool`）、`phase`（`started` / `completed` / `failed`）；检索完成额外提供 `retrieval_status` 与 `chunk_count`。
- 进度只来自实际 Graph 节点，不发送原始 GraphState、Runbook 内容、模型推理内容或内部异常。失败重试可能出现多次同节点事件，表示真实执行尝试。
