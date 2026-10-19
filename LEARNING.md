# 我作为 OpsPilot 作者必须能回答的问题

每道题先用自己的话回答，再打开对应代码验证。

| 问题 | 一句核心答案 | 本项目具体对应位置 |
|---|---|---|
| 什么是 Agent Loop？ | 反复让模型根据已有信息选择工具或给出答案，并把执行结果追加到下一轮上下文的有界循环。 | [Agent.stream / run](app/agent/loop.py) |
| Tool Calling 完整过程是什么？ | 工具 schema → 模型生成调用 → 注册表查找 → 参数校验 → 执行 → Observation → 再次调用模型。 | [Agent.stream / _execute_tool](app/agent/loop.py) |
| 是谁决定调用哪个 Tool？ | LLM 决定，Mock 模式由 MockLLM 的确定性策略模拟这一决定。 | [MockLLM.generate](app/llm/mock.py) |
| Tool 参数是谁生成的？ | LLM 按 schema 生成 JSON 参数，应用负责校验；当前无参工具生成 `{}`。 | [ToolCall](app/llm/base.py)、[参数校验](app/agent/loop.py) |
| Tool 执行结果为什么还要送回 LLM？ | 模型只有读到实际结果，才能以证据形成结论或决定还需哪些数据。 | [role=tool 消息追加](app/agent/loop.py)、[历史验证测试](tests/test_agent.py) |
| 为什么一个 Agent 可能调用 LLM 多次？ | 工具选择和根据结果再次决策发生在不同阶段，订单示例两次选工具后还需一次生成答案。 | [MockLLM.generate](app/llm/mock.py)、[三轮测试](tests/test_agent.py) |
| Agent 什么时候结束？ | 模型返回无 Tool Call 的非空答案时正常结束，调用失败或轮数耗尽时异常结束。 | [Agent.stream / run](app/agent/loop.py) |
| 如何避免 Agent 无限循环？ | 使用配置的 MAX_STEPS 限制模型轮数，并对每次模型和工具调用设置超时。 | [Settings](app/core/config.py)、[有界 for 循环](app/agent/loop.py) |
| Tool 执行失败怎么办？ | 记录安全错误码并生成失败 Observation，模型可以修正、换工具或说明证据不足。 | [Agent._execute_tool](app/agent/loop.py)、[MockLLM 失败分支](app/llm/mock.py) |
| Tool 不存在怎么办？ | 注册表返回 None，循环生成 tool_not_found 错误而不执行任意代码。 | [ToolRegistry.get](app/tools/registry.py)、[未知工具测试](tests/test_agent.py) |
| 为什么需要 Tool Registry？ | 它把名称和工具实现解耦，让增添工具无需修改 Agent 的控制流程。 | [ToolRegistry](app/tools/registry.py)、[build_registry](app/main.py) |
| 为什么 Agent Loop 不能依赖 OpenAI 具体 SDK？ | 依赖 BaseLLM 这个稳定接口才能替换供应商、Mock 和传输方式而不改循环。 | [BaseLLM](app/llm/base.py)、[OpenAICompatibleLLM](app/llm/openai_compatible.py) |
| 为什么 MockLLM 很重要？ | 它让没有 Key、没有外部模型网络时也能真正运行工具选择和多轮证据回传流程。 | [MockLLM](app/llm/mock.py)、[默认配置](app/core/config.py) |
| 为什么 MockLLM 有利于自动化测试？ | 确定性输出使调用顺序、轮数和最终结果可重复断言，避免模型漂移与外部服务波动。 | [test_mock_full_flow_and_observation_history](tests/test_agent.py)、[网络隔离](tests/conftest.py) |
| 为什么 MockLLM 以后有利于性能压测？ | 它可以隔离模型网络延迟来测应用编排吞吐，但不能代替真实模型的端到端性能数据。 | [MockLLM](app/llm/mock.py)、[V0.5 Roadmap](README.md) |
| 为什么 FastAPI Route 使用 async？ | 路由能 await 整条 Agent 调用链，让 IO 等待期间事件循环有机会服务其他请求。 | [diagnose 和 get_agent](app/api/diagnose.py)、[health](app/api/health.py) |
| 为什么 Tool 接口使用 async？ | 未来数据库、监控和 HTTP 查询主要等待 IO，提前统一异步协议便于接入异步客户端。 | [Tool.execute](app/tools/registry.py)、[三个 Mock Tool](app/tools/) |
| async 和多线程是什么关系？ | async 是在 await 处协作让出执行权的并发方式，多线程是另一种执行机制，async 本身不等于并行或自动开线程。 | [asyncio.timeout 与 await](app/agent/loop.py)、[async HTTP client](app/llm/openai_compatible.py) |
| Conda 解决了什么问题？ | Conda 隔离 Python 3.11 解释器和环境，pip 按 pyproject 安装本项目的 Python 依赖。 | [Development Environment](README.md)、[依赖声明](pyproject.toml) |
| 为什么 Docker 不能依赖宿主机 Conda 环境？ | 镜像必须携带自己的 Python Runtime 和依赖才能在其他机器上独立运行。 | [Dockerfile](Dockerfile) |
| 为什么生产环境不能直接暴露模型 Chain-of-Thought？ | 隐藏推理不是可审计事实且可能含敏感内容，用户应看到最终结论、证据和真实执行事件。 | [公开响应 schema](app/agent/schemas.py)、[适配器字段筛选](app/llm/openai_compatible.py)、[reasoning 排除测试](tests/test_openai_compatible.py) |

练习：将 `MAX_STEPS` 设成 2，观察为什么两次工具执行成功仍不能返回最终答案；再恢复为 6。
练习：阅读 `test_mock_uses_observation_values_not_canned_diagnosis`，确认模拟答案确实取自 Observation。


## V0.2-M1：SSE 与异步执行事件

