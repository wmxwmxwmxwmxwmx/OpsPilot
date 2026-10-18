# OpsPilot

**OpsPilot — Enterprise AI SRE & Incident Diagnosis Agent**

当前版本 **V0.2-M4（Redis Cache）**，保留 M1 SSE、M2 Redis Session、M3 PostgreSQL 持久化，新增历史安全摘要的 Cache-Aside 查询。
这是用于学习和扩展的单 Agent 运维诊断后端基础，不是可直接上线的生产运维平台。
它以 FastAPI、异步 Python、可替换的 LLM 接口、手写 Agent Loop 和工具注册表完成一次完整诊断。
默认 `LLM_MODE=mock`，不需要 API Key，也不访问真实 MySQL、Redis 或生产服务器。

## 当前能做什么

- 接收故障描述，经过模型选工具、执行、Observation 回传、多轮调用，返回最终诊断。
- 提供三个只读 Mock Tools；模型可一次返回多个调用，应用按顺序执行。
- 支持 MockLLM 和 OpenAI-compatible Chat Completions 两种模式。
- 提供轮数上限、模型/工具超时、参数校验、错误 Observation、结构化日志和自动化测试。
- 支持普通 JSON 诊断和 POST SSE 诊断，两者复用同一套 Agent 执行逻辑。
- 可选 Redis Session：完整对话轮次、滑动 TTL、版本并发控制、提交成功后返回最终答案。
- 可选 PostgreSQL Incident：脱敏问答、受限 JSONB 摘要、独立迁移与短事务；支持无状态/有状态诊断。
- 可选 Redis Cache：历史 Incident 安全摘要、Cache-Aside、有限 TTL、主动失效和故障回源；提供本地查询 CLI。
- `steps` 仅包含实际 Tool Call / Tool Result，不返回隐藏推理或中间模型文字。

## 架构与目录

```text
Client → FastAPI → DiagnosisService
                    ├── SessionRepository → Redis（可选，短期对话）
                    ├── Agent → LLM / Tools
                    └── IncidentRepository → PostgreSQL（可选，长期记录）
内部查询 / 本地 CLI → IncidentQueryService
                    ├── CacheRepository → Redis（可选，可重建摘要）
                    └── IncidentRepository → PostgreSQL（权威数据源）
```

Agent 内部循环：

```text
User
  │
  ▼
FastAPI
  │
  ▼
Agent Loop
  │
  ▼
LLM ◄──────────────────────────────┐
  │                               │
  ├── Final Answer ──► Final Diagnosis
  │                               │
  ▼                               │
Tool Call                         │
  │                               │
  ▼                               │
Tool Registry                     │
  ├── System Metrics              │
  ├── MySQL Diagnostics           │
  └── Redis Diagnostics           │
  │                               │
  ▼                               │
Observation ──────────────────────┘
```

```text
app/
├── main.py                 # 应用组装、生命周期、中间件、HTTP 错误映射
├── api/                    # /health、/api/v1/diagnose、/api/v1/diagnose/stream、sse.py
├── agent/                  # loop.py、events.py、schemas.py、prompts.py
├── session/               # 对话 Schema、Redis Repository、安全错误和存储脱敏
├── services/              # DiagnosisService 提交；IncidentQueryService Cache-Aside 查询
├── cache/                 # 安全摘要 Schema、集中 Key、异步 Redis 缓存
├── cli/                   # 本地历史摘要查询命令
├── db/                    # SQLAlchemy ORM、异步连接工厂、独立迁移配置
├── incident/              # 持久化业务 Schema、白名单摘要与 Repository
├── llm/                    # base.py、mock.py、openai_compatible.py
├── tools/                  # registry.py 和三个 Mock Tool
└── core/                   # config.py、logging.py
alembic/                    # 版本化 PostgreSQL 迁移，alembic.ini 不含凭据
tests/                      # 默认离线；真实 Redis/PostgreSQL 测试显式启用
pyproject.toml              # 唯一 Python 依赖声明
.env.example                # 无 Secret 的配置样例
Dockerfile                  # 独立 Python 3.11 Runtime
LEARNING.md                 # 作者学习问答及代码定位
```

## Agent Loop

核心在 [`app/agent/loop.py`](app/agent/loop.py) 的 `Agent.stream()`；`Agent.run()` 消费同一事件流组装原有 JSON 响应，HTTP/SSE 编码留在 API 层。

1. 为每个请求创建独立 `messages` 和 `steps`，放入系统提示、可选的对话历史与当前用户描述。
2. 从注册表导出工具名称、描述和 Pydantic JSON Schema，传给 `BaseLLM.generate()`。
3. LLM 返回工具调用时，将 assistant 调用消息加入上下文。
4. 按名称查找工具，验证 JSON 参数，再 `await tool.execute(arguments)`。
5. 将成功或失败结果转换成 Observation，以 `role=tool` 和对应 `tool_call_id` 追加到上下文。
6. 再次调用 LLM，由它决定继续获取证据，还是输出最终答案。
7. 没有 Tool Call 且答案非空时正常结束；达到 `MAX_STEPS` 仍无答案则抛出 `AgentExecutionError`。

`MAX_STEPS=6` 指 **LLM 调用轮数**，不是 `steps` 事件数。一次工具执行产生两个事件。
订单慢场景正常需要 3 轮 LLM、2 次 Tool、4 个工具事件（SSE 另外包含 `agent_start` 和 `final`）。设置为 2 会在收集完两项证据后失败，
因为没有剩余轮次让 LLM 生成最终答案；上限不会被额外的“最后总结”调用绕过。
每轮最多接受 16 个 Tool Call，并对每次 LLM / Tool 执行设置超时。

## Tool Calling

工具描述告诉模型“有什么能力”，输入 schema 告诉模型“参数格式是什么”。
**由 LLM 选择工具并生成参数**，应用只允许执行注册的工具并校验参数；模型本身不会执行 Python。
Observation 是工具真正返回的证据，必须重新送给模型，才能让最终诊断引用结果或发现还缺什么。

