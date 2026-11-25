# Relay Docket

[简体中文](README.zh-CN.md) · [Operations guide](docs/operations.en.md) · [Model coverage](docs/verified-models.md)

Relay Docket audits an Anthropic-compatible relay against Amazon Bedrock invocation logs you can read independently. It looks for a matching provider record and checks the model ID, request, and returned content. Use it for a single synthetic probe or to sample routine Claude Code traffic after responses finish.

| Mode | What to run | What it checks |
| --- | --- | --- |
| One-off probe | `relay-audit check` | One standalone test call with a random tag |
| Passive sampling | `relay-audit serve` | Real traffic forwarded through a loopback proxy. Defaults to 5% and samples after the response. |
| Report | `relay-audit report` | Local results, including AWS request IDs and mismatch or evidence-gap reasons |

## Quick start

**Requirements** Python 3.12+, an Anthropic-compatible relay that routes the test traffic to an AWS Bedrock Runtime account, and independent read access to that account’s invocation logs. AWS credentials use the local SDK credential chain. Configure Bedrock to deliver text input and output to a dedicated CloudWatch Logs group. Grant the audit identity `logs:FilterLogEvents` on that group. [AWS setup guide](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html)

```bash
git clone https://github.com/itsgavinwayne/relay-docket.git
cd relay-docket
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
cp config.example.toml relay-audit.toml
```

The [release page](https://github.com/itsgavinwayne/relay-docket/releases/latest) also provides a wheel. To use it, download the wheel and replace `python -m pip install .` with `python -m pip install /path/to/relay_audit-0.1.0-py3-none-any.whl`.

Edit `relay-audit.toml` and set the relay base URL without `/v1`, authentication method, AWS profile/account/region/log group, invocation-role ARN, and expected model IDs. The keys in `model_map` are the model names you send to your relay. When AWS records an inference profile, map it to its verified base model in `profile_models`.

Set `RELAY_AUDIT_RELAY_KEY` in your local environment or secret manager. Run these commands in that same environment.

```bash
relay-audit doctor --config relay-audit.toml --json
relay-audit check --config relay-audit.toml --model claude-sonnet --json
```

Replace `claude-sonnet` with one of your configured aliases. `doctor` checks log access. `check` sends one billable test request and defaults to waiting about three minutes for log delivery.

## Sample real traffic

In the proxy terminal, set a local token and start the audit proxy. For initial acceptance testing, set `sample_basis_points = 10000` and restart the proxy. Restore `500` for 5% sampling.

```bash
export RELAY_AUDIT_LOCAL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
relay-audit serve --config relay-audit.toml
```

Launch Claude Code with the same local token available in its environment. Use a model alias your relay accepts and that you have configured in `model_map`.

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

## Interpret results

- `VERIFIED` means one independently read Bedrock record matches the configured model and the comparable request and response.
- `MISMATCH` means sufficiently correlated evidence shows a difference.
- `UNVERIFIED` means missing, incomplete, ambiguous, out-of-scope, or inaccessible evidence. A relay URL and key alone cannot establish the actual upstream model.
- `PENDING` means the background job is still waiting or processing.

Each result covers one audited call. The tool supports Claude native records and comparable Bedrock Converse records, with fixture coverage for DeepSeek, GLM, Qwen, Kimi, and MiniMax. S3-backed body recovery and a [Codex Skill](skills/relay-audit/SKILL.md) are available. Live behavior depends on your relay and Bedrock account. [Local validation record](docs/release-validation.md)

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

The default suite uses local fixtures and mock services. For the opt-in AWS acceptance test and S3 setup, see the [operations guide](docs/operations.en.md).