| 问题 | 一句核心答案 | 本项目具体对应位置 |
|---|---|---|
| 什么是 SSE？ | 服务端在一个 HTTP 响应中不断写入文本事件，客户端逐条读取。 | [SSEResponse](app/api/sse.py) |
| 为什么使用 text/event-stream？ | 它声明响应体遵守 SSE 事件帧协议，让消费者按事件边界解析。 | [响应头](app/api/sse.py) |
| 什么是异步生成器？ | async def 中带 yield 的函数，调用后得到可逐次 await 取值的异步迭代对象。 | [Agent.stream](app/agent/loop.py) |
| yield 和 return 有什么区别？ | yield 交出一个事件并暂停，下一次取值才继续；无值 return 结束异步生成器，不能用 return 返回诊断对象。 | [final 后 return](app/agent/loop.py) |
| AsyncIterator 是什么？ | 声明消费者可通过 async for 逐项获取某种类型的值，不承诺一次生成所有值；需要提前退出时还要按生成器契约关闭资源。 | [stream 返回类型和 aclosing](app/agent/loop.py) |
| 为什么 StreamingResponse 能逐步发送？ | 它 async for 消费 body_iterator，每次得到一个块就 await ASGI send，不必等完整结果。 | [SSEResponse.stream_response 的父类调用](app/api/sse.py) |
| AgentEvent 和 ToolResultStep 有什么区别？ | AgentEvent 是所有事件的带判别字段联合类型，ToolResultStep 是其中一种，直接复用普通响应的工具结果结构。 | [事件联合](app/agent/events.py)、[Step](app/agent/schemas.py) |
| Agent.stream 为什么不拼 event/data 文本？ | Agent 只表达业务执行事件，SSE 编码留在 API 层，未来可复用给 WebSocket 或日志消费者。 | [encode_sse](app/api/sse.py) |
| 为什么不能创建两套 Agent Loop？ | 轮数、校验、超时和 Observation 语义容易漂移；run 只收集 stream 的工具事件并用 final 组装响应。 | [Agent.run](app/agent/loop.py)、[两接口等价测试](tests/test_stream.py) |
| tool_call 为什么必须在执行前 yield？ | 客户端才能先看到准备执行的工具；参数必须先校验，失败时只公开空参数和安全错误。 | [_execute_tool](app/agent/loop.py)、[执行前产出测试](tests/test_stream.py) |
| 中文或换行能否破坏 SSE？ | 标准 JSON 把字符串内换行和引号转义，data 保持单行，每帧独立以空行结束。 | [encode_sse](app/api/sse.py)、[注入测试](tests/test_stream.py) |
| 为什么 SSE 开始后不能改 HTTP 状态码？ | 响应头已发送，后续只有响应体可写，所以用 error 终止事件传递失败。 | [stream_diagnosis](app/api/diagnose.py) |
| 工具失败是否就是 error 终止事件？ | 不是；tool_result 的 success=false 仍回传 Observation，只有执行无法继续才发送 error。 | [工具失败测试](tests/test_stream.py) |
| 客户端断开如何触发取消？ | 响应任务组监听 http.disconnect 并取消发送任务，将取消传播到当前 await 的 LLM 或 Tool。 | [SSEResponse.__call__](app/api/sse.py)、[ASGI 断连测试](tests/test_stream.py) |
| 为什么仅检测发送失败不够？ | 模型等待 IO 时暂时没有 send，独立且受管理的监听任务才能及时通知取消。 | [断连监听](app/api/sse.py) |
| 为什么不能吞掉 CancelledError？ | 它代表停止工作；吞掉可能让后续工具继续运行，甚至伪装成成功；它继承 BaseException，不被 except Exception 捕获。 | [取消传播测试](tests/test_stream.py) |
| 为什么 async for 提前退出还要 aclosing？ | 退出循环不保证立即关闭生成器，aclosing 明确执行 aclose，让嵌套生成器和 finally 及时清理。 | [run、stream 和 stream_diagnosis](app/agent/loop.py)、[API](app/api/diagnose.py) |
| 为什么调整日志中间件？ | 旧 call_next 拿到响应头就结束计时；纯 ASGI 中间件等到流结束或取消才记录 request_finish 并重置上下文。 | [RequestLoggingMiddleware](app/core/logging.py) |
| HTTP 200 能否证明流式诊断成功？ | 不能，客户端必须收到 final 才确认正常完成；error 是业务失败，没有终止事件的断连是未完成。 | [README 客户端示例](README.md) |
| 为什么浏览器不能直接用 EventSource 调用本接口？ | EventSource 不支持 POST 请求体，本项目使用 fetch、ReadableStream 和 AbortController。 | [README 请求示例](README.md) |
| ASGITransport 能证明真实实时到达吗？ | 它通常缓冲完整响应，只适合验证内容；用生成器推进测试和 ASGI 事件同步验证发送顺序与取消，网络调用另用 curl 验证。 | [test_stream.py](tests/test_stream.py) |
| Redis Session 应该接在哪里？ | 由请求服务层加载独立上下文、消费执行事件并保存结果，连接生命周期放在 lifespan，不能在共享 Agent 上保存请求历史。 | [get_agent](app/api/diagnose.py)、[lifespan](app/main.py) |

练习：把 MAX_STEPS 设为 2，对比普通接口 HTTP 503 与 SSE HTTP 200 后的 max_steps_exceeded 事件。
练习：阅读 test_call_is_yielded_before_execution_and_contains_validated_arguments，解释为什么关闭第二个 tool_call 后工具不会执行。
练习：阅读 test_asgi_disconnect_cleans_up_without_next_tool，解释为什么事件同步比 sleep 断言更可靠。

## V0.2-M2：Redis 多轮 Conversation Session

