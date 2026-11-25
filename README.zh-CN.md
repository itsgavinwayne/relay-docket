# Relay Docket · 中转案卷

[English](README.md) · [运行手册](docs/operations.md) · [模型覆盖](docs/verified-models.md)

Relay Docket 用你能直接读取的 AWS Bedrock 调用日志核验 AI 中转。它查找对应的上游记录，比对实际模型 ID、请求和响应正文。可以手动查一次，也可以接入 Claude Code，随机抽查真实对话。

| 使用方式 | 命令 | 适合什么场景 |
| --- | --- | --- |
| 单次探测 | `relay-audit check` | 发一条带随机标签的独立测试请求 |
| 日常抽查 | `relay-audit serve` | 本地代理正常转发对话，响应结束后默认抽查 5% |
| 查看报告 | `relay-audit report` | 查看实际模型、AWS 请求 ID 和差异或缺失原因 |

## 用 Agent 安装

把下面这段提示词复制给有终端权限的本地编程 Agent。

```text
请在这台电脑上安装 Relay Docket，仓库是 https://github.com/itsgavinwayne/relay-docket。阅读仓库 README 并直接执行安装。使用独立的 Python 3.12+ 虚拟环境。如果已有项目副本，优先复用。仅在 relay-audit.toml 不存在时从 config.example.toml 复制一份。安装后验证 relay-audit --help 能运行。

如果我已配置中转地址、模型别名以及可读取的 AWS Bedrock 日志，使用现有配置，并在本机凭证就绪时运行 relay-audit doctor。密钥放在本地环境变量或凭证管理器里。只询问缺失的非敏感配置，并告诉我如何在本机设置凭证，无需把密钥粘贴到聊天里。

只有我明确要求时才发送付费测试请求或启动代理。完成后告诉我安装位置、已验证的内容和下一条可执行命令。如果缺少 AWS 或中转信息，先完成本地安装，把真实核验标记为待配置。
```

## 快速开始

**准备条件**　Python 3.12+、一个能把受检流量路由到 Bedrock Runtime 的 Anthropic 兼容中转，以及独立的 AWS 调用日志读取权限。在调用所在区域启用 Bedrock Model Invocation Logging，将文本输入输出送到专用 CloudWatch 日志组，并给审计身份授予该日志组的 `logs:FilterLogEvents` 权限。 [AWS 配置文档](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html)

```bash
git clone https://github.com/itsgavinwayne/relay-docket.git
cd relay-docket
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
cp config.example.toml relay-audit.toml
```

也可在[发布页](https://github.com/itsgavinwayne/relay-docket/releases/latest)下载 wheel，将上面的 `python -m pip install .` 换成 `python -m pip install /path/to/relay_audit-0.1.0-py3-none-any.whl`。

编辑 `relay-audit.toml`，填入中转基础地址（省略 `/v1`）、认证方式、AWS profile、账户、区域、日志组、调用角色 ARN 和预期模型 ID。`model_map` 左边填中转接受的模型别名。若 AWS 日志只记录 inference profile，在 `profile_models` 中填写已确认的基础模型映射。

在本地环境或密钥管理器设置 `RELAY_AUDIT_RELAY_KEY`，然后运行：

```bash
relay-audit doctor --config relay-audit.toml --json
relay-audit check --config relay-audit.toml --model claude-sonnet --json
```

把 `claude-sonnet` 换成你配置的别名。`doctor` 检查日志访问。`check` 会产生一笔真实推理请求，默认等待约三分钟让 AWS 投递日志。只想查一次时，到这里就够了。

## 接入 Claude Code

在运行代理的终端生成本地访问令牌并启动代理。首次验收可将 `sample_basis_points` 改为 `10000`，使每笔都抽中。验收后恢复 `500`，即 5%。

```bash
export RELAY_AUDIT_LOCAL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
relay-audit serve --config relay-audit.toml
```

在启动 Claude Code 的终端配置同一个本地令牌，模型参数使用中转接受且已写入配置的别名。

```bash
env -u ANTHROPIC_API_KEY \
  -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX -u CLAUDE_CODE_USE_FOUNDRY \
  ANTHROPIC_BASE_URL=http://127.0.0.1:8765 \
  ANTHROPIC_AUTH_TOKEN="$RELAY_AUDIT_LOCAL_TOKEN" \
  claude --model claude-sonnet
```

```bash
relay-audit report --config relay-audit.toml --hours 24 --json
relay-audit report --config relay-audit.toml --status MISMATCH
```

## 如何看结果

- `VERIFIED` 表示唯一的 AWS 调用记录与预期型号及受检输入输出一致。
- `MISMATCH` 表示能够可靠关联，但发现模型或正文有差异。
- `UNVERIFIED` 表示证据缺失、不完整、存在歧义或超出核验范围。只有第三方中转 URL 和 Key 时，实际模型仍无法确认。
- `PENDING` 表示正在等待日志或后台处理。

结果只覆盖抽中的那笔调用。当前支持 Claude 原生格式以及可对比的 Bedrock Converse 记录。DeepSeek、GLM、Qwen、Kimi、MiniMax 的对账适配已用合成样本测试。项目还包含 S3 大正文读取和可选的 [Codex Skill](skills/relay-audit/SKILL.md)。各模型在真实账户的覆盖以部署验收为准。 [本地验收记录](docs/release-validation.md)

## 开发

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

默认测试使用本机模拟服务。真实 AWS 验收和 S3 配置见[运行手册](docs/operations.md)。
