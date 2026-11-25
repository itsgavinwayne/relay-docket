---
name: relay-audit
description: 核验 Anthropic 兼容中转的实际模型，查看随机抽查结果，或通过已配置的 Relay Audit CLI 发起一次 Bedrock 合成探测。使用条件包括可信 AWS 日志与已安装的 relay-audit 命令。
---

# Relay Audit

使用已安装的 `relay-audit` CLI 执行核验。读取 [命令用法](references/commands.md) 获取参数和结果说明。默认查询配置和本地报告。用户要求运行一次新探测时，才调用 `check`，因为该命令产生真实推理请求。

使用配置中的认证环境变量和本机 AWS profile。处理请求时不要把凭证写到参数、聊天或报告中。若 CLI、配置或 AWS 读权限缺失，按实际报错提供缺失项。

报告预期模型、AWS 记录的模型、状态原因、关联方式及 AWS 请求 ID。仅在状态为 `VERIFIED` 或有完整匹配正文的 `MODEL_MISMATCH` 时，将对应 AWS 型号作为该次受检调用的结论。`UNVERIFIED` 中的 AWS 型号只能作为候选证据。没有 AWS 证据时保留未验证结论。