| 问题 | 本项目中的答案 | 代码位置 |
|---|---|---|
| redis.asyncio 如何工作？ | 每个命令通过异步连接发送 Redis 协议并等待响应，await 时允许事件循环处理其他请求；创建 Redis 对象本身不表示已连接。 | [lifespan](app/main.py)、[SessionRepository](app/session/repository.py) |
| 异步 Redis 与同步 Redis 有什么区别？ | 同步网络等待会占住当前线程；本项目所有实际 Redis IO 都 await，不能在 async 路由直接调用同步客户端。 | [create/load/commit](app/session/repository.py) |
| 为什么在 lifespan 管理 Redis？ | 启用时创建一个带连接池的应用级客户端，各请求复用；退出 await aclose，避免每个请求重新建连接。 | [create_app.lifespan](app/main.py)、[生命周期测试](tests/test_sessions.py) |
| 为何启动时不强制 ping Redis？ | Session 是可选能力，强制健康检查会让 Redis 故障阻断无状态诊断；只有会话 IO 才连接并报告存储错误。 | [lazy client 创建](app/main.py)、[故障降级测试](tests/test_sessions.py) |
| TTL 与 Sliding Expiration 是什么？ | 创建设置有效期，只有成功提交一整轮对话才把 TTL 刷新到配置值；读、失败和冲突均不续期。 | [SET NX EX / SET XX EX](app/session/repository.py) |
| Session TTL 与 Cache TTL 有什么不同？ | Session TTL 控制对话存续；业务缓存 TTL 控制指标或查询结果的新鲜度，本版本没有实现后者。 | [session 配置](app/core/config.py)、[README](README.md) |
| Pydantic JSON Serialization 做了什么？ | model_dump_json 编码模型，model_validate_json 把 Redis 字节解析为经过校验的模型；不会反序列化任意 Python 对象。 | [_encode/_decode](app/session/repository.py) |
| 从 Redis 读出的数据为什么仍需验证？ | 存储可能损坏或被外部改写，必须检查大小、UUID、版本、UTC 时间、允许的角色和完整轮次，不能直接送给模型。 | [ConversationSession.validate_history](app/session/schemas.py)、[损坏数据测试](tests/test_session_repository.py) |
| Session Boundary 是什么？ | 本项目只存用户消息与 Assistant 最终答案，System Prompt、ToolCall、原始 Observation、Reasoning、异常详情不进入 Redis。 | [ConversationTurn](app/session/schemas.py)、[commit](app/session/repository.py) |
| Conversation History 与 Execution Trace 有什么区别？ | 前者帮助模型理解连续问题，后者记录一次执行的工具与证据；本阶段只保存前者，后者留给后续 Incident Persistence。 | [Session Schema](app/session/schemas.py)、[AgentEvent](app/agent/events.py) |
| 最终答案包含 CPU 指标是否等于存了 Observation？ | 不是存储原始工具协议，但答案文字仍可能总结指标；因此历史只作为对话背景，本轮仍要重新采集证据。 | [MockLLM.generate](app/llm/mock.py)、[服务提交](app/services/diagnosis.py) |
| 历史如何注入 Agent？ | run/stream 接收 ConversationTurn 序列，重新校验并转为独立 Message，排在系统提示之后、当前用户问题之前。 | [Agent.run/stream](app/agent/loop.py) |
| 为什么不用 self.messages 保存历史？ | 应用共享 Agent，实例可变历史会造成请求间污染；每次 stream 都创建新的列表和 Message 对象。 | [历史隔离测试](tests/test_sessions.py) |
| MockLLM 为什么不能取第一个 User？ | 第一个 User 现在可能是旧轮次；它取最新 User，并只统计其后的 Observation，避免上一轮问题驱动当前工具选择。 | [current_user](app/llm/mock.py) |
| Mock 测试是否证明真实多轮理解？ | 只能证明历史正确传入、模拟策略选对工具；真实语言理解需要真实 Provider 和独立评估。 | [CapturingLLM](tests/test_sessions.py) |
| 如何限制历史增长？ | 保留最近 N 个完整对话对，同时限制单条字符数和整体 UTF-8 JSON 字节数；超限拒绝提交，不切半轮。 | [commit/_encode/_decode](app/session/repository.py) |
| 为什么字符数限制不等于 token 预算？ | 不同模型的分词不同；M2 没有 tokenizer 计数和摘要，仍需后续增加模型上下文预算。 | [Settings](app/core/config.py)、[README 限制](README.md) |
| 什么是 Lost Update？ | 两请求基于同一版本算出结果，后写者覆盖前写者历史；version + WATCH/MULTI/EXEC 让后提交的旧快照失败。 | [commit](app/session/repository.py)、[并发测试](tests/test_redis_integration.py) |
| Redis WATCH 到底保护什么？ | 从 WATCH 到 EXEC 之间任何被观察 Key 的更改都会使事务失败；还要与请求开始时的 version 比较，覆盖 Agent 执行期间的更新。 | [WATCH → GET → version 比较](app/session/repository.py) |
| 为什么不能只比较版本再普通 SET？ | 比较与写入之间仍有竞争窗口，另一个请求可插入提交；Redis WATCH 把该窗口内的变化转为事务失败。 | [真实并发事务测试](tests/test_redis_integration.py) |
| MULTI/EXEC 能否回滚所有错误？ | Redis 事务不是通用 SQL 回滚；本实现只排一个 SET 同时更新 JSON 和 TTL，避免多个业务写命令部分成功。 | [commit](app/session/repository.py)、[EXECABORT 测试](tests/test_redis_integration.py) |
| 如何防止过期会话复活？ | 提交重新 GET 检查存在性，WATCH 检测读取后过期，SET XX 又要求 Key 已存在；不会为缺失会话补建。 | [commit](app/session/repository.py)、[真实过期测试](tests/test_redis_integration.py) |
| 为什么不自动重试并发冲突？ | 最终答案基于旧历史计算，不能仅重试写入；返回 409 或 SSE error，让新诊断请求加载最新历史。 | [SessionError](app/session/errors.py)、[DiagnosisService](app/services/diagnosis.py) |
| 为什么 Session SSE 的 final 要暂存？ | Agent 完成还不等于会话提交完成；Service 先 await commit，再 yield final，失败只发送 error。 | [DiagnosisService.stream](app/services/diagnosis.py)、[提交门控测试](tests/test_sessions.py) |
| 客户端取消会不会保存半轮？ | 诊断未完成时不调用 commit；事务写入是完整 User/Assistant 对。提交成功后再取消不能回滚，确认丢失时提交结果可能未知。 | [断连边界测试](tests/test_sessions.py) |
| 为什么 Pipeline reset 要屏蔽 AnyIO 取消？ | SSE 的取消作用域会反复打断 await；仅屏蔽事务清理能完成 UNWATCH/连接归还，执行和提交仍保持可取消。 | [commit finally](app/session/repository.py) |
| Redis 不可用时如何降级？ | 无 session_id 时直接调用原 Agent；有状态请求返回安全 storage_error，禁用时明确 disabled，不偷偷创建内存会话。 | [DiagnosisService](app/services/diagnosis.py)、[故障测试](tests/test_sessions.py) |
| 如何避免存储凭据？ | 写入边界移除已知配置密钥及常见显式凭据形式；不记录消息、ID、Redis URL 或底层异常。通用业务机密识别仍未解决。 | [privacy.py](app/session/privacy.py)、[SessionError](app/session/errors.py) |
| UUID4 是否就是访问授权？ | 不是；M2 没有用户认证与会话所有权校验，随机 ID 不能代替权限检查，不应公开部署。 | [创建接口](app/api/sessions.py)、[README 安全边界](README.md) |
| 如何证明事务不是仅被 FakeRedis 模拟？ | 默认单元测试用 FakeRedis，显式配置 OPSPILOT_TEST_REDIS_URL 后运行真实 Redis 测试；只清理测试创建的随机 Key。 | [test_redis_integration.py](tests/test_redis_integration.py) |

练习：把同一个 Session 同时发给两个请求，观察为什么普通 API 是一个 200、一个 409，而 SSE 是一个 final、一个 error。
练习：先手动缩短开发 Session 的 TTL，分别读取、执行失败、成功诊断，检查只有成功提交会续期。
练习：阅读 test_session_sse_disconnect_cleans_up_and_commit_boundary，解释 llm/watch/committed 三种断连点为什么产生不同保存结果。
练习：检查 Redis JSON，区分 Assistant 最终答案中的指标总结与不能保存的原始 ToolResult 协议。

## V0.2-M3：异步 PostgreSQL 与持久化诊断记录

本节对应已经实现的代码。`IncidentRecord` 表示一次完成的模拟诊断执行，不是生产事故判定。
M3 只存 `completed`，Agent 失败或 final 产生前取消不落库；没有为未来状态机预建空行。

### 1. 什么是 async DB？

在 [`app/incident/repository.py`](app/incident/repository.py)，`await session.flush()`、
`await session.get()` 及事务退出时的 COMMIT 通过 asyncpg 等待 PostgreSQL 的网络响应。
等待时当前协程让出事件循环，其他请求可以继续执行。`async def` 本身不使同步驱动变成异步；
因此 `DATABASE_URL` 只允许 `postgresql+asyncpg://`，不能替换成同步 psycopg URL。
ORM 对象构造、白名单摘要、Pydantic 校验和脱敏仍是本地 CPU 工作，并不自动并行。

### 2. AsyncEngine、Connection 与 AsyncSession 有什么区别？

[`create_database_engine`](app/db/session.py) 创建应用级 AsyncEngine，持有数据库方言配置和连接池。
Connection 是从池借出的一条数据库连接的包装，SQL 最终通过它发送。
AsyncSession 管理一次操作的 ORM 对象、事务状态与连接使用，直到需要 SQL 时才向 Engine 借连接。
在 [`main.lifespan`](app/main.py) 中共享 Engine 和 `async_sessionmaker(engine, expire_on_commit=False)`
工厂；[`IncidentRepository`](app/incident/repository.py) 每次 create/get 都调用工厂创建新 Session。