真实适配器使用 Chat Completions 的 `tools`、assistant `tool_calls` 和 `tool_call_id` 协议。
JSON 参数在工具边界校验；坏 JSON 同样成为错误 Observation，不会执行工具。
参考：[OpenAI 官方 Function Calling 文档](https://developers.openai.com/api/docs/guides/function-calling)。

## Tool Registry

[`app/tools/registry.py`](app/tools/registry.py) 用字典保存 `name → Tool`，支持
`register(tool)`、`get(name)`、`list_tools()`。重复注册会报错，未知名称返回 `None`。
每个 Tool 提供 `name`、`description`、`input_schema` 和异步 `execute()`。

工具在 `app/main.py:build_registry()` 中注册。增加工具只需要实现接口并注册，
Agent Loop 不增加按工具名判断的分支。未来可写遵守同一接口的 MCP 适配器；V0.1 没有 MCP 实现。

## 当前三个 Mock Tools

**所有指标均为固定 Mock Data，不是机器实时状态或真实生产数据。切换真实 LLM 后，工具仍是 Mock。**

| 工具 | 模拟返回值 | 输入 |
|---|---|---|
| `get_system_metrics` | CPU 35.2%、内存 61.4%、Load 1.28 | `{}` |
| `get_mysql_slow_queries` | orders 查询 2310 ms，扫描 1700000 行，返回 18 行 | `{}` |
| `get_redis_metrics` | 内存 312 MB、命中率 0.96、连接 84、阻塞 0 | `{}` |

三个工具都使用 `EmptyInput`，拒绝任何额外参数。当前工具没有真正异步 IO，但以后访问
MySQL、Redis、Prometheus、HTTP API、日志系统属于 IO-bound 操作，统一 `async` 接口可保持调用链稳定。
仅把阻塞函数包进 `async def` 不会让它变为非阻塞；未来仍需要异步驱动或明确的线程池边界。

## MockLLM

[`app/llm/mock.py`](app/llm/mock.py) 根据最新 User 问题和其后的 Observation 确定下一步，无共享轮次计数。
订单故障确定性选择：系统指标 → MySQL 慢查询 → 最终答案；包含 `redis` 或“缓存”的描述会额外调用 Redis。
工具顺序属于 MockLLM 的模拟策略，Agent Loop 没有硬编码这些工具。

最终答案读取 Observation 中的 CPU、SQL 耗时和扫描行数；改变工具返回值会改变答案，
这由测试验证。工具失败时直接说明证据不足，不把失败当作正常指标。
它不是自然语言理解模型，不会对任意问题智能规划，适合验证编排、回归测试和以后做不依赖模型网络的基准测试。

## OpenAI-compatible Adapter 与配置

[`app/llm/base.py`](app/llm/base.py) 定义 `BaseLLM` 和内部消息类型；
[`app/llm/openai_compatible.py`](app/llm/openai_compatible.py) 用 `httpx.AsyncClient` 实现协议转换。
HTTP Client 由 FastAPI 生命周期创建和关闭；Agent 不依赖任何厂商 SDK。

配置样例在 [`.env.example`](.env.example)。默认不需要创建 `.env`。
如需真实模型，在本地安全配置以下变量，勿提交密钥：

```dotenv
LLM_MODE=openai
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=
```

上面三个空值必须填入供应商提供的实际值。`OPENAI_BASE_URL` 是 API 根地址，
包含供应商要求的 `/v1` 等路径，**不要包含 `/chat/completions`**，适配器会追加它。
Base URL 不允许用户名、密码、query 或 fragment。配置在应用启动时验证。
开发机可用 `.env`，已被 `.gitignore` 排除；镜像构建也会排除 `.env`。

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` 或 `openai` |
| `MAX_STEPS` | `6` | 每次诊断最多模型调用次数，允许 1–100 |
| `LLM_TIMEOUT_SECONDS` | `30` | 每次模型调用超时 |
| `TOOL_TIMEOUT_SECONDS` | `10` | 每次工具调用超时 |
| `OPENAI_API_KEY` | 空 | 真实模式必填，使用 SecretStr 保存 |
| `OPENAI_BASE_URL` | 空 | 真实模式必填 |
| `OPENAI_MODEL` | 空 | 真实模式必填 |

适配器支持标准文本和 function tool calling，丢弃 `reasoning` / `reasoning_content` 等字段。
模型服务的 HTTP 错误、超时、截断答案、空答案和无效响应会转换成业务错误。
不同供应商仍可能需要额外字段，尤其是要求回传专有 reasoning 字段的模型；V0.1 没有实现这些扩展。
本次没有使用真实 Key 调用供应商，兼容性通过 `httpx.MockTransport` 验证。

## Development Environment

```text
Windows Host
    ↓
WSL2 Ubuntu
    ↓
Conda
    ↓
opspilot environment
    ↓
Python 3.11
```

项目路径：`/home/wmx/project/opspilot`。只用 Conda 管理 Python 版本和环境隔离，
Python 包统一由 `pyproject.toml` + pip 管理，不创建 venv，不使用 Poetry、Pipenv 或 uv virtualenv。

先检查已有环境，存在时不要重建或删除：

```bash
conda env list
# 仅在不存在时执行：
conda create -n opspilot python=3.11 pip -y
```

此次默认 Anaconda 软件源要求接受条款，实际使用以下一次性软件源选项完成环境创建，未更改全局 Conda 配置：

```bash
conda create -n opspilot --override-channels -c conda-forge python=3.11 pip -y
```

交互式开发：

```bash
cd /home/wmx/project/opspilot
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate opspilot
which python
python --version
which pip
pip install -e ".[dev]"
pytest -v
python -m uvicorn app.main:app --reload --no-access-log
```

不同自动化 Shell 不保证共享 activate 状态，无需激活的等价形式：

```bash
conda run -n opspilot pip install -e ".[dev]"
conda run -n opspilot pytest -v
conda run -n opspilot python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

实时查看自动化启动日志并避免 Uvicorn 原始访问日志记录 query string，建议：

```bash
conda run --no-capture-output -n opspilot \
  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

如果非交互 Shell 找不到 `conda`，本机安装位置为 `/home/wmx/miniconda3/bin/conda`，
可用该绝对路径替换命令，或先 `source /home/wmx/miniconda3/etc/profile.d/conda.sh`。
检查环境：

```bash
conda run -n opspilot python --version
conda run -n opspilot python -c "import sys; print(sys.executable)"
conda run -n opspilot pip --version
conda env list
```

Python 应来自 `/home/wmx/miniconda3/envs/opspilot/bin/python`，版本是 3.11.x。
如已有环境不是 Python 3.11，先人工确认处理方案，不删除或覆盖现有环境。

## API

- `GET /health`：返回 `{"status":"ok","service":"opspilot"}`，只表示进程健康，不检查供应商可用性。
- `POST /api/v1/diagnose`：请求 `{"message":"订单接口突然变慢，请帮我排查"}`。
- `POST /api/v1/diagnose/stream`：相同请求体，返回 `text/event-stream` 执行事件。
- `POST /api/v1/sessions`：显式创建 Redis 会话（默认禁用），返回 HTTP 201。
- `GET /docs`：交互式 API 文档。

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/v1/diagnose \
  -H "Content-Type: application/json" \
  -d '{"message":"订单接口突然变慢，请帮我排查"}'
```

实际成功响应结构（答案在此缩短）：

```json
{
  "answer": "基于 Mock Data 的模拟诊断：CPU 35.2%，慢 SQL 2310 ms，扫描 1700000 行……以上不是生产环境结论。",
  "steps": [
    {"type":"tool_call","tool":"get_system_metrics","call_id":"mock_call_1","arguments":{}},
    {"type":"tool_result","tool":"get_system_metrics","call_id":"mock_call_1","success":true,"result":{"cpu_percent":35.2,"memory_percent":61.4,"load_average":1.28}},
    {"type":"tool_call","tool":"get_mysql_slow_queries","call_id":"mock_call_2","arguments":{}},
    {"type":"tool_result","tool":"get_mysql_slow_queries","call_id":"mock_call_2","success":true,"result":{"slow_queries":[{"sql":"SELECT * FROM orders WHERE user_id = ?","duration_ms":2310,"rows_examined":1700000,"rows_returned":18}]}}
  ]
}
```

`message` 会去除首尾空白，必须是 1–4000 字符的非空字符串；多余字段也会被拒绝。
每个响应带有应用生成的 `X-Request-ID`，可关联同一次请求的日志。

## V0.2-M1：SSE 执行事件流

这是 **Agent 执行事件 Streaming**；`BaseLLM.generate()` 仍等待整次模型响应，没有 token 级输出。

```mermaid
flowchart TD
    HTTP[HTTP POST + DiagnoseRequest 校验] --> JSON["POST /api/v1/diagnose"]
    HTTP --> SSE["POST /api/v1/diagnose/stream"]
    JSON --> RUN[Agent.run 消费事件并收集 steps]
    RUN --> STREAM[Agent.stream 单一执行循环]
    SSE --> CONSUMER[stream_diagnosis 消费事件]
    CONSUMER --> STREAM
    STREAM --> LLM[LLM.generate + 轮数上限和超时]
    LLM --> VALIDATE[ToolRegistry 查找和参数校验]
    VALIDATE --> CALL[tool_call 事件]
    CALL --> EXEC[顺序执行 Tool + 超时和输出 JSON 校验]
    EXEC --> RESULT[tool_result 事件 + Observation]
    RESULT --> LLM
    LLM --> FINAL[final 事件]
    STREAM --> EVENTS[结构化 AgentEvent]
    EVENTS --> COLLECT[run 返回 answer + steps]
    COLLECT --> JSONCLIENT[普通 JSON 客户端]
    EVENTS --> ENCODE[encode_sse 编码]
    CONSUMER --> ERROR[执行异常转安全 error 事件]
    ERROR --> ENCODE
    ENCODE --> RESPONSE[SSEResponse / StreamingResponse]
    RESPONSE --> CLIENT[SSE 客户端]
    CLIENT --> DISCONNECT[断连取消、关闭生成器、finally 日志清理]
```

[`app/agent/events.py`](app/agent/events.py) 定义按 `type` 判别的 Pydantic 联合类型。
工具事件直接复用 `ToolCallStep`、`ToolResultStep`，避免两套字段逐渐不一致。

| SSE event | data 字段 | 含义 |
|---|---|---|
| `agent_start` | `max_steps` | 执行开始；上限仍表示模型调用轮数 |
| `tool_call` | `tool`, `call_id`, `arguments` | 准备调用；仅包含已校验参数，坏参数或未知工具为 `{}` |
| `tool_result` | `tool`, `call_id`, `success`, `result` 或 `error` | 工具执行结果；失败仍作为 Observation 交还模型 |
| `final` | `answer` | 正常终止，只发送一次 |
| `error` | `code`, `message` | 执行失败终止，只发送一次 |

内部模型保留 `type`，SSE 把它写到 `event:` 行，`data:` 是去除 `type`、省略空值的标准 JSON。
每条消息以 `\n\n` 结束。中文直接编码为 UTF-8，字符串中的回车、换行、引号等由 JSON 转义，
不会插入新的 SSE 帧。客户端应使用 `call_id` 匹配调用与结果。

启动服务后调用（`-N` 关闭 curl 输出缓冲）：

```bash
python -m uvicorn app.main:app --reload --no-access-log

curl -N -X POST http://localhost:8000/api/v1/diagnose/stream \
  -H "Content-Type: application/json" \
  -d '{"message":"订单接口突然变慢"}'

curl -N -X POST http://localhost:8000/api/v1/diagnose/stream \
  -H "Content-Type: application/json" \
  -d '{"message":"Redis缓存出现异常，请帮我诊断"}'
```

订单场景共 6 条事件，Redis 场景共 8 条事件，额外包含 `get_redis_metrics` 的调用与结果。
订单场景响应节选（省略 MySQL 调用/结果，缩短答案）：

```text
event: agent_start
data: {"max_steps":6}

event: tool_call
data: {"tool":"get_system_metrics","call_id":"mock_call_1","arguments":{}}

event: tool_result
data: {"tool":"get_system_metrics","call_id":"mock_call_1","success":true,"result":{"cpu_percent":35.2,"memory_percent":61.4,"load_average":1.28}}

event: final
data: {"answer":"基于 Mock Data 的模拟诊断……"}

```

响应包含 `Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、
`X-Accel-Buffering: no`。浏览器原生 `EventSource` 不支持 POST 请求体，应使用 `fetch()`：

```javascript
const controller = new AbortController();
const response = await fetch("/api/v1/diagnose/stream", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({message: "订单接口突然变慢"}),
  signal: controller.signal,
});
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
let pending = "";
let terminal = false;
try {
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    pending += value; // 网络 chunk 和 SSE 消息边界不一定重合。
    let boundary;
    while ((boundary = pending.indexOf("\n\n")) !== -1) {
      const frame = pending.slice(0, boundary);
      pending = pending.slice(boundary + 2);
      const lines = frame.split("\n");
      const event = lines.find(line => line.startsWith("event: ")).slice(7);
      const data = JSON.parse(lines.find(line => line.startsWith("data: ")).slice(6));
      console.log(event, data);
      if (event === "final" || event === "error") terminal = true;
    }
  }
  if (!terminal) throw new Error("诊断流提前断开，未收到终止事件");
} finally {
  await reader.cancel();
  reader.releaseLock();
}
// 在取消按钮的事件处理器中调用 controller.abort()。
```

此解析示例针对本服务固定的单行 `data:`、LF 分帧格式；没有添加 GET 接口。

请求校验在发送响应前完成，错误仍是 HTTP 422 JSON。流开始后 HTTP 通常已经是 200，
无法再改为 502/503/504；业务失败由 API 消费层把 `AgentExecutionError` 转成 `error`，例如：

```text
event: error
data: {"code":"llm_timeout","message":"模型调用超时，请稍后重试。"}

