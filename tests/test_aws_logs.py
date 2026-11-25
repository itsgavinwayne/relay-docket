import gzip
import json
from io import BytesIO
from types import SimpleNamespace

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest


@pytest.fixture
def reader():
    from relay_audit.aws_logs import AWSReader

    config = SimpleNamespace(
        aws_profile=None,
        aws_region="us-east-1",
        aws_account="123456789012",
        log_group="/audit/bedrock",
        s3_bucket="audit-bodies",
        s3_prefix="traces",
        max_body_bytes=128,
        max_log_pages=3,
    )
    session = boto3.Session(
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        region_name=config.aws_region,
    )
    logs = Stubber(session.client("logs"))
    s3 = Stubber(session.client("s3"))
    sleeps = []
    instance = AWSReader(
        config, logs=logs.client, s3=s3.client, sleep=sleeps.append, clock=lambda: 50.0
    )
    with logs, s3:
        yield instance, logs, s3, sleeps
        logs.assert_no_pending_responses()
        s3.assert_no_pending_responses()


def event(record, timestamp=10_500):
    return {"timestamp": timestamp, "message": json.dumps(record)}


def invocation(model="anthropic.sonnet"):
    return {
        "schemaType": "ModelInvocationLog",
        "accountId": "123456789012",
        "region": "us-east-1",
        "requestId": "abc",
        "modelId": model,
    }


def query(**extra):
    return {"logGroupName": "/audit/bedrock", "startTime": 10_000, "endTime": 11_000, **extra}


def test_doctor_checks_cloudwatch_group_read(reader):
    audit, logs, _, _ = reader
    logs.add_response("filter_log_events", {"events": []}, {
        "logGroupName": "/audit/bedrock", "limit": 1,
    })
    assert audit.doctor() is None


def test_scan_advances_empty_pages_and_deduplicates_identical_records(reader):
    audit, logs, _, _ = reader
    record = invocation()
    logs.add_response("filter_log_events", {"events": [], "nextToken": "first"}, query())
    logs.add_response("filter_log_events", {
        "events": [event(record), event(record)],
    }, query(nextToken="first"))
    assert audit.scan(10, 11) == [record]


