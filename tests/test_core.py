from dataclasses import replace

from relay_audit.config import Profile
from relay_audit.core import capture, reconcile
from relay_audit.store import Store


KEY = b"x" * 32
MODEL = "anthropic.claude-sonnet-example-v1:0"
PROFILE = Profile(aws_account="123456789012", aws_region="us-east-1",
                  allowed_roles=("arn:aws:iam::123456789012:role/Relay",),
                  model_map={"sonnet": (MODEL, )})
REQUEST = {"model": "sonnet", "max_tokens": 128,
           "messages": [{"role": "user", "content": "say hello"}]}
RESPONSE = {"type": "message", "role": "assistant", "model": "made-up",
            "content": [{"type": "text", "text": "Hello!"}], "stop_reason": "end_turn"}
RECORD = {"schemaType": "ModelInvocationLog", "schemaVersion": "1.0", "timestamp": "2026-11-25T00:00:00Z",
          "accountId": "123456789012", "region": "us-east-1", "requestId": "aws-1",
          "operation": "InvokeModel", "modelId": MODEL,
          "identity": {"arn": "arn:aws:sts::123456789012:assumed-role/Relay/session"},
          "input": {"inputBodyJson": {"anthropic_version": "bedrock-2023-05-31",
              **{key: v for key, v in REQUEST.items() if key != "model"}}},
          "output": {"outputBodyJson": RESPONSE}}
START = 1795564800


def exchange(**kwargs):
    return capture(PROFILE, KEY, REQUEST, RESPONSE, START, START + 1, **kwargs)


def test_verified_and_model_mismatch():
    result = reconcile(exchange(), PROFILE, KEY, [RECORD, RECORD], lambda x, **_: x)
    assert result["status"] == "VERIFIED", result
    assert result["aws_model_id"] == MODEL
    other = RECORD | {"modelId": "anthropic.claude-haiku-example-v1:0"}
    result = reconcile(exchange(), PROFILE, KEY, [other], lambda x, **_: x)
    assert result["status"] == "MISMATCH", result
    assert result["reason"] == "MODEL_MISMATCH"


def test_ambiguous_and_hint_are_conservative():
    assert reconcile(exchange(), PROFILE, KEY, [RECORD, RECORD | {"requestId": "aws-2"}], lambda x, **_: x)["status"] == "UNVERIFIED"
    altered = RECORD | {"output": {"outputBodyJson": RESPONSE | {
        "content": [{"type": "text", "text": "Different"}]}}}
    result = reconcile(exchange(request_id_hint="aws-1"), PROFILE, KEY, [altered], lambda x, **_: x)
    assert result["status"] == "UNVERIFIED"
    assert result["response_match"] is False
    result = reconcile(exchange(request_id_hint="aws-1"), PROFILE, KEY,
                       [altered | {"input": {"inputBodyJson": {}}}, RECORD | {"requestId": "aws-2"}], lambda x, **_: x)
    assert result["status"] == "UNVERIFIED"


def test_configuration_change_and_leases(tmp_path):
    store = Store(tmp_path / "audit.db")
    item = exchange()
    store.enqueue(item, 100, limit=10)
    first = store.claim("default", 100)
    assert first and store.claim("default", 101) is None
    second = store.claim("default", 1000)
    assert second and first[1] != second[1]
    assert not store.finish(item["audit_id"], first[1], {"status":"VERIFIED"}, 1001)
    assert store.finish(item["audit_id"], second[1], {"status":"UNVERIFIED"}, 1002)
    assert store.report("default")[0]["status"] == "UNVERIFIED"
    result = reconcile(item, replace(PROFILE, model_map={}), KEY, [RECORD], lambda x, **_: x)
    assert result["reason"] == "CONFIG_CHANGED"
