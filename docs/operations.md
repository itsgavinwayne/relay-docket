# 运行手册

## 接通 AWS

1. 在中转发起调用的 AWS 账户和区域配置 Bedrock Model Invocation Logging，并指定同账户同区域的专用 CloudWatch 日志组。启用所用操作的 JSON 输入和输出记录。使用一个只承载合成流量的沙箱开始。启用区域级正文日志可能记录同区域的其他受支持调用。 [AWS Bedrock 日志](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html)
2. 给中转单独的调用 IAM 角色或用户。它只需要推理权限。给审计 CLI 单独的读身份，在日志组 ARN 范围内授权 `logs:FilterLogEvents`。如果使用 S3 大正文，单独授予配置 bucket/prefix 下的 `s3:GetObject`。使用 SSE-KMS 时另按现有加密配置授予读身份必要的解密权限。Bedrock 日志写入身份使用 AWS 指定的配置。
3. 在 CLI 配置绑定日志组、账户、调用区域和准许身份。若中转明确提供 AWS 请求 ID 响应头，可配置该头作为检索线索。若 Bedrock 日志记录的是 inference profile，在确认 profile 的成员后用 `profile_models` 显式配置。成员必须收敛到同一基础型号。 [AWS 模型配置](https://docs.aws.amazon.com/bedrock/latest/userguide/foundation-models-reference.html)
4. S3 正文选项只接受配置允许的 bucket 和路径前缀下的 `.json` 或 `.json.gz`。在部署配置中明确指定完整前缀。限制正文的输入大小和解压大小，AWS 账号同时用于校验 S3 所有者。

## 安全运行与验收

本地状态目录必须为 0700。队列只保存按本机密钥计算的正文摘要和关联元数据，终态落盘后释放队列正文摘要。数据库、key、配置和报告均应存放在本机个人目录。状态文件由运行用户访问。AWS 日志正文保留期由 AWS 独立配置。超过审计范围的协议仍可由代理转发，报告给出未验证原因。请求体超过本地转发上限时返回 413，可据沙箱测量调大上限。被动模式的样本在完成后选取。主动探测使用合成标签，可单独用调度器周期运行。

先运行默认本地测试，再在有授权的 AWS 沙箱中启用真机测试。

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
RELAY_AUDIT_LIVE=1 \
  RELAY_AUDIT_LIVE_CONFIG=relay-audit.toml \
  RELAY_AUDIT_LIVE_MODEL=claude-sonnet \
  python -m pytest tests/live -q
```

在沙箱还应逐个按模型和 API 验收，包括流式原文、工具往返和大正文。用同一个客户端分别直连中转和经代理对比首块 p95，并记录调用时钟、CloudWatch 到达时间、查询次数、未验证原因和采样比例。若完整正文晚于默认时间，在确认实际到达延迟后增加 `log_wait`。若角色清单、上游路由或模型映射在队列任务进行时变化，旧任务会显示配置变化。覆盖率按实际测试通过的 AWS 记录格式登记。

| 结果 | 含义 |
|---|---|
| `PENDING` | 正等待日志到达或由后台处理。 |
| `VERIFIED` | 在可访问的证据范围内，唯一记录的型号及受检输入输出一致。 |
| `MISMATCH` | 已关联的型号或内容有差异。 |
| `UNVERIFIED` | 记录不可见、不完整、无法消歧或格式未支持。按 `reason` 解决证据问题。 |

报告里的中转型号只供排查，可独立读取的 AWS 记录才是模型证据。被动模式只有一个弱关联候选、而响应有差异时，会交付未验证结果和候选记录。重复日志传输可去重，多笔独立调用的歧义会保留。账户外调用、黑盒对话以及未抽中的请求都超出核验范围。