```

正常完成只有 `final`，执行失败只有 `error`；工具失败事件本身不终止 Agent。
异常文案固定，不回传 Provider 原始异常、traceback 或坏参数。原 JSON API 继续抛出业务异常，
由原 HTTP 异常处理器映射状态码及 `steps`。

[`app/api/sse.py`](app/api/sse.py) 复用 Starlette 的逐块发送实现，同时用框架已有的 AnyIO
任务组管理发送与断连监听，任一结束即取消另一方，所有任务都在响应结束前收回。
监听覆盖 ASGI 2.3/2.4，模型或工具尚在等待时也可取消，不必等下次发送失败。
`aclosing` 从 SSE 消费器到 Agent、工具生成器逐层关闭；`CancelledError` 不转换成成功或错误事件。
Agent 在模型和工具调用前设置协作取消点，Mock 工具无真实 IO 时同样能响应取消。
直接消费 `Agent.stream()` 并提前退出的调用方也必须使用 `async with aclosing(...)`。

当前限制：没有心跳、事件持久化、事件 ID、断点续传或自动重连；断连后不保证收到终止事件，
客户端应把缺少 `final`/`error` 的 EOF 视为未完成。实际到达速度还受网络和代理缓冲影响。
M2 已增量接入可选 Redis Session；尚无长期数据库记录、真实监控或新的 LLM 协议。取消依赖下游异步调用配合，
阻塞同步 IO 不应直接放在事件循环中。

## V0.2-M2：Redis Conversation Session

Session 用于跨请求恢复短期对话上下文，与 Redis Mock Tool、业务指标 Cache、长期故障记录是不同功能。
默认 `SESSION_ENABLED=false`。不传 `session_id`（或传 `null`）时不创建、不读取、不写入 Redis，
即使启用了 Session 且 Redis 故障，无状态普通诊断和 SSE 仍可使用。

### 开发 Redis

使用已有 Redis 时不要改配置或清空数据库。以下两种方式任选其一，先检查端口/容器名没有占用：

```bash
ss -ltn 'sport = :16379'
docker ps -a --filter name=opspilot-redis-m2
# 有输出表示需要先检查，不要自动停止已有服务。
docker run --rm --name opspilot-redis-m2 \
  -p 127.0.0.1:16379:6379 redis:7.4.11 \
  redis-server --save "" --appendonly no
# 另一终端停止刚创建的容器：
docker stop opspilot-redis-m2
```

本次环境没有 Redis Server/CLI，Docker Desktop 的 WSL 集成也未启用；实际使用官方源码在 `/tmp`
构建 Redis 7.4.11，未执行 `make install`、安装系统服务或修改内核配置。可复现方式（需要已有 gcc/make）：

```bash
redis_build_dir=$(mktemp -d /tmp/opspilot-redis.XXXXXX)
curl -fL https://download.redis.io/releases/redis-7.4.11.tar.gz \
  -o "$redis_build_dir/redis.tar.gz"
tar -xzf "$redis_build_dir/redis.tar.gz" -C "$redis_build_dir"
make -C "$redis_build_dir/redis-7.4.11" -j2 MALLOC=libc OPTIMIZATION=-O2 \
  REDIS_CFLAGS= REDIS_LDFLAGS= redis-server redis-cli
"$redis_build_dir/redis-7.4.11/src/redis-server" \
  --bind 127.0.0.1 --port 16379 --save "" --appendonly no --dir "$redis_build_dir"
