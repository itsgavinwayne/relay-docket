# Claude 中转模型核验开发计划

> 历史开发计划。当前安装、运行和验收状态以仓库 README 与运行手册为准。

> 供开发人员按任务执行。每个任务以勾选框跟踪，先建立可复现的失败用例，再实现、验证并提交。任务 1–6 构成首版，任务 7–8 扩大覆盖，任务 9 是独立可选交付。

**目标**　对真实对话随机抽样，用独立可读的 AWS Bedrock 调用日志核对模型、请求和响应，并给出可重查的结论。

**架构**　本地代理负责透传和抽样，后台 CLI 负责 AWS 对账，SQLite 保存待查摘要和结果。独立主动探测可验证接入是否正常。Skill 调用同一套 CLI 并呈现报告。

**技术栈**　Python 3.12、FastAPI / Uvicorn、httpx、boto3、SQLite、pytest。开发目录采用 `src/relay_audit/`，用 `pyproject.toml` 提供 `relay-audit` 命令。

---

## 1. 范围和前置条件

- 首版客户端为 Anthropic Messages 兼容客户端，验收包含 Claude Code。审计入口为 `POST /v1/messages`，支持普通 JSON 和 SSE。客户端需要的 `/v1/messages/count_tokens` 和 `/v1/models` 在上游支持时透传，调用不计入推理抽样。
- 首版读取一个专用 AWS 测试账户、调用区域和 CloudWatch 日志组。AWS 调用角色与审计身份分开。中转要能将测试流量接到该账户，开发前需确认账户、区域、日志组和调用角色。前置条件缺失时，可用本地模拟环境实现和验证软件，真实归因验收仍需接通 AWS。
- 上游首批适配 Bedrock Runtime 的 Claude 原生 `InvokeModel`、`Converse`。`InvokeModelWithResponseStream`、`ConverseStream` 的日志正文以真实样本验收，能完整还原才开放相应验证能力。AWS 日志只覆盖文档列出的调用端点和操作。先确认按所选区域和账户能实际调用的模型档位。 [AWS 调用日志](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) · [端点可用性](https://docs.aws.amazon.com/bedrock/latest/userguide/models-endpoint-availability.html)
- 模型清单分为三层。**读取层**展示任何合格 AWS 记录里的原始 `modelId`。**首版核验层**覆盖 Claude Opus、Sonnet、Haiku 的精确配置版本。**扩展核验层**优先支持 DeepSeek、GLM、Qwen、Kimi、MiniMax，再按需求接入其他模型。共用 Converse 结构的模型可复用适配器，各模型仍须用真实样本验证字段映射。账户外调用统一按证据不足处理。
- 支持两种模式。默认被动抽查真实请求，在响应完成后随机抽样。主动探测独立发请求、生成 128 位随机标签，不写入用户对话历史，用于校准链路或定时巡检。
- 首版的核验声明覆盖已列明的请求字段和响应内容。它依赖 AWS 可见记录及关联规则。抽样不可证明其他请求，旁路上游或同一上下文的影子调用也可能造成歧义。若要逐笔证明并抵御此类归因歧义，另立支持签名证明的网关项目。

## 2. 数据流和判定合同

1. 客户端调用本机代理，代理发往配置中的固定中转地址。流式响应逐块转发，审计在旁路读取，AWS 查询不进入响应关键路径。
2. 对每个完成的约定路由请求，在本地抽样一次。用单独的 `audit_id` 标识这次客户端请求，记录配置版本、请求时间窗、受检字段摘要、流完整性及可选上游请求 ID 提示。摘要使用本机密钥的 HMAC-SHA256。未抽中的内容随请求释放。被抽中但无法解析的请求也计入抽样分母。
3. Worker 用自己的 AWS 身份查询日志。检查 `schemaType`、`schemaVersion`、`accountId`、`region`、`identity.arn`、`operation` 和时间窗。去重键为账户、区域和 AWS `requestId`。同键内容冲突时视为证据不完整。候选查询不要按预期模型过滤，否则会漏掉模型替换。
4. 主动探测用 AWS 输入正文中用户消息指定位置的随机标签关联。中转愿意配合时可同步传入 `requestMetadata.audit_id` 以加快查找，关联仍要看实际正文。被动模式下，中转返回的 AWS 请求 ID 只用于定位候选，须和 AWS 记录核验。没有强关联线索时，允许用**唯一的完整请求摘要匹配**形成候选，并要求完整响应也匹配才给通过。相同请求命中多个调用、查询分页未走完、候选正文缺失足以影响消歧，均不能通过。 [Bedrock 请求元数据](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-request-metadata.html) · [CloudWatch 分页](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html)
5. 正常候选出现后继续等待该任务的证据收集截止时间，再以完整查询结果判定，避免先到的一条日志掩盖后到的重试。首版默认时间窗为请求发出前 5 秒到结束后 5 秒，日志等待 180 秒，按实测交付延迟和时钟偏差调整。等待期间显示 `PENDING`。抓取限制、超时或限流耗尽时以未验证收口。
6. 比对覆盖模型、请求及响应三个维度。显式映射调用参数、系统提示、角色和有序内容块。文本保留空白与换行，JSON 对象键排序，数组顺序保留，缺失字段与显式值仅在有文档和测试时等价。Claude Code 通过验收前须覆盖工具定义、工具调用及返回值。把不支持或影响语义的未知字段判为不可核验，禁用模糊相似度。保留原始响应 `model` 作为诊断字段。
7. 模型别名由本地配置映射到允许的精确 ID。日志给出 inference profile 时，按已验证且带时间的 profile 成员映射；成员无法唯一收敛到同一基础型号时，展示 profile ID 并将型号维度标为未验证。若实现可选自动刷新，使用单独的 `bedrock:GetInferenceProfile` 权限并保存快照。 [AWS 模型和 inference profile](https://docs.aws.amazon.com/bedrock/latest/userguide/foundation-models-reference.html)

| 终态 | 触发条件 |
|---|---|
| `VERIFIED` | 候选唯一，限定范围内的输入和完整响应相符，且可解析型号符合配置。 |
| `MISMATCH` | 已可靠关联且受检证据完整，模型或正文不同。独立随机标签定位且无歧义时，可报告仅保留标签但改写输入。仅以输入摘要或中转提供的请求 ID 定位、响应对不上时，先标未验证并列出候选差异。若输入输出都匹配但确切型号不同，则报告型号差异。 |
| `UNVERIFIED` | 记录不可见、关联歧义、正文缺失、超时、权限不足、流未闭合、未支持格式或型号映射不明确。附具体原因及可安全披露的候选字段。 |

报告字段包含 `schema_version`、`audit_id`、`mode`、`status`、`reason`、`association_method`、`expected_model`、`aws_model_id`、`resolved_model_id`、`aws_request_id`、`request_match`、`response_match`、`compared_fields`、`started_at`、`finished_at`。缺失值用 JSON `null`。单次检查退出码为 0 已验证、2 差异、3 未验证、1 执行或配置错误。

## 3. 实施任务

### 任务 1　工程骨架和验证配置

文件为 `pyproject.toml`、`src/relay_audit/config.py`、`src/relay_audit/types.py`、`src/relay_audit/cli.py`、`config.example.toml`、`tests/test_config.py`。

- [ ] 定义可版本化的 Profile、Capture、Invocation、Finding 数据结构，以及上述结果枚举和原因码。
- [ ] 加载 TOML 配置。包含固定中转基址、认证环境变量名、AWS profile、账户、区域、日志组、允许角色、模型映射、采样率、查询时限和本地保留期。
- [ ] CLI 提供 `doctor`、`serve`、`check`、`report`。`doctor` 输出凭证、指定日志组读取及基础配置状态，显示当前 profile 与预期模型；真实调用由 `check` 发起。
- [ ] 测试非法采样率、未设置凭证、非本机监听、重复别名及退出码。运行 `python -m pytest tests/test_config.py -q`，预期通过。

### 任务 2　CloudWatch 和主动探测

文件为 `src/relay_audit/aws_logs.py`、`src/relay_audit/probe.py`、`tests/test_aws_logs.py`、`tests/test_probe.py`、`tests/fixtures/bedrock/`。

- [ ] 建立 AWS 沙箱日志组，限制审计凭证只能查询指定组，书面记录测试流量进哪个账户、区域和调用角色。先手工发送一次短文本，确认日志确有账号、模型、请求正文和响应正文。
- [ ] 用 boto3 `FilterLogEvents` 按专用日志组和时间窗读取，处理空页仍带 token、重复 token、退避、限流、拒绝访问、超大正文引用。首版遇到 S3 正文引用返回 `BODY_NOT_AVAILABLE`，跨存储读取放入任务 8。
- [ ] `check --profile sandbox --model claude-sonnet --json` 发一次短合成请求并附随机标签，保存审计 ID。关闭 HTTP 自动重试，避免一次 check 无提示地重复付费。
- [ ] 测试用 botocore Stubber 模拟空页后找到、请求 ID 重复、限流重试和无读权限，运行 `python -m pytest tests/test_aws_logs.py tests/test_probe.py -q`。

### 任务 3　语义映射、关联和对账

文件为 `src/relay_audit/normalize.py`、`src/relay_audit/reconcile.py`、`src/relay_audit/model_map.py`、`tests/test_normalize.py`、`tests/test_reconcile.py`。

- [ ] 为 Anthropic Messages、Bedrock Claude 原生和 Converse 定义同一个版本化比较结构。输入涵盖系统提示、历史消息、推理参数和 stop 序列；输出保留有序文本、停止语义及支持的工具块。每种转换对应一份脱敏原始样本和一份预期结构。
- [ ] 比对仅允许明确列出的等价转换。例如纯文本字符串和相同单一 text block 可在规则声明后等价。不要把缺失内容当成空正文，也不要把两个连续文本块任意合并。
- [ ] 实现候选关联、型号映射和三种终态。加入日志模型与请求模型不同但输入输出相同、AWS 有记录但响应被换掉、重复请求不可消歧、只查到未知 profile 的用例。
- [ ] 运行 `python -m pytest tests/test_normalize.py tests/test_reconcile.py -q`。验收中转自报模型字段的变化不会改变 AWS 模型判定。

### 任务 4　本地代理和 SSE

文件为 `src/relay_audit/proxy.py`、`src/relay_audit/sse.py`、`tests/test_proxy.py`、`tests/test_sse.py`。

- [ ] 本机绑定 `127.0.0.1`，使用固定上游和本地访问令牌。上游凭证由代理单独注入，禁用重定向，按规则处理 HTTP 逐跳头及长度头。上游错误状态与内容照常返回。禁用上游环境代理，限制请求体、单个 SSE 事件、审计解析累计大小和并发。
- [ ] 向客户端流式输出原始 SSE 响应内容，旁路 parser 按事件边界处理部分 UTF-8、分割的行、ping、按索引组装 text 和 tool input delta。要求成功结束并完整消费，断流、客户端取消、流内错误、未知语义块和 fallback 进入对应未验证原因。代理不执行业务工具。 [Anthropic SSE 事件](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [ ] 工具输入在块结束后解析 JSON，按稳定的调用位置关联工具 ID，仅在上下游 ID 确实属于传输层标识且关联完整时允许映射。工具执行结果仍作为受检输入内容。用 Claude Code 捕获的脱敏请求补齐 thinking、工具调用、工具结果、失败或截断样例。支持范围外仍可透传。
- [ ] 运行 `python -m pytest tests/test_proxy.py tests/test_sse.py -q`。使用真实 loopback 集成用例验证首块先于结束到达、慢读端不会导致无界内存，以及断流不会得到通过结论。

### 任务 5　事后随机抽样、队列和报表

文件为 `src/relay_audit/sampling.py`、`src/relay_audit/store.py`、`src/relay_audit/worker.py`、`src/relay_audit/report.py`、`tests/test_sampling.py`、`tests/test_worker.py`、`tests/test_report.py`。

- [ ] 配置默认 5%，支持 0% 和 100%。完成后以 `secrets.randbelow(10000) < rate_basis_points` 决定抽样，测试注入随机源。采样标记只存在本机。记录请求总数、抽样数、已验证数、差异数和未验证数。
- [ ] SQLite 以审计 ID 唯一入队，事务领取任务，使用租约恢复进程意外退出的任务。重试 AWS 查询时保持同一审计 ID，避免重复写终态。代理后台入队独立于实时响应。队列满时记未验证及丢弃计数，前台仍正常转发。
- [ ] 仅落盘摘要和受限元数据，默认本地保留 7 天，HMAC 密钥单独存放，文件仅用户可读，固定密钥版本。报告按 profile、时间和状态过滤，可输出 JSON 及终端表格。汇总同时显示抽样率和证据缺失率。
- [ ] 运行 `python -m pytest tests/test_sampling.py tests/test_worker.py tests/test_report.py -q`。验证采样发生在响应结束后，应用重启后未完成任务可继续，过期报告可清理。

### 任务 6　端到端验收和交付

文件为 `tests/integration/test_audit_flow.py`、`tests/live/test_bedrock.py`、`README.md`、`docs/operations.md`。

- [ ] 模拟中转提供正常透传、改写输出、追加系统提示、模型替换、重复调用、正文丢失、AWS 延迟和 SSE 中断的独立场景。应覆盖主动和被动模式中关联强度不同导致的判定差异。默认测试只用本机模拟。
- [ ] 显式使用沙箱 AWS 和上游账户做真实付费验收，先用 100% 抽样核对已开通的 Claude 档位，再用 5% 验证常规运行。通过 Claude Code 配置本地代理，走完普通对话和工具往返，核对日志中的操作类型、正文和 profile 映射。
- [ ] 记录 AWS 日志到达耗时、查询量、抽样覆盖、无法验证原因，以及代理相对直连的首块延迟。建议目标为同机负载下 SSE 首块 p95 额外延迟低于 30 ms，以固定并发与负载记录实测；未达到则查明延迟来源。日志读取中断期间也必须持续转发。
- [ ] 运行 `python -m pytest -q`、`python -m build`，再在干净虚拟环境安装 wheel，验证四个 CLI 子命令的帮助和启动。真实 AWS 测试单独运行 `RELAY_AUDIT_LIVE=1 python -m pytest tests/live -q`，仅在沙箱权限、真实中转和费用已获授权时执行。
- [ ] 交付 wheel、配置样例、安装和回滚说明。回滚通过把客户端基址恢复到中转原地址完成。运行文档区分本地摘要和 AWS 正文的保留期，明确 AWS 区域级正文日志可能覆盖同区其他受支持调用。

### 任务 7　扩大模型验证覆盖

文件为 `src/relay_audit/providers/`、`tests/fixtures/providers/`、`tests/test_provider_adapters.py`、`docs/verified-models.md`。

- [ ] 按 DeepSeek、GLM、Qwen、Kimi、MiniMax 排序接入，先读取日志的原始模型 ID，再逐个用实际 API、区域和样本验证映射。优先复用 Converse 适配器，只有格式确有差异才新增适配模块。
- [ ] 每个纳入完整核验的模型至少存一组脱敏正常样本及一组输出差异样本，登记确切型号或 profile、AWS 操作、是否支持流式及实际验收日期。行为猜测不计入已核验覆盖。
- [ ] 运行 `python -m pytest tests/test_provider_adapters.py -q`。未做真实样本验收的模型在支持表标为“仅读取 ID”。

### 任务 8　大正文和规模化运行

文件为 `src/relay_audit/aws_bodies.py`、`src/relay_audit/aws_logs.py`、`tests/test_aws_bodies.py`、`docs/operations.md`。

- [ ] 配置 AWS 大正文专用 S3 目的地。按真实日志样本确认引用字段后才读取，限制可读 bucket、prefix、对象大小和解压后大小。对象暂不可读时按证据缺失判定。扩大流量时共享相近时间窗的扫描结果，并为查询并发和次数设上限。 [AWS 正文大小和日志目的地说明](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html)
- [ ] 运行 `python -m pytest tests/test_aws_bodies.py tests/test_aws_logs.py -q`。验证跨桶引用、S3 无权限、大小超限及迟到对象。经沙箱复验后再提高采样率或扩大生产流量。

### 任务 9　可选 Codex Skill

文件为 `skills/relay-audit/SKILL.md`、`skills/relay-audit/references/commands.md`。

- [ ] 定义“查中转用了什么模型”“跑一次探测”“看抽查报告”的触发语句，分别映射到 CLI 的 `report` 或 `check`。选择 profile 后读取 JSON，说明判定和 AWS 证据范围。
- [ ] 验收对三种终态及 CLI 缺失的呈现。Skill 不从聊天正文、响应口吻或中转自报字段推断验证结果，用户无需把 API Key 贴进对话。

## 4. 交付顺序和工期

| 阶段 | 任务 | 交付条件 | 单人估算 |
|---|---|---|---|
| 技术验证 | 1–3 | 一次主动探测可端到端关联，能发现确切型号差异 | 2–3 个工作日 |
| 可试用 | 4–5 | Claude Code 实际对话可透传，已完成请求可随机抽样，后台有报告 | 3–4 个工作日 |
| 首版验收 | 6 | 本机模拟、至少一条真实 AWS 路径、安装和回滚说明齐全 | 2–3 个工作日 |
| 扩大覆盖 | 7–8 | 增加验证模型、补齐大正文，按实际负载压测 | 3–5 个工作日起 |
| 对话入口 | 9 | 可通过 Codex 查询或主动探测 | 0.5–1 个工作日 |

**预计首版 7–10 个工作日**，假设开工时已具备可读的沙箱日志、配合接入的中转与可调用模型。首版验收以任务 1–6 为准，任务 7–8 供扩大覆盖。真实 AWS 日志样本是首个技术关口，尤其需要验证流式日志的正文完整度和生产对话的大小分布。