Session 是可变事务上下文，两个并发请求不能共享一个 Session，否则会争用同一事务、连接和对象状态。
`expire_on_commit=False` 防止提交后属性读取引起隐式刷新；Repository 对外仍返回独立 Pydantic 数据模型。

### 3. 连接池何时借出与归还连接？

Engine 构造时没有网络连接。通常第一次 `flush()` 或 `get()` 时借出连接，池中没有空闲连接时
按上限新建或等待。本实现 `pool_size=5`、`max_overflow=5`，并使用 pre_ping 检测失效的池连接。
事务结束后连接归还池，Session 关闭再释放剩余资源；应用退出 `await engine.dispose()` 关闭池。
池大小是**每个进程**的设置，多个 Uvicorn worker 会各自创建池，部署时应一起计算连接预算。

真实测试 [`test_real_http_lifespan_pool_and_agent_does_not_borrow_connection`](tests/test_postgres_integration.py)
在 LLM 调用期间断言 checkedout 为 0，并验证请求结束后归还、lifespan 退出后关闭连接。

### 4. 为什么 LLM 推理期间不能持有事务？

一次诊断可能多轮 LLM、多个工具，每次耗时数秒。若在调用前 begin 并借出连接，池会被长时间占满，
事务也可能持有锁和旧快照，影响数据库并发与清理。`DiagnosisService` 先执行 Agent、提交 Redis，
最后才调用 IncidentRepository。脱敏、UUID 生成与 Schema 校验也在数据库事务外。
SSE 过程中转发工具事件不会借出 PostgreSQL 连接。

### 5. async with session.begin() 做了什么？

[`create_completed`](app/incident/repository.py) 的核心是：

```python
async with database_session(self._sessions) as session:
    async with session.begin():
        session.add(row)
        await session.flush()
        commit_started = True
# begin 正常退出时 COMMIT；异常退出时 ROLLBACK。
```

`begin()` 创建显式事务上下文，但不意味着立即发送 SQL 或借到连接。
成功退出内部块时提交，异常退出时回滚；外层 `database_session` 在 finally 中关闭 Session。
所有业务写入应处于同一个 PostgreSQL 事务内；当前只写一行，也仍要得到 COMMIT 确认才能报告成功。
集成测试的 CREATE DATABASE 必须使用 AUTOCOMMIT，这是数据库管理命令的限制，**不是业务 INSERT 的模式**。

### 6. flush 与 commit 的区别是什么？

`session.add(row)` 只把对象加入工作单元。`await session.flush()` 发送 INSERT，让数据库检查类型、
NOT NULL 和 CHECK；此时行尚未持久提交，其他普通事务看不到未提交行，后续异常仍可撤销。
退出 `session.begin()` 才提交事务。`commit_started=True` 只是记录“即将进入提交阶段”，
不能被解释为“已经提交成功”；Repository 返回才表示应用已收到提交确认。

### 7. 为什么异常需要 rollback？PostgreSQL ACID 与 Redis 事务有何差别？

PostgreSQL 事务的原子性保证一组写入全部提交或撤销；约束保证一致性，隔离级别控制并发可见性，
持久性由 WAL、提交和数据库部署配置共同提供。发生 SQL 错误后事务会处于失败状态，必须 rollback
才能继续正常使用连接。`session.begin()` 负责正常异常路径，`database_session()` 的 shielded close
处理 AnyIO 重复取消下的资源清理，不把 COMMIT 或 Agent 本身放进 shield。

[`test_real_constraints_rollback_entire_transaction`](tests/test_postgres_integration.py) 先 INSERT 合法行，
再 INSERT 非法行，最后断言两行都不存在；这说明不能把 flush 当作提交。
Redis MULTI/EXEC 保证队列执行期间不被其他命令交错，但不提供 PostgreSQL 那样的通用错误回滚。
M2 因而只排一个 SET，同时更新完整 JSON 和 TTL，再用 WATCH 与 version 防止旧快照覆盖。

### 8. Repository Pattern 的作用是什么？

[`IncidentRepository`](app/incident/repository.py) 封装 create/get、事务、ORM 转换和数据库错误映射。
它不执行 Agent、访问 Redis、编码 SSE 或决定 HTTP 状态。Service 依赖业务方法，API 依赖受控模型，
将来调整持久化细节不会迫使 Agent 修改。Repository 可共享，因为其成员只有 Session 工厂和脱敏配置；
实际 Session 属于单次调用。当前不自动重试，尤其不会重跑基于旧历史生成答案的 Agent。

### 9. ORM Model 与 Pydantic Schema 如何分工？

[`IncidentRow`](app/db/models.py) 描述表、原生 UUID、TIMESTAMPTZ、JSONB、索引和 CHECK，
是 SQLAlchemy 的数据库映射。它不应该直接返回 HTTP。
[`IncidentRecord`](app/incident/schemas.py) 负责 UUID4、UTC 时间、终态、长度、字节数、摘要白名单等
业务校验。读取使用 `model_validate(row)`，写入使用模型字段构造 ORM 行。
数据库约束和应用校验互补：前者保护直接 SQL 写入的基本合法性，后者提供更细的业务结构边界。

### 10. JSON 和 JSONB 有什么区别？为什么用 JSONB？

PostgreSQL `json` 保留输入文本形式（包括空白与键顺序），查询需要重新解析。
`jsonb` 存为解析后的结构，不保留这些文本细节，重复对象键只保留一个值；它支持结构查询与索引。
工具执行次数和条目数量会变化，适合放入版本化的结构化摘要，不需要为每个工具增加列。

本项目 JSONB 只有 schema_version、tools、tool_count、truncated；条目只有 name、success、error_code。
`schema_version=1` 是结构协议，未来改变结构需要明确兼容旧记录，不能靠猜测字段含义。
数据库校验顶层类型和版本；Pydantic 进一步校验白名单、条目数、计数与截断标志的一致性。

### 11. JSONB 与关系字段怎么选择？何时加 GIN？

ID、会话关联、状态、时间、耗时和问答有固定含义，适合普通列，可以施加类型、CHECK 和 B-tree 索引。
可变工具摘要放 JSONB。把所有字段塞入 JSONB 会削弱约束、增加查询与演进成本。
内部查询目前仅用主键 `get(id)`；集成测试展示了以下 SQL 的等价查询：

```sql
SELECT step_summary ->> 'schema_version' FROM incident_records;
SELECT id FROM incident_records
WHERE step_summary @> '{"tools":[{"name":"get_system_metrics"}]}'::jsonb;
```

`->` 返回 JSONB，`->>` 返回文本，`@>` 测试包含关系。只有实际大量使用包含、键存在等查询，
并经执行计划证明需要时，才考虑 GIN；GIN 会增加存储和写入成本。本阶段没有凭空添加 GIN，
也没有为 JSONB 查询建立公共 HTTP 接口。

### 12. 为什么要 Alembic？

