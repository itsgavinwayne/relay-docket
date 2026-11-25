# Operations guide

[简体中文](operations.md) · [Project README](../README.md)

## Connect the relay and AWS

1. Route the test key or alias to the Bedrock account and region whose logs you can read independently. Use `bedrock-runtime`.
2. Enable Bedrock Model Invocation Logging for text input and output in that region and select a dedicated CloudWatch Logs group. The logging configuration can capture other supported calls in the same region, so start with an isolated sandbox. [Bedrock logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html)
3. Configure a separate audit identity with `logs:FilterLogEvents` on that group. If the local AWS profile uses SSO, sign in before starting the audit. Give the relay invocation identity the required inference permissions. Put its IAM ARN in `allowed_roles`.
4. If the relay returns the Bedrock request ID, set `upstream_request_id_header` as a lookup hint. For inference profiles, add the verified profile-to-base-model mapping locally.
5. For externalized bodies, configure CloudWatch large-data delivery to S3 and fill in `s3_bucket` and `s3_prefix`. The audit identity needs `s3:GetObject` on the configured object prefix and suitable KMS read permission when required. The reader checks bucket ownership and limits compressed and decoded body size.

## Acceptance test

Run the default local suite first. With your authorized sandbox relay and AWS log profile configured:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
RELAY_AUDIT_LIVE=1 \
  RELAY_AUDIT_LIVE_CONFIG=relay-audit.toml \
  RELAY_AUDIT_LIVE_MODEL=claude-sonnet \
  python -m pytest tests/live -q
```

Repeat for each model and operation you plan to use. In Claude Code, exercise a normal conversation and a tool round trip. Check that the source log contains comparable input and complete output. Increase `log_wait` if measured delivery latency requires a longer window. Restart the proxy after changing config.

## Local state and recovery

The proxy binds to `127.0.0.1`. The audit state directory uses mode 0700 and contains a local comparison key, digests, and audit metadata. AWS separately manages invocation body retention. To bypass the proxy, close the proxied client and start Claude Code with its original environment. For evidence gaps, inspect `reason` and check logs, the invocation identity, text-body delivery and model mappings before repeating the test.

The standalone `check` command exits with 0 for verified, 2 for mismatch, 3 for unverified, or 1 for configuration or execution errors.