def test_scan_rejects_conflicting_duplicate_invocations(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, logs, _, _ = reader
    logs.add_response("filter_log_events", {
        "events": [event(invocation()), event(invocation("other"))],
    }, query())
    with pytest.raises(EvidenceError, match="LOG_INCOMPLETE"):
        audit.scan(10, 11)


def test_scan_rejects_malformed_log_event_in_window(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, logs, _, _ = reader
    logs.add_response("filter_log_events", {
        "events": [{"timestamp": 10_500, "message": "{broken"}],
    }, query())
    with pytest.raises(EvidenceError, match="LOG_INCOMPLETE"):
        audit.scan(10, 11)


def test_scan_rejects_repeated_token(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, logs, _, _ = reader
    logs.add_response("filter_log_events", {"events": [], "nextToken": "same"}, query())
    logs.add_response("filter_log_events", {"events": [], "nextToken": "same"}, query(nextToken="same"))
    with pytest.raises(EvidenceError, match="LOG_INCOMPLETE"):
        audit.scan(10, 11)


def test_scan_rejects_page_limit_without_returning_partial_records(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, logs, _, _ = reader
    logs.add_response("filter_log_events", {
        "events": [event(invocation())], "nextToken": "1",
    }, query())
    logs.add_response("filter_log_events", {"nextToken": "2"}, query(nextToken="1"))
    logs.add_response("filter_log_events", {"nextToken": "3"}, query(nextToken="2"))
    with pytest.raises(EvidenceError, match="LOG_INCOMPLETE"):
        audit.scan(10, 11)


def test_scan_retries_throttling_with_backoff(reader):
    audit, logs, _, delays = reader
    logs.add_client_error("filter_log_events", "ThrottlingException", expected_params=query())
    logs.add_response("filter_log_events", {"events": [event(invocation())]}, query())
    assert audit.scan(10, 11) == [invocation()]
    assert len(delays) == 1
    assert delays[0] > 0


def test_scan_has_bounded_throttling_retries(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, logs, _, delays = reader
    for _ in range(3):
        logs.add_client_error("filter_log_events", "ThrottlingException", expected_params=query())
    with pytest.raises(EvidenceError, match="LOG_UNAVAILABLE"):
        audit.scan(10, 11)
    assert 0 < delays[0] < delays[1]


def test_scan_access_denial_does_not_expose_service_message(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, logs, _, delays = reader
    logs.add_client_error(
        "filter_log_events", "AccessDeniedException", "internal account metadata", expected_params=query()
    )
    with pytest.raises(EvidenceError) as exc:
        audit.scan(10, 11)
    assert exc.value.reason == "LOG_ACCESS_DENIED"
    assert str(exc.value) == exc.value.reason
    assert delays == []


def test_nearby_windows_share_only_fresh_complete_scans(reader):
    audit, logs, _, _ = reader
    record = invocation()
    other = dict(invocation(), requestId="abcd")
    bucket = {"logGroupName": "/audit/bedrock", "startTime": 0, "endTime": 14_999}
    logs.add_response("filter_log_events", {
        "events": [event(record), event(other, 11_500)],
    }, bucket)
    assert audit.scan(10, 11, not_before=49) == [record]
    assert audit.scan(11, 12, not_before=50) == [other]

    audit._clock = lambda: 51.0
    logs.add_response("filter_log_events", {
        "events": [event(record), event(other, 11_500)],
    }, bucket)
    assert audit.scan(10, 11, not_before=51) == [record]


def s3_record(path="s3://audit-bodies/traces/data/example_input.json.gz"):
    return {"input": {"inputContentType": "application/json", "inputBodyS3Path": path},
            "output": {"outputBodyJson": {"text": "inline"}}}


def stub_object(s3, data, key="traces/data/example_input.json.gz", size=None):
    body = BytesIO(data)
    stream = StreamingBody(body, len(data))
    response = {"Body": stream, "ContentLength": len(data) if size is None else size}
    s3.add_response("get_object", response, {"Bucket": "audit-bodies", "Key": key, "ExpectedBucketOwner": "123456789012"})
    return body


def test_hydrate_loads_gzip_json_and_preserves_input(reader):
    audit, _, s3, _ = reader
    body = stub_object(s3, gzip.compress(b'{"messages":[]}'))
    record = s3_record()
    hydrated = audit.hydrate(record)
    assert hydrated["input"]["inputBodyJson"] == {"messages": []}
    assert hydrated["output"] == record["output"]
    assert "inputBodyJson" not in record["input"]
    assert body.closed


def test_hydrate_reads_plain_json_output_arrays(reader):
    audit, _, s3, _ = reader
    stub_object(s3, b'[{"type":"message_stop"}]', key="traces/data/example_output.json")
    record = {
        "input": {"inputBodyJson": {"messages": []}},
        "output": {"outputBodyS3Path": "s3://audit-bodies/traces/data/example_output.json"},
    }
    assert audit.hydrate(record)["output"]["outputBodyJson"] == [{"type": "message_stop"}]


@pytest.mark.parametrize("path", [
    "s3://other-bucket/traces/body.json",
    "s3://audit-bodies/traces-other/body.json",
    "s3://audit-bodies/traces/../private/body.json",
    "s3://audit-bodies/traces/%2e%2e/private/body.json",
    "s3://audit-bodies/traces/body.png",
    "https://audit-bodies/traces/body.json",
    "s3://audit-bodies/traces/body.json?versionId=abc",
])
def test_hydrate_rejects_unallowlisted_references_before_fetching(reader, path):
    from relay_audit.aws_logs import EvidenceError

    audit, _, _, _ = reader
    with pytest.raises(EvidenceError, match="BODY_NOT_AVAILABLE"):
        audit.hydrate(s3_record(path))


@pytest.mark.parametrize("payload, size", [
    (b"{}", 129),
    (gzip.compress(b'{"data":"' + (b"x" * 128) + b'"}'), None),
    (gzip.compress(b'{"secret":1,"secret":2}'), None),
    (gzip.compress(b'{}')[:-2], None),
])
def test_hydrate_rejects_oversize_and_invalid_objects_and_closes_stream(reader, payload, size):
    from relay_audit.aws_logs import EvidenceError

    audit, _, s3, _ = reader
    body = stub_object(s3, payload, size=size)
    with pytest.raises(EvidenceError, match="BODY_NOT_AVAILABLE"):
        audit.hydrate(s3_record())
    assert body.closed


@pytest.mark.parametrize("error_code", ["AccessDenied", "NoSuchKey"])
def test_hydrate_handles_denied_or_late_s3_objects(reader, error_code):
    from relay_audit.aws_logs import EvidenceError

    audit, _, s3, _ = reader
    key = "traces/data/example_input.json.gz"
    s3.add_client_error("get_object", error_code, expected_params={"Bucket": "audit-bodies", "Key": key, "ExpectedBucketOwner": "123456789012"})
    with pytest.raises(EvidenceError, match="BODY_NOT_AVAILABLE"):
        audit.hydrate(s3_record())


def test_hydrate_rejects_references_when_s3_is_disabled(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, _, _, _ = reader
    audit.config.s3_bucket = None
    with pytest.raises(EvidenceError, match="BODY_NOT_AVAILABLE"):
        audit.hydrate(s3_record())


def test_hydrate_selected_input_does_not_touch_output_reference(reader):
    audit, _, s3, _ = reader
    stub_object(s3, gzip.compress(b'{"messages":[]}'))
    record = s3_record()
    record["output"] = {"outputBodyS3Path": "s3://other-bucket/traces/output.json"}
    hydrated = audit.hydrate(record, fields=("input",))
    assert hydrated["input"]["inputBodyJson"] == {"messages": []}
    assert hydrated["output"] == record["output"]


def test_hydrate_rejects_finite_number_overflow(reader):
    from relay_audit.aws_logs import EvidenceError

    audit, _, s3, _ = reader
    stub_object(s3, gzip.compress(b'{"temperature":1e400}'))
    with pytest.raises(EvidenceError, match="BODY_NOT_AVAILABLE"):
        audit.hydrate(s3_record())