[`alembic/versions/0001_create_incident_records.py`](alembic/versions/0001_create_incident_records.py)
是可审核、可重放的版本化结构变化。`alembic current` 读取当前版本，`upgrade head` 补齐迁移，
`downgrade base` 删除本版本表。`create_all()` 不能替代已有结构的版本演进，也不应让每个
应用进程在启动时竞争执行 DDL。

[`MigrationSettings`](app/db/config.py) 单独读取 MIGRATION_DATABASE_URL，可使用专门 DDL 账号；
不自动复用应用账号。[`env.py`](alembic/env.py) 使用 async Engine、NullPool 和 run_sync 适配
Alembic。生产先迁移再启动；降级会丢数据，只能经过评估后执行。本次降级验证全部发生在 fixture
自己创建的随机数据库。迁移与 ORM 是否一致由 `alembic check` 的真实集成测试检查。

### 13. 为什么 Session 与 IncidentRecord 分开？

Session 是下一轮 Agent 的短期上下文，只保存完整 User/Assistant 对，TTL 随成功提交刷新。
IncidentRecord 是一次执行的长期结果，可无 Session，也不因 Session 过期被删除。
`session_id` 是逻辑关联，不是 PostgreSQL 外键；M3 不把 PostgreSQL 当作会话恢复来源。
真实双存储测试通过让 Redis Key 过期后再读取 Incident，证明两者生命周期独立。

### 14. 为什么两个提交不能保证原子性？

Redis 与 PostgreSQL 没有共同事务管理器。`DiagnosisService._complete()` 明确执行
`Redis commit → IncidentRepository.create_completed → 返回结果`，只能保证顺序，不能保证原子性。
Redis 冲突时不会写 PostgreSQL；Redis 成功后 PG 失败时，Redis 已接受本轮，不会自动撤销。
使用 `incident_partial_failure` 或 `incident_partial_unknown`，并返回/记录 session_committed=true，
让客户端知道不能把失败当作两个存储都没写。详细矩阵见 README。

### 15. PostgreSQL COMMIT 响应丢失怎么办？

若 flush/连接阶段失败且尚未进入 COMMIT，本次不会成为已提交记录。
若已进入 COMMIT，网络错误可能发生在提交执行前，也可能发生在提交成功后的确认传输阶段，
因此保守返回 `incident_commit_unknown`。确定的约束拒绝仍属于 data_error。
生成的 incident_id 出现在错误和安全日志中，运维可用内部 `get(id)` 核查。
查到行可确认存在；原提交尚未确定结束时，暂时查不到不能证明不会提交。

[`test_real_lost_commit_response_is_unknown_and_row_may_exist`](tests/test_postgres_integration.py)
先实际提交，再注入响应丢失错误，断言服务报 unknown 而行仍存在。这是受控故障注入，
并非声称实际拔掉网络。M3 不自动补写或重跑 Agent，后续需请求幂等键、结果恢复和一致性修复。
SSE 取消也不承诺已经提交的数据回滚，final 缺失不能证明数据库无记录。

### 16. 如何避免保存敏感 Observation？

[`SummaryBuilder`](app/incident/summary.py) 逐项复制白名单元数据，不对整个 steps 做 model_dump。
未知工具名称归一化，错误原文丢弃；只保留前 64 项但统计全部调用，JSONB 限制 16 KiB。
问答在 Repository 边界复用 M2 脱敏，并注入 OpenAI Key、完整连接 URL 与密码作为已知秘密。
整条模型 JSON 限制 128 KiB，数据库还有基本字节约束，超长答案拒绝保存。

基础脱敏不等于完整 DLP；自然语言问答仍可能含业务机密。当前工具只生成 Mock 数据，真实工具
接入必须审核字段、权限和保留期。未实现认证，所以只有内部 Repository.get，没有任意读取
用户问答的公共 GET API。UUID 是定位符，不是访问控制。

### 调用链复盘

`HTTP → DiagnosisService → Agent → Redis Session（可选）→ IncidentRepository → PostgreSQL → Response`

- HTTP 请求读取、真实 LLM/Tool、Redis load/commit、PostgreSQL SQL 和 SSE send 是异步 IO；当前 Mock Tools 的方法虽然 async，内部不访问网络。
- PostgreSQL 事务仅覆盖 Repository 的 INSERT/SELECT 及 COMMIT/ROLLBACK，不跨越 Agent 或 Redis。
- SSE 普通事件随 Agent 执行产出，Service 暂存 final，必要提交全部确认后才发送。
- Agent 失败、取消、Redis 冲突没有“补存成功记录”；存储失败不自动重跑 Agent。

练习：对比 flush 后失败与 COMMIT 后丢失确认的两个真实测试，解释为什么前者行数是 0，后者是 1。
练习：把 INCIDENT_PERSISTENCE_ENABLED 关闭，验证旧 API 不返回 incident_id，也不创建 Engine。
练习：说明扩展真实工具时，应修改摘要白名单、数据脱敏策略和来源标识的哪些位置。