# 前台进程按 Ctrl+C 停止；此临时实例不做磁盘持久化。
```

源码来自 [Redis 官方发行目录](https://download.redis.io/releases/)。不要执行 FLUSHALL/FLUSHDB。
以上启动方式仅用于本机开发；Session 随 TTL 或无持久化 Redis 重启丢失。

### 配置与连续请求

在现有 Conda 环境更新依赖：`pip install -e ".[dev]"`。redis-py 是运行依赖，fakeredis 仅是测试依赖。
`.env.example` 包含以下配置；`REDIS_URL` 用 `SecretStr` 保存，拒绝 query 配置覆盖：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `SESSION_ENABLED` | `false` | 禁用时创建/使用会话返回 `session_disabled` |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 支持 redis/rediss；本页隔离实例用 16379 |
| `SESSION_TTL_SECONDS` | `1800` | 1–604800 秒，成功交互后滑动续期 |
| `SESSION_MAX_TURNS` | `10` | 保留最近 1–100 轮完整 User/Assistant 对话 |
| `SESSION_MAX_MESSAGE_CHARS` | `16000` | 存储单条消息字符上限，配置范围 4000–32000；HTTP 输入仍最多 4000 字符 |
| `SESSION_MAX_BYTES` | `262144` | 整个 JSON 的 UTF-8 字节上限，配置范围 1024–1048576 |

```bash
SESSION_ENABLED=true REDIS_URL=redis://127.0.0.1:16379/0 LLM_MODE=mock \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 18000 --no-access-log
```

另一个已激活 opspilot 环境的终端：

```bash
# HTTP 201；无请求体或 {} 均可，不接受客户端指定 session_id。
session_id=$(curl -fsS -X POST http://127.0.0.1:18000/api/v1/sessions \
  | python -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')

curl -sS http://127.0.0.1:18000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$session_id\",\"message\":\"订单接口突然变慢\"}"

curl -sS http://127.0.0.1:18000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$session_id\",\"message\":\"那 Redis 缓存方面呢？\"}"

curl -N -sS http://127.0.0.1:18000/api/v1/diagnose/stream \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$session_id\",\"message\":\"继续检查 Redis 缓存\"}"

# 使用已有 redis-cli（源码方式位于 $redis_build_dir/redis-7.4.11/src/redis-cli）：
redis-cli -p 16379 GET "opspilot:session:$session_id"
redis-cli -p 16379 TTL "opspilot:session:$session_id"
```

创建响应为 `{"session_id":"服务器生成的 UUID4","expires_in_seconds":1800}`。
普通诊断响应仍只有 `answer`、`steps`；SSE 仍是 M1 的五类事件，不增加 Session 事件。

### 三条调用链

```mermaid
flowchart LR
    A[无 session_id 的普通请求] --> B[DiagnosisService.run]
    B --> C[Agent.run 消费 Agent.stream]
    C --> D[LLM 和工具]
    D --> E[answer + steps]
```

```mermaid
flowchart LR
    A[携带 session_id 的普通请求] --> B[Repository.load 获取快照和版本]
    B --> C[Agent.run 注入历史并执行]
    C --> D[Repository.commit 原子保存完整对话对]
    D --> E[提交成功后返回 answer + steps]
```

```mermaid
flowchart TD
    A[携带 session_id 的 SSE 请求] --> B[DiagnosisService.stream 加载会话]
    B --> C[Agent.stream 注入历史并执行]
    C --> D{AgentEvent}
    D -->|普通执行事件| E[M1 SSE 编码和 StreamingResponse]
    D -->|final| F[暂存最终答案并提交 Redis]
    F -->|成功| E
    F -->|失败| G[安全 error 事件]
    G --> E
    E --> H[客户端]
    H -->|断连| I[M1 取消传播与生成器清理]
```

Agent 接收 `history: Sequence[ConversationTurn] | None`，构造新的消息对象：
`System Prompt → 历史 User/Assistant 对 → 当前 User`。不在 Agent 实例上保存历史，不让 Agent 连接 Redis。
MockLLM 读取最新 User，仅统计本次执行的工具 Observation；CapturingLLM 测试检查了实际传入的历史。
这只验证上下文传递和模拟规划，不代表验证了真实模型的自然语言多轮理解。

### 数据边界、TTL 与原子提交

[`app/session/schemas.py`](app/session/schemas.py) 定义严格字段的 Pydantic 模型。
Key 为 `opspilot:session:{UUID4}`，Value 是 `model_dump_json()` 生成的 JSON，读取用
`model_validate_json()` 验证 UUID、版本、带 UTC 时区的时间及完整对话对。例如第二轮完成后：

```json
{
  "session_id": "a2ce3af5-9780-44da-938b-7609de0e08ea",
  "version": 2,
  "messages": [
    {"role":"user","content":"订单接口突然变慢"},
    {"role":"assistant","content":"第一轮最终诊断（示意）"},
    {"role":"user","content":"那 Redis 缓存方面呢？"},
    {"role":"assistant","content":"第二轮最终诊断（示意）"}
  ],
  "created_at": "2026-10-12T00:00:00Z",
  "updated_at": "2026-10-12T00:01:00Z"
}
```

只保存 User 输入和 Assistant 最终回答，不保存 System Prompt、ToolCall、原始 Observation、
Reasoning 或异常详情。最终回答可能包含模型总结后的指标，但不会恢复旧的工具执行状态；
本轮模型仍需重新调用工具获取证据。会话是短期对话历史，不是执行轨迹/审计记录。

[`privacy.py`](app/session/privacy.py) 在写入前移除配置中的 API Key、Redis 密码，以及常见的
password/api_key/token 字段、Bearer、URL 凭据、私钥等形式；存储后的历史可能与原响应文字不同。
这不是任意敏感信息识别系统，无法识别所有业务机密或 PII；不要输入敏感生产数据，真实工具接入前需专门脱敏策略。

创建使用 `SET NX EX`。普通 GET、失败、冲突不会刷新 TTL；成功提交的 `SET XX EX` 同时写完整 JSON 和 TTL。
超过轮数时按两条消息为一组裁剪；降低保留轮数后，下一次读取也只向 Agent 提供新的保留窗口，
不会因读取而改写 Redis。单条过长或整体字节超限则拒绝提交，不截断答案或写半轮对话。

[`SessionRepository.commit`](app/session/repository.py) 在 Agent 执行完之后短暂持有独立 Pipeline：
`WATCH key → GET 并校验当前版本/存在性 → MULTI → SET XX EX → EXEC`。
成功版本加一；版本改变或 WATCH 冲突拒绝旧结果，不自动重跑昂贵的 Agent，也不无界重试。
执行期间过期返回 not_found；WATCH 配合 XX 防止过期会话复活。事务只包含一个 SET，JSON 与 TTL 不会部分更新。
流水线在 finally 中 reset；仅清理阶段屏蔽 AnyIO 取消，让连接返回池，执行与提交本身仍可取消。
使用方式参见 [redis-py 官方异步文档](https://redis.io/docs/latest/develop/clients/redis-py/async/)。

[`app/main.py`](app/main.py) 在 lifespan 中按需创建一个 `redis.asyncio.Redis`，复用最多 20 个连接，
连接和命令超时各 2 秒、关闭自动重试，退出时 `await aclose()`。创建客户端不进行启动 PING，
Redis 不可用不会阻止无状态功能启动；第一次 Session IO 才会建立连接并报告故障。

### 错误与一致性边界

| 情况 | 普通 HTTP API | 已开始的 SSE |
|---|---|---|
| UUID4 不合法 | 422 `invalid_request` | 响应开始前同样 422 JSON |
| Session 功能关闭 | 503 `session_disabled` | `error` 同码 |
| 会话不存在/已过期 | 404 `session_not_found` | `error` 同码 |
| 同一会话并发冲突 | 409 `session_conflict` | `error` 同码 |
| Redis 故障 | 503 `session_storage_error` | `error` 同码 |
| Redis JSON/Schema 损坏 | 503 `session_data_error` | `error` 同码 |
| 会话大小超限 | 413 `session_too_large` | `error` 同码 |

本实现 SSE 在生成器内加载会话，所以加载失败也通过 HTTP 200 后的 `error` 告知，可能没有 `agent_start`。
客户端必须检查终止事件。Service 拦截 Agent 的 `final`，直到 Session 提交成功才向 API 交出。
Agent 日志 `agent_finish.success` 仅表示模型诊断完成；完整有状态成功还需 `session_committed`，以及客户端收到 final。

诊断失败或诊断未完成时取消，不保存当前轮次。提交已经成功后，断连不会回滚。
EXEC 已发出但确认丢失时，客户端无法断定是否写入；因此 storage_error 不承诺“肯定没有保存”，
本阶段没有幂等请求 ID、提交状态查询或自动恢复。冲突后需显式重新发起诊断，新请求会加载最新上下文。

Session ID 只是随机标识，不是用户身份或授权。当前没有认证、会话所有权校验、限流、配额、
Redis 高可用、token 预算或长会话摘要；开发端口只绑定 localhost，不应直接部署到公网。

## 错误处理与日志

| 情况 | 行为 |
|---|---|
| 请求格式错误 | HTTP 422，`invalid_request`，不回显请求内容 |
| 工具不存在 | Observation `tool_not_found`，交还模型 |
| 参数错误 | Observation `invalid_tool_arguments`，不执行，不回显坏参数 |
| 工具异常 / 超时 | Observation `tool_execution_error` / `tool_timeout`，交还模型 |
| LLM 异常 / 无效答案 | HTTP 502，`llm_error` / `invalid_llm_response` |
| LLM 超时 | HTTP 504，`llm_timeout` |
| 达到轮数上限 | HTTP 503，`max_steps_exceeded`，保留已有可观测 steps |
| 未预期应用错误 | HTTP 500，固定 `internal_error` 文案 |

工具失败是可恢复事件，模型可以修正调用或结束并说明不足；最终答复 HTTP 200 不代表故障已解决。
上表的 HTTP 错误码适用于普通诊断接口；SSE 开始后的失败使用 `error` 事件。
取消信号不会被 `except Exception` 吞掉。程序不向客户端返回 traceback 或第三方异常正文。

`app/core/logging.py` 输出 JSON Lines，记录 `request_start/finish`、`agent_start/step/finish`、
`tool_start/success/failure`、工具耗时、Agent 总耗时和错误码。日志仅接收允许的运行元数据，
不记录 API Key、Authorization、输入正文、模型输出、工具结果或异常原文。
请求日志中间件使用纯 ASGI 调用，`request_finish` 在响应体完成或断连清理后记录，
`X-Request-ID` 和日志上下文贯穿整个事件流；HTTP 200 仅表示流已建立，诊断结果看终止事件。
日志中的 `route` 是匹配的路由模板，不是含 query 的原始 URL。没有 Prometheus、Grafana 或 Langfuse 集成。

## V0.2-M3：PostgreSQL Incident Persistence

`IncidentRecord` 是**一次已经完成的诊断执行**，不代表确认发生了生产事故。
Redis Session 保存有 TTL 的对话上下文；PostgreSQL 保存独立、长期可读取的诊断记录。
两者可以分别启用。当前全部 Tools 都是 Mock，即使选择真实 LLM，工具证据也仍是模拟数据。

### 配置、启动与迁移

先安装依赖：`conda run -n opspilot pip install -e ".[dev]"`。
本次实际验证 SQLAlchemy 2.0.55、asyncpg 0.32.0、Alembic 1.20.0、Python 3.11.16。
默认 `INCIDENT_PERSISTENCE_ENABLED=false`，不会创建 PostgreSQL Engine 或连接数据库。
开启后，数据库故障明确报错，不静默丢弃记录。启动仅构造惰性连接池，不做 ping 或迁移。

下面命令创建**新的本地专用开发容器**，不要对已有业务数据库执行初始化或降级：

```bash
# 需要已启用 WSL integration 的 Docker Desktop；本次验收未使用 Docker。
# 交互输入本地开发密码，避免把实际密码写入命令历史。
read -s -p 'Local PostgreSQL password: ' POSTGRES_PASSWORD
export POSTGRES_PASSWORD
docker run --name opspilot-m3-pg -d \
  -e POSTGRES_USER=opspilot -e POSTGRES_PASSWORD -e POSTGRES_DB=opspilot \
  -p 127.0.0.1:5432:5432 postgres:18
unset POSTGRES_PASSWORD
```

在被 Git 忽略的 `.env` 中配置以下变量；示例密码只是占位，URL 中特殊字符要百分号编码：

```dotenv
INCIDENT_PERSISTENCE_ENABLED=true
DATABASE_URL=postgresql+asyncpg://opspilot:password@127.0.0.1:5432/opspilot
MIGRATION_DATABASE_URL=postgresql+asyncpg://opspilot:password@127.0.0.1:5432/opspilot
```

| 配置 | 使用方 | 约定 |
|---|---|---|
| `INCIDENT_PERSISTENCE_ENABLED` | FastAPI | 默认 false，与 SESSION_ENABLED 独立 |
| `DATABASE_URL` | 应用 Settings / Engine | 开启时必填，SecretStr，必须使用 asyncpg |
| `MIGRATION_DATABASE_URL` | 独立 MigrationSettings | Alembic 必填，不回退到 DATABASE_URL，不依赖 LLM 配置 |

生产中让应用角色只有必要的 DML 权限，迁移使用独立 DDL 角色。先部署迁移，再启动应用：

```bash
conda run -n opspilot alembic upgrade head
conda run -n opspilot alembic current
conda run -n opspilot python -m uvicorn app.main:app --no-access-log
```

当前 revision 是 `0001_incident_records`。迁移使用 asyncpg Engine + `connection.run_sync()`
适配 Alembic 的同步迁移函数；网络 IO 仍由异步驱动执行。`alembic.ini` 不存 URL。
应用中没有 `create_all()`。Dockerfile 带有迁移文件，部署时可在同镜像中独立运行
`alembic upgrade head`，默认容器启动命令只启动 API。

`alembic downgrade base` 会删除表及记录，**只在可丢弃的测试数据库验证**；测试随后再次
`alembic upgrade head`。迁移文件不导入可变的 ORM 模型，避免未来模型修改改变旧版本迁移。

### 表结构与持久化边界

实现见 [`app/db/models.py`](app/db/models.py)、[`app/incident/schemas.py`](app/incident/schemas.py)。

| 字段 | PostgreSQL 类型 | 含义 |
|---|---|---|
| `id` | UUID，主键 | Repository 生成 UUID4，客户端不能指定 |
| `session_id` | UUID，可空 | 本次使用的 Redis Session；无外键，过期不影响记录 |
| `status` | VARCHAR(16) | 当前唯一值 `completed`，不支持状态更新 |
| `question` | TEXT | 脱敏问题，1–4000 字符 |
| `answer` | TEXT | 脱敏最终答案，1–16000 字符 |
| `step_summary` | JSONB | 版本化、白名单、受限执行摘要 |
| `duration_ms` | BIGINT | 单调时钟测量 Agent 执行耗时，不含 Session 读取及双存储提交 |
| `created_at` | TIMESTAMPTZ | 开始调用 Agent 的 UTC 时间 |
| `finished_at` | TIMESTAMPTZ | Agent 返回答案的 UTC 时间，必须不早于 created_at |

除 `session_id` 外全部 NOT NULL。数据库 CHECK 限制状态、字符长度、非负耗时、时间顺序、
JSONB 顶层形状/版本和体积。JSONB ≤ 16 KiB；Pydantic 序列化整条记录 ≤ 128 KiB；数据库额外限制
问题、答案、JSONB 文本之和 ≤ 128 KiB。文本禁止 NUL，不截断答案后伪装完整保存。
摘要更细的条目白名单与计数一致性由 Pydantic 验证。

主键自动提供 B-tree 索引；额外索引是 `created_at` 和部分索引 `(session_id, created_at)`
（只覆盖非空 session_id）。不对低选择性的 completed 状态建索引，也暂不建 GIN。

当前只记录完整诊断，不建立 running/failed/cancelled 状态，因此没有顶层 `error_code` 列：
工具失败码在摘要里，终止执行/存储错误通过安全日志与 HTTP/SSE 返回。工具失败后模型仍正常
给出答案，执行状态仍可为 completed；它表示执行结束，不代表所有工具成功或结论已被验证。

```json
{
  "schema_version": 1,
  "tools": [
    {"name": "get_system_metrics", "success": true, "error_code": null},
    {"name": "get_mysql_slow_queries", "success": false, "error_code": "tool_timeout"}
  ],
  "tool_count": 2,
  "truncated": false
}
```

[`SummaryBuilder`](app/incident/summary.py) 只消费 ToolResultStep 的安全元数据，最多保留前 64 项，
`tool_count` 仍统计全部调用（上限 1600），超过 64 项标记 `truncated=true`。
不保存工具参数、call_id、原始结果、错误原文、内部 messages、System Prompt 或隐藏推理。
未知名称统一为 `unregistered_tool`，未知失败码归一为 `tool_execution_error`，防止模型把秘密
藏在元数据中。新增工具时需要审查并更新白名单。

问答沿用 [`redact_content`](app/session/privacy.py)：清除已知 OpenAI Key、完整数据库/Redis URL、
连接密码及常见显式凭据格式。答案仍可能含指标总结；基础正则不能识别任意业务机密，
接入真实工具前仍需领域脱敏、访问权限和数据保留策略。

### 完整调用链与事务

```mermaid
sequenceDiagram
    participant H as HTTP / SSE
    participant S as DiagnosisService
    participant R as Redis Session（可选）
    participant A as Agent
    participant I as IncidentRepository
    participant P as PostgreSQL（可选）
    H->>S: run / stream
    opt session_id
        S->>R: await load（异步 IO）
        R-->>S: 对话快照
    end
    S->>A: await run / async for stream
    Note over A: LLM / Tool 异步调用，期间不借用 PostgreSQL 连接
    A-->>H: 经 Service 转发普通 SSE 工具事件
    A-->>S: 最终答案
    opt session_id
        S->>R: await commit（WATCH/MULTI/EXEC）
    end
    opt 开启 PostgreSQL
        S->>I: await create_completed
        Note over I: 生成 UUID4、脱敏、校验，尚未开始数据库事务
        I->>P: begin → await flush / INSERT
        I->>P: COMMIT（退出 begin 上下文）
        P-->>I: 确认提交，归还连接
        I-->>S: IncidentRecord
    end
    S-->>H: JSON 结果 / SSE final（含可选 incident_id）
```

[`DiagnosisService`](app/services/diagnosis.py) 只协调现有 Agent，未重写 Agent Loop/SSE 编码。
JSON API 和 SSE final 只在所有启用的存储确认提交后才表示成功；关闭持久化时响应字段保持原样。
SSE 的普通工具事件即时转发，final 在 Service 中暂存。开启持久化后成功响应多一个可选 `incident_id`。

[`IncidentRepository.create_completed/get`](app/incident/repository.py) 返回 Pydantic 模型，
不暴露 ORM；get 未找到返回 None。每次调用从同一个 `async_sessionmaker` 创建新的 AsyncSession，
不在共享实例里保存 Session。Engine 和连接池由 lifespan 创建和关闭，`await engine.dispose()`
释放池连接。池最多 5 个常驻 + 5 个溢出连接，等待池 2 秒、连接 2 秒、单数据库命令 5 秒。
`database_session()` 为取消后的 close/rollback 清理屏蔽 AnyIO 取消；业务执行和 COMMIT 仍可取消。

### 双存储失败矩阵

| 发生位置 | Redis 结果 | PostgreSQL 结果 | 客户端行为 |
|---|---|---|---|
| Agent 失败 / final 前取消 | 不写本轮 | 不写记录 | 原 Agent error；取消不补发事件 |
| Redis 冲突、过期、数据错误 | 本轮未接受 | 不调用 PostgreSQL | 原 Session error，无 final |
| Redis 提交响应丢失 | 可能已写 | 不调用 PostgreSQL | session_storage_error，结果可能未知 |
| 无状态：PG 连接/flush 失败 | 无 Session 操作 | 本次未提交 | incident_storage_error / incident_data_error |
| 无状态：PG COMMIT 网络故障 | 无 Session 操作 | 可能已提交 | incident_commit_unknown |
| 有状态：Redis 成功，PG 在 COMMIT 前失败 | 已写本轮 | 本次未提交 | incident_partial_failure |
| 有状态：Redis 成功，PG COMMIT 结果未知 | 已写本轮 | 可能已提交 | incident_partial_unknown |
| 两次提交均确认 | 已写（如启用） | 已写 | JSON 200 / SSE final |
| 写入或提交期间断连取消 | 取决于取消时机 | 未提交或已提交均可能 | 无 final，不承诺回滚已经提交的写入 |

Incident 失败普通 API 返回 HTTP 503（单纯超长记录为 413），SSE 已发送 HTTP 200 后用
error 终止。错误含安全 `code`、`message`、`incident_id`（已分配时）和 `session_committed`；
日志记录相同边界与 request ID，不记录底层异常或问答。确定的约束错误映射 data_error；
进入 COMMIT 后的连接/超时异常保守映射 unknown。

**Redis 与 PostgreSQL 之间不存在原子事务**。Redis 不会因 PG 失败自动回滚，服务不重跑 Agent、
不自动重试写入。运维可按 incident_id 调用内部 get 核查：读到行可确认存在；暂时读不到行
不应在原提交仍可能进行时断言失败。M3 没有公开查询 HTTP API，因为尚无认证与所有权校验。
UUID 仅用于关联，不能代替授权。Outbox、幂等请求与一致性修复属于后续工作。

### 真实 PostgreSQL 验证

测试必须由环境变量显式启用，连接到名称以 `opspilot_test` 开头的专用 bootstrap 数据库，
用户需 CREATEDB。fixture 每次创建随机 `opspilot_test_m3_<uuid>` 数据库，在其中迁移和测试，
最后只删除该数据库；不迁移、不清空 bootstrap 数据库，也不访问已有业务数据库。
Redis 测试只清理自己创建的随机 Key。

```bash
# 先自行创建隔离测试实例与 opspilot_test 数据库；以下地址是格式示例。
export OPSPILOT_TEST_POSTGRES_URL='postgresql+asyncpg://test_user:password@127.0.0.1:15432/opspilot_test'
export OPSPILOT_TEST_REDIS_URL='redis://127.0.0.1:16379/0'
conda run -n opspilot pytest -q
# 只运行 PostgreSQL 测试（含双存储测试）：
conda run -n opspilot pytest -q -m postgres_integration
unset OPSPILOT_TEST_POSTGRES_URL OPSPILOT_TEST_REDIS_URL
```

本次验收使用 `/tmp` 中独立的 PostgreSQL 18.6 / Redis 8.0.5 实例，未改动系统服务或已有数据库。
验收后随机测试数据库及测试 Key 已清理，临时 PostgreSQL/Redis 实例已停止。
[`test_postgres_integration.py`](tests/test_postgres_integration.py) 验证真实 INSERT/SELECT、JSONB
`->` 与 `@>` 查询、约束拒绝及多行整体 rollback、连接归还、取消回滚、ORM/迁移一致性、
CLI `current → downgrade base → upgrade head → current`、提交成功后丢失确认的故障注入，
以及 Redis 会话过期后 PG 记录仍可读取。离线单元测试禁止 asyncpg 建立真实连接。

## V0.2-M4：Redis Cache-Aside 历史摘要查询

M4 缓存的是**已经完成、业务上不可变的 Incident 安全摘要**。它不缓存用户问题、完整答案、
Session 历史、工具 Observation、实时指标或实时诊断结论，也不会按自然语言问题复用 Agent 答案。
每次 `/diagnose` 和 `/diagnose/stream` 仍独立执行原有 Agent，并遵循 M3 的提交顺序。

### 业务入口与数据范围

[`IncidentQueryService.get_summary(id)`](app/services/incident_query.py) 是实际的历史读取入口，
由 [`main.lifespan`](app/main.py) 装配为 `app.state.incident_queries`，使用现有
`IncidentRepository.get(id)` 回源。它没有增加公开 GET API；尚未建立身份认证与所有权校验，
UUID 或缓存命名空间均不能替代访问授权。

本地运维命令使用同一个应用装配和 Service，只读取已存在的诊断摘要：

```bash
# 修改 pyproject.toml 后重新安装以注册 CLI；无需新依赖。
conda run -n opspilot pip install -e ".[dev]"
# 先配置 .env 中 DATABASE_URL / REDIS_URL，并迁移、执行一次诊断获得 incident_id。
incident_id='550e8400-e29b-41d4-a716-446655440000'  # 替换为诊断响应中的实际 UUID4
conda run -n opspilot python -m app.cli.incident_summary "$incident_id"
# 等价的已安装命令；第二次读取在 TTL 内命中：
conda run -n opspilot opspilot-incident-summary "$incident_id"
# 只删除这一条摘要的缓存，确认删除后再查询：
conda run -n opspilot opspilot-incident-summary "$incident_id" --invalidate
```

CLI 输出安全运行日志和摘要 JSON，不运行 Agent、不创建 Incident、不执行迁移。
退出码 0 表示找到，3 表示记录不存在，2 表示持久化未开启或参数不合法，1 表示查询失败或
主动失效未获确认。失效失败时 CLI 不声称已读取最新数据，可关闭 CACHE_ENABLED 后再查询权威库。
内部调用示例（在已有应用 lifespan 内）：

```python
summary = await app.state.incident_queries.get_summary(incident_id)
acknowledged = await app.state.incident_queries.invalidate(incident_id)
```

严格的 [`IncidentSummary`](app/cache/schemas.py) 显式从 M3 模型抽取以下字段，拒绝额外字段：

```json
{
  "schema_version": 1,
  "incident_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "tool_count": 2,
  "tool_names": ["get_system_metrics", "get_mysql_slow_queries"],
  "truncated": false,
  "duration_ms": 1530,
  "created_at": "2026-10-17T02:00:00Z",
  "data_source": "mock_tools"
}
```

`tool_names` 保留 M3 前 64 次调用的顺序和重复名称，不代表去重工具集合；`tool_count` 是总次数，
`truncated` 表示摘要是否截断。工具名沿用 M3 白名单，时间必须 UTC，schema_version 必须明确为整数 1。
`data_source=mock_tools` 表明当前 Tools 都是模拟数据，即使配置真实 LLM 也不能将其视为生产证据。

### Cache Key、TTL 与配置

Key 的唯一构造位置是 [`incident_summary_key`](app/cache/keys.py)：

```text
opspilot:cache:incident_summary:v1:{UUID4}
```

它包含项目、缓存命名空间、数据种类、Schema 版本和服务器生成的业务 ID，不包含用户原文。
Prefix 必须为 `opspilot:cache` 或其冒号分隔的安全子命名空间，整体最长 128 字符；
子段只允许字母、数字、下划线和短横线。不能配置为 `opspilot:session`，不接受通配符。
不兼容的摘要升级需要同时更换 Key 的 v1 与模型版本，并让旧 Key 按 TTL 自然回收。

| 配置 | 默认值 | 约束与含义 |
|---|---|---|
| `CACHE_ENABLED` | false | 开启必须同时开启 INCIDENT_PERSISTENCE_ENABLED，保留权威数据源 |
| `CACHE_TTL_SECONDS` | 300 | 1–604800 秒，所有写入用 SET EX |
| `CACHE_KEY_PREFIX` | opspilot:cache | 受限缓存命名空间，可加测试/环境子段 |
| `CACHE_MAX_BYTES` | 65536 | 1–1048576，UTF-8 JSON 读写上限 |
| `CACHE_IO_TIMEOUT_SECONDS` | 0.5 | 每次缓存 IO 的整体 deadline，范围大于 0 且不超过 10 秒 |
| `REDIS_URL` | 既有 Redis 配置 | Cache 与 Session 使用同一 URL，不重复配置连接池 |

```dotenv
INCIDENT_PERSISTENCE_ENABLED=true
CACHE_ENABLED=true
CACHE_TTL_SECONDS=300
CACHE_KEY_PREFIX=opspilot:cache
CACHE_MAX_BYTES=65536
CACHE_IO_TIMEOUT_SECONDS=0.5
```

Cache 关闭时，已装配的 QueryService 直接查询 PostgreSQL；不创建缓存客户端或执行缓存 IO。
`SESSION_ENABLED=false` 不阻止 Cache 工作。两者都启用时共享一个应用级异步 Redis Client/Pool，
同一客户端只关闭一次；无论请求数量多少，都不按请求新建客户端。
共享连接/命令 socket timeout 仍为 M2 的 2 秒、禁用自动重试，Cache 额外施加较短的操作 deadline。
该 deadline 不是整个业务请求超时；损坏数据可能依次执行 GET、DEL、SET，每次分别限时。

Session TTL 控制不可自动重建的对话状态生命周期，并在成功提交时滑动刷新；Cache TTL 控制
可从 PostgreSQL 重建的副本，普通 GET 不刷新。当前摘要不可变，TTL 主要用于内存回收及版本更新。
[Redis SET EX](https://redis.io/docs/latest/commands/set/) 在同一命令中写入值和有效期，避免 SET 后
再单独 EXPIRE 之间出现无 TTL 窗口；[普通读取不延长有效期](https://redis.io/docs/latest/commands/expire/)。

### Cache-Aside 调用链

```mermaid
flowchart TD
    Q[内部业务 / 本地 CLI] --> S[IncidentQueryService.get_summary]
    S --> C{Cache 是否开启}
    C -->|否| P[IncidentRepository.get → PostgreSQL SELECT]
    C -->|是| G[CacheRepository.get → Redis GET]
    G -->|合法 Hit| R[返回安全摘要]
    G -->|Miss / Redis 错误| P
    G -->|坏 JSON / 版本或 ID 不符 / 超长| D[尝试 DEL 错误 Key]
    D --> P
    P -->|数据库错误| E[原业务异常继续传播]
    P -->|记录不存在| N[返回 None，不缓存空值]
    P -->|找到| W[白名单构建 IncidentSummary]
    W --> F[开启缓存时 SET EX；失败记录日志]
    F --> R
```

[`CacheRepository`](app/cache/repository.py) 只负责 Redis GET/SET EX/DEL、序列化、校验、大小、TTL
和缓存故障日志；不知道 PostgreSQL、Agent 或 SSE。读取检查缓存 ID 与请求 ID 一致，防止串记录。
缓存损坏时尝试删除指定 Key，即使 DEL 失败仍回源；成功回填会覆盖坏值。不缓存 None 或数据库异常。

QueryService 的 PostgreSQL 调用不在缓存异常捕获块中，数据库失败不会被伪装成 None 或缓存成功。
缓存只捕获预期 RedisError、操作超时和明确的数据校验错误；编程错误仍传播，CancelledError
不会转为 Cache Miss。GET 取消不继续回源，SET 取消不会把取消请求当作成功返回。

### Invalidation、Stale Data 与一致性边界

M3 没有生产 UPDATE/DELETE 业务方法，记录创建后不可变，因此 M4 没有为演示强行增加更新 API。
内部 `invalidate(id)` 用单个 DEL，仅删除指定缓存 Key；返回 true 表示已收到删除确认（包括 Key
原本不存在），false 表示未获确认，不保证旧 Key 已消失。它从不扫描/删除 Session Key，也不用 FLUSHDB/FLUSHALL。

未来如果增加可变记录，基本顺序应为 `PostgreSQL COMMIT 成功 → DEL Cache`。COMMIT 失败不能报告
更新成功；DEL 失败则记录错误，并承认旧值可能保留。两者不是一个原子事务。
过期是 TTL 结束；陈旧是缓存值已经落后于权威库，可能仍有正 TTL。TTL 不保证强一致性。

即使提交后删除，也可能发生：A Miss 并读到旧值 → B 提交新值并 DEL → A 把旧值 SET 回去。
[`test_stale_fill_race_is_explicitly_not_strong_consistency`](tests/test_cache.py) 用事件同步复现该顺序。
真实集成测试还在独立测试数据库里受控 UPDATE 耗时，验证未失效时读旧摘要，COMMIT 后失效才回源新值。
生产业务暂不允许这类修改；未来需要版本比较、更新序列、可靠失效通知或特定一致性协议。
运维直接 SQL 修正或删除记录也在不可变契约之外，必须考虑失效和并发在途回填，不能仅凭 DEL 承诺最新读取。

缓存结果不参与 Session version 校验、事务提交、Agent 执行许可或实时诊断决策。
PG 暂时不可用时，合法缓存 Hit 仍可返回历史摘要；Miss 则必须报告 PG 错误。

### 故障降级与可观测性

| 失败 | Cache 查询行为 | 与 Session 的区别 |
|---|---|---|
| Redis GET 失败/超时 | 记录 cache_error，回源 PostgreSQL | Session 读取失败仍返回 SessionError |
| Redis SET 失败/超时 | 返回已经查到的 PG 摘要 | Session commit 失败不能假装成功 |
| Redis DEL 失败/超时 | 返回 false，记录失效未确认 | 不修改已有 Session WATCH/MULTI/EXEC |
| 内容损坏/版本不符/超限 | 尝试删除后回源 | 不把损坏数据交给调用方 |
| PG 查询失败 | 原异常传播，不填充缓存 | PG 仍是权威来源 |
| 客户端取消 | 传播取消，停止后续步骤 | 不在后台重试或补写 |

日志事件包括 `cache_hit`、`cache_miss`、`cache_set`、`cache_invalidate`、`cache_error`、`cache_fallback`。
只记录缓存种类、操作、耗时和固定错误码；不记录 Key/Prefix、凭据、连接字符串、问答或原始工具数据。
这里的 fallback 表示本次去 PostgreSQL 查询，普通 Miss 和缓存故障均可能触发。
没有新增 Prometheus、自动重试、分布式锁、Lua 或后台刷新任务。

### 并发与实际验收

并发 Miss 可以重复回源；每个 PostgreSQL 操作仍独立创建 AsyncSession。Redis 每次 SET EX 原子写入
完整 JSON，因此不会形成半条缓存。真实测试同步两个 Miss，实际观察到 **2 次 SELECT**，随后值完整且带 TTL。
这不是 Singleflight：热点 Key 过期仍有击穿/Stampede 风险，不存在 ID 的重复查询仍有穿透风险，
大量 Key 同时过期或 Redis 故障仍可能造成雪崩和数据库压力。未来按实际流量评估合并请求、有限锁、
TTL 抖动、短期空值缓存、限流和数据库容量，不提前引入复杂协调。

本次使用已有临时运行文件，在新的 `/tmp` 数据目录启动 PostgreSQL 18.6 / Redis 8.0.5，
没有安装系统组件或操作已有数据库。测试复用 [`postgres_support.py`](tests/postgres_support.py)
中的 M3 随机数据库 fixture；Redis Cache 使用随机 `opspilot:cache:m4:<uuid>` 前缀，只删除自己登记的 Key。

| 查询步骤 | 实际 Incident SELECT 增量 |
|---|---:|
| 首次 Cache Miss | 1 |
| 第二次 Cache Hit | 0 |
| 主动 invalidate 后读取 | 1 |
| 强制 TTL 到期后读取 | 1 |

SQL 数字来自 [`test_cache_integration.py`](tests/test_cache_integration.py) 中 SQLAlchemy 的实际
`before_cursor_execute` 计数，不是根据返回结果相同推断。没有做延迟性能基准，不声称某个百分比提速。
真实测试也覆盖 CLI 两次读取、无 Session 的 Cache、有 Session 的共享客户端、缓存失效不影响 Session、
Redis 真实连接拒绝回退，以及同一问题依然执行两次完整 Agent。

```bash
# 需先准备隔离服务及名为 opspilot_test* 的 bootstrap DB，账号具有 CREATEDB。
export OPSPILOT_TEST_POSTGRES_URL='postgresql+asyncpg://test_user:password@127.0.0.1:15432/opspilot_test'
export OPSPILOT_TEST_REDIS_URL='redis://127.0.0.1:16379/0'
conda run -n opspilot pytest -q tests/test_cache_integration.py
conda run -n opspilot pytest -q
unset OPSPILOT_TEST_POSTGRES_URL OPSPILOT_TEST_REDIS_URL
# 默认离线回归：
conda run -n opspilot pytest -q
```

M4 前基线：242 项，默认 **217 passed、25 skipped**。M4 新增 **83 项离线测试、11 项双存储集成测试**。
完成后默认 **300 passed、36 skipped**；显式启用两个数据库后 **336 passed、0 failed、0 skipped**。
按标记统计真实 Redis **18 项**、PostgreSQL **30 项**，其中 **12 项同时使用两者**，不能将这两个数字直接相加。
默认跳过的 36 项仅因未配置集成环境；完整验收没有未执行测试。
验收后已确认随机测试数据库和 Redis 测试 Key 清理完成，并停止本次临时实例；没有提交或推送 Git。

## Tests

M3 开发前在项目 Conda Python 3.11 中实际运行的基线是 **166 passed、6 skipped**。
M3 历史验收结果：启用隔离 PostgreSQL 与 Redis 后 **242 passed、0 skipped、0 failed**；
默认离线 **217 passed、25 skipped、0 failed**（25 项需显式数据库配置）。原有测试全部保留，未放宽断言。
新增 `test_incidents.py` 与 `test_postgres_integration.py` 覆盖数据模型、脱敏与大小、
事务错误映射、提交顺序、SSE final 门控、取消清理、连接生命周期和真实迁移。

```bash
conda run -n opspilot pytest -v
```

V0.1 基线 60 项，V0.2-M1 完成后、本次 M2 修改前基线 **98 passed**。M2 验收时保留所有旧测试，共 **172 项**：
默认离线运行 **166 passed，6 skipped**；真实 Redis 集成测试单独启用后 **6 passed，0 failed**；启用真实 Redis 的完整运行 **172 passed，0 failed**。测试覆盖三个工具、注册表、完整多轮流程、观察值改变答案、并发隔离、
请求校验、MAX_STEPS、未知工具、坏参数、工具异常/超时、模型异常/超时、取消传播、日志脱敏、
真实适配器的请求协议及错误响应。
新增 [`tests/test_stream.py`](tests/test_stream.py) 覆盖 SSE 顺序、JSON 转义、错误终止、
工具失败 Observation、并发隔离、旧接口等价、OpenAPI、提前关闭及 ASGI 2.3/2.4 断连。
取消测试通过事件同步触发，不依赖 sleep 计时断言；ASGITransport 只用来验证完整响应，
不据此声称验证真实网络的逐事件到达时间。`tests/conftest.py` 隔离配置并禁止真实 HTTP transport；
API 单元测试用 ASGITransport，供应商模拟用 MockTransport，没有调用真实模型 API。
非集成测试禁止真实 Redis TCP 连接，FakeRedis 只用于可控单元测试。

新增 `test_session_repository.py`、`test_sessions.py` 验证 Schema、TTL、裁剪、大小限制、存储脱敏、
历史注入、Mock 最新问题、会话隔离、并发冲突、故障降级、生命周期、提交前不发 final 及断连边界。
真实测试在 `test_redis_integration.py`，通过显式环境变量启用，不使用 FLUSHDB/FLUSHALL，
只清理测试自己创建的 UUID Key：

```bash
OPSPILOT_TEST_REDIS_URL=redis://127.0.0.1:16379/0 pytest -v -m redis_integration
# 同一隔离实例上运行完整测试：
OPSPILOT_TEST_REDIS_URL=redis://127.0.0.1:16379/0 pytest -v
```

本次真实 Redis 7.4.11 集成测试覆盖 TTL、过期不复活、WATCH 与过期/并发修改、
两个同时提交仅一个成功、EXECABORT 不部分写入、普通与 SSE 多轮 API。
另通过真实 Uvicorn 18000 端口验证：创建时 version=0，两次普通诊断后 version=2、4 条历史；
第三次 SSE 收到 final 时 Redis 已为 version=3、6 条历史，TTL 每次从 100 刷新回 1800。验收结束后已停止本次 Uvicorn 与独立 Redis 实例。

M1 验收时还实际启动了 Mock 模式 Uvicorn（`--reload --no-access-log`），使用 `curl -N` 验证
订单与 Redis SSE：分别返回 6、8 条事件，Redis 包含 `get_redis_metrics` 调用/结果；
原 JSON 接口返回 `answer + 4 steps`，与订单 SSE 的答案及工具事件一致。验证后已停止服务。

## Docker

```bash
docker build -t opspilot:0.1 .
docker run --rm -p 8000:8000 -e LLM_MODE=mock opspilot:0.1
```

运行容器前应停止占用 8000 端口的开发服务，或映射到其他宿主端口。
镜像使用独立 `python:3.11-slim`、pip 和非 root 用户，不依赖宿主机 Conda。
Dockerfile 已提供；本次验收没有构建或运行 Docker 镜像。没有 docker-compose 或数据库容器。

## Limitations / Technical Debt

- 所有运维数据都是 Mock，不能据此判断生产故障；MockLLM 的规划规则只用于演示。
- 真实供应商仅做离线协议测试，未完成带真实 Key 的联调；专有 reasoning 续传等扩展不支持。
- 已有可选短期 Redis 会话；没有身份认证、会话所有权校验、限流、LLM token Streaming、总请求时间预算或全局并发控制。
- 字符/字节及轮数上限不等于模型 token 预算；没有摘要、幂等提交或冲突结果合并，不应直接暴露到公网。
- 有轮数与单次超时保护，但多工具请求仍可较慢；同一轮多个工具按顺序执行。
- 模型输出只是建议，未实现全面的敏感内容检测；未来接入真实工具前需要输出脱敏和数据权限控制。
  当前 steps 中只有固定 Mock Data；有效工具参数及输出将来若含 Secret，必须在工具边界显式处理。
- 依赖使用兼容版本范围，尚无锁文件或自动更新策略；Docker 镜像也未固定 digest。
- 没有重试退避、费用统计或生产监控，日志为最低限度的运行元数据。
- 历史摘要缓存没有 Singleflight、负缓存、TTL 抖动、可靠失效或命中率聚合监控；首次 Miss 仍读取完整 Incident 后再投影。

## Roadmap

除已标注完成的 M1/M2/M3/M4 外，以下功能仍是规划。

### V0.2

- **M1 已完成**：SSE Agent 执行事件流。
- **M2 已完成**：Redis Conversation Session。
- **M3 已完成**：PostgreSQL Incident Persistence。
- **M4 已完成**：Redis Cache-Aside 历史安全摘要，TTL、主动失效、故障降级、内部查询与本地 CLI。

### V0.3

LangGraph、Checkpoint、Failed Task Resume、Human-in-the-loop。

### V0.4

RAG Runbook、MCP、Prometheus、Grafana、Agent Trace。

### V0.5

pytest integration tests、k6 load testing、Mock LLM benchmark、performance tuning。

M4 已复用内部 `IncidentRepository.get(id)`；认证、租户授权、缓存击穿保护和可靠失效仍需后续设计。
当前未引入 XA、Saga、Outbox 或事件总线。
先完成当前层的学习问答：[我作为 OpsPilot 作者必须能回答的问题](LEARNING.md)。
