# OpsPilot

**OpsPilot — Enterprise AI SRE & Incident Diagnosis Agent**

V0.1 是用于学习和扩展的单 Agent 运维诊断后端基础，不是可直接上线的生产运维平台。
它以 FastAPI、异步 Python、可替换的 LLM 接口、手写 Agent Loop 和工具注册表完成一次完整诊断。
默认 `LLM_MODE=mock`，不需要 API Key，也不访问真实 MySQL、Redis 或生产服务器。

## V0.1 能做什么

- 接收故障描述，经过模型选工具、执行、Observation 回传、多轮调用，返回最终诊断。
- 提供三个只读 Mock Tools；模型可一次返回多个调用，应用按顺序执行。
- 支持 MockLLM 和 OpenAI-compatible Chat Completions 两种模式。
- 提供轮数上限、模型/工具超时、参数校验、错误 Observation、结构化日志和自动化测试。
- `steps` 仅包含实际 Tool Call / Tool Result，不返回隐藏推理或中间模型文字。

## 架构与目录

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
├── api/                    # /health 和 /api/v1/diagnose
├── agent/                  # loop.py、schemas.py、prompts.py
├── llm/                    # base.py、mock.py、openai_compatible.py
├── tools/                  # registry.py 和三个 Mock Tool
└── core/                   # config.py、logging.py
tests/                      # 全部离线测试，包括 HTTP 协议模拟
pyproject.toml              # 唯一 Python 依赖声明
.env.example                # 无 Secret 的配置样例
Dockerfile                  # 独立 Python 3.11 Runtime
LEARNING.md                 # 作者学习问答及代码定位
```

## Agent Loop

核心在 [`app/agent/loop.py`](app/agent/loop.py) 的 `Agent.run()`，HTTP 路由只负责接收和返回数据。

1. 为每个请求创建独立 `messages` 和 `steps`，放入系统提示与用户描述。
2. 从注册表导出工具名称、描述和 Pydantic JSON Schema，传给 `BaseLLM.generate()`。
3. LLM 返回工具调用时，将 assistant 调用消息加入上下文。
4. 按名称查找工具，验证 JSON 参数，再 `await tool.execute(arguments)`。
5. 将成功或失败结果转换成 Observation，以 `role=tool` 和对应 `tool_call_id` 追加到上下文。
6. 再次调用 LLM，由它决定继续获取证据，还是输出最终答案。
7. 没有 Tool Call 且答案非空时正常结束；达到 `MAX_STEPS` 仍无答案则抛出 `AgentExecutionError`。

`MAX_STEPS=6` 指 **LLM 调用轮数**，不是 `steps` 事件数。一次工具执行产生两个事件。
订单慢场景正常需要 3 轮 LLM、2 次 Tool、4 个事件。设置为 2 会在收集完两项证据后失败，
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

[`app/llm/mock.py`](app/llm/mock.py) 根据传入的 Observation 确定下一步，无共享轮次计数。
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
取消信号不会被 `except Exception` 吞掉。程序不向客户端返回 traceback 或第三方异常正文。

`app/core/logging.py` 输出 JSON Lines，记录 `request_start/finish`、`agent_start/step/finish`、
`tool_start/success/failure`、工具耗时、Agent 总耗时和错误码。日志仅接收允许的运行元数据，
不记录 API Key、Authorization、输入正文、模型输出、工具结果或异常原文。
日志中的 `route` 是匹配的路由模板，不是含 query 的原始 URL。没有 Prometheus、Grafana 或 Langfuse 集成。

## Tests

```bash
conda run -n opspilot pytest -v
```

首次完整验收：**60 passed**。测试覆盖三个工具、注册表、完整多轮流程、观察值改变答案、并发隔离、
请求校验、MAX_STEPS、未知工具、坏参数、工具异常/超时、模型异常/超时、取消传播、日志脱敏、
真实适配器的请求协议及错误响应。`tests/conftest.py` 隔离配置并禁止真实 HTTP transport；
API 用 ASGITransport，供应商模拟用 MockTransport，没有调用真实模型 API。

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
- 没有身份认证、限流、持久化、会话、Streaming、总请求时间预算或全局并发控制；不应直接暴露到公网。
- 有轮数与单次超时保护，但多工具请求仍可较慢；同一轮多个工具按顺序执行。
- 模型输出只是建议，未实现全面的敏感内容检测；未来接入真实工具前需要输出脱敏和数据权限控制。
  当前 steps 中只有固定 Mock Data；有效工具参数及输出将来若含 Secret，必须在工具边界显式处理。
- 依赖使用兼容版本范围，尚无锁文件或自动更新策略；Docker 镜像也未固定 digest。
- 没有重试退避、费用统计或生产监控，日志为最低限度的运行元数据。

## Roadmap

以下仅是规划，V0.1 没有提前实现。

### V0.2

SSE Streaming、Redis Session、Redis Cache、PostgreSQL Incident Persistence。

### V0.3

LangGraph、Checkpoint、Failed Task Resume、Human-in-the-loop。

### V0.4

RAG Runbook、MCP、Prometheus、Grafana、Agent Trace。

### V0.5

pytest integration tests、k6 load testing、Mock LLM benchmark、performance tuning。

最值得接着完成的三个功能：SSE 事件流、Redis Session、PostgreSQL 故障记录持久化。
先完成当前层的学习问答：[我作为 OpsPilot 作者必须能回答的问题](LEARNING.md)。