参考：[SQLAlchemy 异步文档](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)、
[Alembic 异步迁移](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic)、
[PostgreSQL JSON 类型](https://www.postgresql.org/docs/current/datatype-json.html)。

## V0.2-M4：历史 Incident 摘要的 Redis Cache-Aside

学习入口是 [`IncidentQueryService`](app/services/incident_query.py)，缓存实现是
[`CacheRepository`](app/cache/repository.py)，本地命令是 [`incident_summary.py`](app/cli/incident_summary.py)。
M4 只缓存不可变的 completed 历史摘要；它不参与 Agent 实时执行或 M2/M3 的成功提交判断。

### 1. 什么是 Cache-Aside？

调用方显式协调缓存和权威库，而不是让数据库自动管理缓存。`get_summary()` 先 await cache.get，
未命中才 await incidents.get，再构造 `IncidentSummary` 并 await cache.set。
缓存关闭时直接回源；数据库没有记录就返回 None，不保存空值或异常。
缓存模块不认识 PostgreSQL，IncidentRepository 也不认识 Redis，编排集中在 QueryService。

### 2. Cache Hit 和 Cache Miss 是什么意思？

Hit 是 Redis 有值，并且摘要的 JSON、Schema 版本、UUID、字段类型和字节数都校验成功。
Miss 是 Key 不存在。损坏值不是合法 Hit，会记录 cache_data_error、尝试删除并回源；
Redis 故障也回源，但单独记录 cache_error，避免把所有失效原因混为一种。
[`test_cache_aside_miss_hit_invalidate_reduces_queries`](tests/test_cache.py) 的源 Repository 调用增量为 1、0、1。

### 3. 为什么先查 Redis，再查 PostgreSQL？

同一历史摘要重复读取时，Hit 可以减少 PostgreSQL SELECT 和连接池占用。
若先查 PostgreSQL 再检查 Redis，已经支付了数据库查询成本，不是本项目要验证的优化。
代价是 Miss 多出一次 Redis GET 和一次回填 SET；低命中率场景可能更慢，因此默认不开启，
本次只证明查询次数减少，没有宣称某个百分比的端到端提速。

### 4. 为什么 PostgreSQL 是权威存储？

Incident 在 M3 的 PostgreSQL 事务中创建，Cache 值则是按白名单派生的可重建副本。
删除所有本项目缓存不会删除 Incident，下一次 Miss 能回源重建。
`CACHE_ENABLED=true` 要求 `INCIDENT_PERSISTENCE_ENABLED=true`，避免装配出只有缓存、没有源库的查询入口。
权威性不等于每次读取都会验证最新值：Hit 可直接返回历史摘要，即使这时 PG 不可用。

### 5. Cache 和 Session 的 Redis 数据有什么区别？

[`SessionRepository`](app/session/repository.py) 保存用于下一轮 Agent 的对话，是业务状态；
丢失后不能简单从 Incident 恢复原有版本、完整历史与 TTL。
Cache 只保存可从 PG 派生的历史元数据，GET/SET 故障允许降级；
Session commit 仍必须 WATCH/MULTI/EXEC 成功，不能借用 Cache 的“写失败也继续成功”语义。
两者使用不同命名空间，可以共享连接池，但不共享事务与数据模型。

### 6. Redis SET EX 的作用是什么？

`CacheRepository.set()` 使用 `await client.set(key, payload, ex=ttl_seconds)`。
一条 Redis 命令同时写入完整 JSON 并设置过期时间，不会出现先 SET 成功、EXPIRE 失败而永久驻留的窗口。
普通 get 不续期。真实测试先把剩余 TTL 降到 10 秒，读取后仍不超过 10 秒，随后用服务端过期命令
使 Key 到期，再断言下一次查询重新 SELECT。参考 [Redis SET](https://redis.io/docs/latest/commands/set/)。

### 7. Cache Key 应该如何设计？

[`incident_summary_key`](app/cache/keys.py) 集中构造
`opspilot:cache:incident_summary:v1:{UUID4}`。UUID 来自服务器生成的 Incident ID，不接收任意用户问题作为 Key。
配置 prefix 只能位于 opspilot:cache 下，安全子段支持测试环境隔离；它不能变成 Session 前缀。
Key 是寻址规则，不能证明谁有权读取，也不是租户认证机制。

### 8. 为什么 Key 要有版本号？

不同摘要结构不能在同一 Key 上混读。未来增加不兼容结构时，修改 Key 前缀 v1 和 payload 中的版本，
新读者使用新 Key，旧 Key 由 TTL 回收。当前 `schema_version` 必须显式存在且为整数 1，
缺失、字符串、布尔值或其他版本均会被视为坏数据；不能让默认值掩盖缺版本的旧格式。

### 9. TTL 到期与 Stale Data 有什么区别？

到期表示副本不再存在；陈旧表示它仍存在但不再等于权威数据。两者不是同一件事。
真实测试在专用 PG 数据库更新 duration_ms 后，Redis 仍有正 TTL，读取却返回旧耗时。
当前业务不更新已完成 Incident，所以 TTL 主要控制内存和结构更新；如果将来支持修正/删除，
就必须额外管理陈旧窗口。有限 TTL 只是限制通常的保留时间，不能作为强一致性保证。

### 10. 什么是 Cache Invalidation？

主动使副本失效，让下一次读取重新回源。`IncidentQueryService.invalidate(id)` 委托 CacheRepository
执行单 Key DEL。返回 true 表示收到删除确认，原本不存在也算成功；false 表示未确认。
它不删除数据库、不删除 Session，也不使用通配删除或 FLUSHDB/FLUSHALL。
M3 没有业务 UPDATE，所以本阶段提供内部方法和测试，不添加人为的更新 API。

### 11. 为什么更新数据库后通常删除缓存，而不是直接更新缓存？

直接更新会让写路径承担摘要构建和双写顺序，两个并发写者还可能把旧摘要最后写入缓存。
删除把副本重建统一交给读取路径，逻辑通常更简单。不过删除也不是万能一致性机制，
必须继续分析并发回填、DEL 失败和访问频率。M4 的实际写路径仍只是 M3 INSERT，未加新的业务更新。

### 12. 为什么先更新数据库，再删除缓存？

如果先删缓存再提交数据库，另一个读者可能在提交前读到旧值并重新缓存，即使事务最后成功也会留下旧副本。
因此未来写者应在 PG COMMIT 确认后调用 invalidate；COMMIT 失败就不能声称更新成功。
真实测试里的受控 SQL UPDATE 在 `async with engine.begin()` 退出后才失效。
Redis DEL 不属于 PG 事务，两者无法一起 rollback。

### 13. 提交后删除是否完全避免旧数据？

不能。测试 [`test_stale_fill_race_is_explicitly_not_strong_consistency`](tests/test_cache.py) 使用事件同步：
A Miss 并持有旧快照 → B 提交新值 → B DEL → A SET 旧快照 → 后续 Hit 读旧值。
M4 用“只缓存业务不可变的 completed 记录”缩小这一风险，没有假装修复竞态。
未来若允许变化，需要版本化记录、CAS/更新序列或可靠通知等方案，并分析删除与回填的完整协议；
对必须最新的读取应绕过缓存。一次 DEL 也不能保证删除前已开始的读取不会再次回填。

### 14. 什么是缓存穿透？

大量查询不存在的 ID，Redis 总是 Miss，PG 也总返回 None。M4 默认不做负缓存，所以同一缺失 ID
查询两次产生两次 SELECT，测试明确验证此行为。未来可考虑短 TTL 空值缓存、鉴权/限流或 Bloom Filter，
但负缓存又需要处理后来创建的数据何时变得可见，不能无条件加入。

### 15. 什么是缓存击穿？

一个热门 Key 过期，许多请求一起 Miss 并回源，瞬间集中打到数据库。
M4 不加锁，两个并发 Miss 真实产生两次 SELECT。这里所有结果都是同一不可变摘要，SET EX 仍完整写入，
所以正确性不受重复回源影响，但容量可能受影响。流量足够大时才考虑 Singleflight 或短生命周期锁。

### 16. 什么是缓存雪崩？

很多 Key 同时过期，或者 Redis 故障导致大面积 fallback，回源总量突然上升。
本项目固定 TTL 且没有抖动；缓存可降级不代表数据库有无限承载能力。
生产需要容量规划、TTL 抖动、流量限制和降级监控。本次没有修改全局 Redis 策略或引入大型监控组件。

### 17. 什么是 Cache Stampede？

它描述并发请求争相重建缓存，热点失效时常与击穿一起出现。
本进程 Singleflight 可把同一 Key 的回源合并为一个任务，但要处理取消、异常与等待者生命周期；
多进程还需要讨论分布式协调。分布式锁有租约、持有者失败和锁释放等额外问题。
对本阶段简单历史摘要，明确重复查询的边界比引入 Redlock 或复杂 Lua 更合适。

### 18. 为什么缓存故障可以回退数据库？

缓存是可丢弃的优化层。GET 异常/超时返回未命中并记录错误；SET 失败仍返回已查到的权威摘要；
DEL 失败返回 false，不声称失效完成。每次缓存 IO 有独立 `asyncio.timeout`，共享 Redis Client 禁用自动重试。
只捕获预期 RedisError/超时及数据校验错误，不捕获任意 Exception；CancelledError 必须继续传播。
测试覆盖真实 TCP 连接拒绝、Redis GET/SET/DEL 异常、操作 deadline 和外部取消。

### 19. 为什么 Session 更新失败不能相同降级？

Session 更新属于接受一轮完整 User/Assistant 对话的业务承诺。未确认 commit 就返回 final，
下一轮可能读不到刚才的历史，或覆盖另一个请求已提交的版本。
M4 的 Redis 客户端复用只涉及连接资源，SessionRepository 的异常与 WATCH 语义完全保留。
[`test_cache_outage_does_not_change_session_commit_errors`](tests/test_cache.py) 同时验证：摘要能回源，Session 仍报错。

### 20. 为什么不能直接缓存实时 Agent 答案？

自然语言问题相同，并不说明服务器、时间窗口、会话、权限或指标相同。
复用旧答案会跳过当前采集并把历史证据误认为实时状态。M4 的 Key 只接受一个已完成 Incident 的 ID，
查询服务不用于选择工具或允许诊断。真实测试对同一问题分别调用 JSON 和 SSE，仍执行六次 LLM 调用，
即两个独立的三轮 Mock Agent，而不是命中一次旧答案。

### 21. 如何证明缓存减少数据库查询？

离线测试用 AsyncMock 的 get.await_count，真实测试在 Engine 上监听 before_cursor_execute，
只统计涉及 incident_records 的 SELECT。首次 Miss=1，第二次 Hit=0，主动失效后=1，TTL 到期后=1。
这证明避免了查询，不证明一定降低端到端延迟：序列化、Redis 网络往返、Miss 回填也有成本。
本次没有延迟百分比或吞吐提升的结论。测试不是仅比较两次结果内容相同。

### 22. 哪些场景缓存反而增加复杂度？

低复用率、源查询已经便宜、数据频繁变化、严格要求最新值或授权规则复杂时，缓存可能增加额外延迟、
副本一致性、失效、故障和权限成本。本项目因此默认 CACHE_ENABLED=false，仅投影稳定安全字段。
首次 Miss 当前仍由 M3 get 读取完整记录再投影；未来可根据真实查询分析决定是否增加数据库侧只选摘要字段的读取。
共享 Session/Cache 连接池也存在资源竞争，若流量需要隔离，应再引入独立配置和各自生命周期。

### 完整 V0.2 的分工

| 里程碑 | 负责的事 | 一致性要求 |
|---|---|---|
| M1 SSE | 执行事件流、取消、编码 | 工具事件即时发送，终态不能提前确认 |
| M2 Redis Session | 短期上下文 | WATCH/version 防丢更新，提交失败明确报错 |
| M3 PostgreSQL | 长期诊断记录 | 短事务、约束、回滚；双存储部分成功/未知明确报告 |
| M4 Redis Cache | 历史安全摘要副本 | 可重建、可降级；TTL/失效不等于强一致 |

`DiagnosisService` 承担执行与提交；`IncidentQueryService` 承担历史查询。两条链共享 Repository/Client
等资源，但缓存 Hit 不代替执行、不参与事务判断。所有真实数据库网络 IO 都异步；Cache/PG 调用顺序执行，
没有跨 Redis 与 PostgreSQL 的共同事务。

练习：运行本地 CLI 两次，观察 cache_miss/cache_fallback/cache_set 与 cache_hit 的日志差别。
练习：关闭 CACHE_ENABLED 再读同一 ID，解释为什么仍能读取、却每次都要 SELECT。
练习：阅读旧值回填测试，说明为什么简单加一个更长或更短的 TTL 都不能使其变成强一致。
练习：解释为什么两个并发 Miss 的结果正确，并不代表已经解决 Stampede。

## V0.3：从一次协程执行到可恢复的工作流

下表对应本次实际实现，`test_workflow_graph.py` 是快速离线验证，
`test_workflow_postgres.py` 才证明跨连接、跨进程持久化；两者不能互相替代。

| # | 问题 | 结合代码回答 |
|---|---|---|
| 1 | LangGraph 是什么？ | 本项目使用的状态图运行库；`build_graph` 定义节点/路由，编译后的 Graph 调度执行并写 Checkpoint。它不是另一个 LLM，也没有替换 BaseLLM。 |
| 2 | 与手写 Agent Loop 的区别？ | `Agent.stream` 把执行进度保存在当前协程的消息列表中；Graph 将每次规划、单个工具、审批分别作为持久化边界。旧接口保持旧循环，新 API 使用图。 |
| 3 | StateGraph、Node、Edge 是什么？ | `StateGraph(GraphState)` 指定共享状态形状；`WorkflowNodes.plan/execute_tool` 等是节点；`add_edge/add_conditional_edges` 决定下一个节点。见 [graph.py](app/workflow/graph.py)。 |
| 4 | GraphState 如何更新？ | 节点返回局部 dict，框架合并；`checked_update` 在写前检查总尺寸。消息通过新列表替换，代码不就地修改 Checkpoint 读出的共享对象。见 [state.py](app/workflow/state.py)。 |
| 5 | Reducer 有什么用？ | `tool_results: Annotated[..., merge_results]` 根据服务器 call_id 合并、替换重复结果，而不是简单相加；`test_reducer_deduplicates_and_limits_state` 验证幂等合并。 |
| 6 | START / END 的作用？ | START 指向首次 plan；END 结束 Graph。END 不等于业务已提交，Service 之后还要处理 Session 与 Incident。 |
| 7 | 条件分支如何实现？ | `after_plan` 根据是否已有答案去 finalize 或 validate；`after_validation` 看审批信息；`after_tool` 优先消费同轮剩余调用，否则回 plan。路由是 async，避免无意义的线程池跳转。 |
| 8 | 什么是 Checkpoint？ | 某次图执行边界的状态、版本、待执行节点和 pending writes；`aget_state` 返回可恢复快照。它包含控制信息，不只是问答 JSON。 |
| 9 | 与 Redis Session 的区别？ | Session 只保存有限的 user/assistant 成对历史和 TTL/version；Checkpoint 保存当前工具、审批和下一节点，生命周期独立，可在 Session 已过期后继续。 |
| 10 | thread_id 的作用？ | Saver 查找执行历史的键。本项目等于服务器生成 workflow_id，每次执行新建；同一个 session_id 能有多个不同 thread。 |
| 11 | 为什么 Checkpointer 要持久化？ | InMemorySaver 随进程退出丢失；`test_real_process_exit_then_approval_in_new_process` 用两个 Python 进程证明 PostgreSQL 审批中断可继续。 |
| 12 | AsyncPostgresSaver 如何工作？ | 通过独立 psycopg pool 保存 checkpoint blobs/writes 等表；开启 autocommit/dict_row，显式 `setup()` 执行官方内部迁移。见 [checkpoint.py](app/workflow/checkpoint.py)。 |
| 13 | 在什么边界保存？ | 本项目 `durability='sync'` 等待图步骤持久化后进入下一步；plan、validate、审批和每个 tool 分别有边界。当前正在执行而未确认的节点仍可能重跑。 |
| 14 | Resume 与 Retry 的区别？ | RetryPolicy 在本次执行中重试瞬时错误；Resume 重新连接已有 thread，用 `None` 继续保存的待执行节点。它不会创建新 workflow 或重置初始输入。 |
| 15 | Failed Task Resume 是什么？ | 第二个工具失败时，第一个已确认结果留在 Saver；修复后 resume 从 execute_tool 继续。`test_m3_failed_node_resume_no_reexecution` 把第一个工具替换为“若调用则失败”的断言来证明没有重跑。 |
| 16 | 为什么某些节点重跑？ | 故障发生在节点内时没有完成 checkpoint；interrupt 恢复也从节点开头再进入。图不是 Python 指令指针的持久化，不能依赖局部变量从任意行继续。 |
| 17 | 为什么不天然 exactly-once？ | 工具效果与 checkpoint 保存是两个提交；效果已产生、checkpoint 未写入时进程可能崩溃。图只知道这个节点尚未确认，因此会重试执行。 |
| 18 | 什么是幂等性？ | 相同操作重发得到相同已确认效果。本项目 mock action 的唯一 operation_id 使重复 INSERT 返回原结果；不是靠内存计数器。 |
| 19 | 幂等键如何设计？ | SHA256(workflow_id、服务器 call_id、工具名称、规范化参数)。同一次调用恢复键相同，不同 workflow/调用/参数键不同。键在 validate 节点中确定，并随 Checkpoint 保存。 |
| 20 | 怎样防止重复副作用？ | 模拟效果本身就是数据库记录，`record_action` 用唯一约束和短事务完成效果与登记。未知结果只允许显式恢复核查；不自动重试写工具。外部系统需自己的幂等/查询机制。 |
| 21 | interrupt 如何暂停？ | `human_approval` 调用 `interrupt(pending_approval)`，框架持久化中断并返回；Service 发 approval_required，HTTP 请求结束，不占着 Python Event 等人。 |
| 22 | Command 如何恢复？ | API 校验 ID/decision，拿跨进程锁确认中断后，向原 thread 传 `Command(resume=...)`。节点重新进入时 interrupt 返回这个决定。 |
| 23 | 为什么 interrupt 前不能副作用？ | 恢复会重复执行该节点的前半段，所以审批节点只读状态、调用 interrupt、校验决定。动作写入在独立 execute_tool 中；批准前动作表为空。 |
| 24 | HITL 与弹窗区别？ | 弹窗只是 UI；这里有服务端持久化审批内容、固定参数、工作流状态机、锁和恢复路径。浏览器或进程重启后批准仍作用于原操作。共享 token 仍不等于审批者身份授权。 |
| 25 | SSE 断开为何不删除 Workflow？ | 网络断开不能撤销已提交的数据库操作；只取消当前 IO 并保留 checkpoint。`test_sse_disconnect_releases_io_pool_lock_and_keeps_checkpoint` 验证 plan/tool/commit 三种窗口。 |
| 26 | Session 过期怎样处理？ | `commit_session` 读取原 ID/version，过期返回 expired，冲突返回 conflict；都不复活或覆盖旧 Session，独立 Incident 仍保存。业务输出显式返回 session_status。 |
| 27 | 为何不能合并四类存储？ | Checkpoint 是执行游标；Workflow 表管状态/互斥关联；Incident 是业务结果；Session 是跨请求上下文；Cache 是可重建查询投影。它们的 TTL、权限、事务和恢复语义不同。 |
| 28 | 什么时候 Graph 更合适？ | 任务跨请求、人工停顿、节点失败恢复或需重启后继续时；短小同步诊断的旧 loop 更容易理解，所以本版同时保留。 |
| 29 | 增加哪些维护成本？ | LangGraph/checkpoint 版本兼容、psycopg 第二套 pool、官方内部迁移、状态 schema/隐私、checkpoint 保留与断线恢复测试；不只增加一个依赖包。 |
| 30 | 仍有哪些生产限制？ | 无用户身份/RBAC/租户隔离、无后台调度、无生产写工具、无外部 fencing/通用 exactly-once、无跨 Redis/PG 原子事务、无完整 DLP；pending/failed 长期保留需额外运维策略。 |

对应源码：[nodes.py](app/workflow/nodes.py)、[service.py](app/workflow/service.py)、
[repository.py](app/workflow/repository.py)、[session_commit.py](app/workflow/session_commit.py)、
[privacy.py](app/workflow/privacy.py)。

### 四种恢复必须分别证明

| 场景 | 入口 | 不变量 | 证据 |
|---|---|---|---|
| Node Retry | RetryPolicy 3 次有限尝试 | 仅只读工具/规划瞬时错误；取消、校验、拒绝不重试 | `test_transient_retry_is_bounded`、`test_cancellation_not_retried` |
| Checkpoint Resume | 原 thread + `None` | 第一个已完成工具不执行，失败工具继续 | `test_m3_failed_node_resume_no_reexecution` |
| Human Interrupt Resume | 原 thread + `Command(resume=...)` | 拒绝零动作、批准单动作、重复审批 409 | `test_m4_approval_restart_and_duplicate_requests`、`test_two_approvals_only_one_owner` |
| Process Restart Recovery | 新 Python 进程/新连接 | 不依赖旧协程、内存锁或计数器 | `test_real_process_exit_then_approval_in_new_process`、`test_hard_process_crash_after_action_before_checkpoint` |

### 理解三个提交窗口

1. **动作已成功、Checkpoint 未成功**：崩溃测试在 `record_action` 提交后、execute_tool 返回前结束子进程。
   新进程看到待执行 execute_tool，使用原 operation_id 查询/返回已存在记录，不产生第二条动作。
2. **Redis EXEC 成功、客户端未收到响应**：回执和 Session 写在同一事务中。恢复先读回执，不再次追加。
   回执独立于 Session TTL；真实 Redis 测试 `scenario=unknown` 验证 version 仍为 1。
3. **Graph 已结束、Incident 提交失败/响应丢失**：前者只补业务提交；后者依据稳定 ID 与同事务 completed
   状态核查，不重复写 Incident。两种情况都不重跑 Graph；分别有独立测试。

这些机制是在各自系统中约束重复执行，**不是 Redis + PostgreSQL + 外部服务的分布式事务**。
如果 Redis 灾难恢复丢失回执、工作流库与 Checkpoint 库恢复到不同时刻，当前实现没有自动一致性修复协议。
真实高风险变更接入前，需要重新设计身份授权、目标系统幂等、执行所有权 fencing、超时结果核查和补偿。

### 自己动手验证

- 阅读 `test_multi_call_plan_resumes_each_call_at_its_own_boundary`，确认一个 LLM 响应里的多个工具仍逐节点执行。
- 将只读工具 safety 改为未知值，观察 validate_tool 拒绝；不要通过 LLM 返回的“已批准”文字授权。
- 比较 `status()` 返回的摘要和 `snapshot()` 的内部状态：HTTP 不应提供后者。
- 对 completed 执行 prune，然后确认 Incident 仍存在；对 pending_approval 执行 prune，确认审批没有被删。
- 阅读旧 `SessionRepository.commit` 与新的 `commit_session`：前者语义保留，后者增加独立回执以处理长工作流重试。
- 检查 `JsonPlusSerializer` 的严格设置：禁用 pickle，禁止任意应用类型恢复；框架安全内置类型仍可用于 interrupt。
