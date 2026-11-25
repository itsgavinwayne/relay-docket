# 命令用法

- 看最新报告，运行 `relay-audit report --config relay-audit.toml --profile default --json`。
- 查一个审计 ID，在上述命令后添加 `--id` 和指定 ID。
- 只看差异，在报告命令后添加 `--status MISMATCH`。
- 检查日志访问，运行 `relay-audit doctor --config relay-audit.toml --profile default --json`。
- 用户明确要求发起真实探测时，用 `relay-audit check --config relay-audit.toml --profile default --model claude-sonnet --json`。用所选 Profile 中的别名替换示例值。该命令发送合成提示词并等待日志。退出码 2、3 都可能伴随有效的审计 JSON。

结果包含 `expected_model`、`aws_model_id`、`resolved_model_id`、`association_method`、`status`、`reason`、`request_match`、`response_match` 与 `aws_request_id`。对账结论仅覆盖该笔受检记录。
