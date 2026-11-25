"""Capture only audit digests, then reconcile independently retrieved evidence."""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import uuid

from .normalize import normalize_request, normalize_response


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(key, value):
    return hmac.digest(key, _json(value), "sha256").hex()


def _policy(config, key):
    return _digest(key, {
        "relay": config.relay_url,
        "account": config.aws_account,
        "region": config.aws_region,
        "log_group": config.log_group,
        "roles": config.allowed_roles,
        "models": config.model_map,
        "profiles": config.profile_models,
        "skew": config.clock_skew,
        "bucket": config.s3_bucket,
        "prefix": config.s3_prefix,
    })


def capture(config, key, request_body, response_body, started_at, finished_at,
            request_id_hint=None, reason=None, *, probe_tag=None):
    result = {
        "schema_version": 1, "audit_id": uuid.uuid4().hex,
        "mode": "probe" if probe_tag else "passive",
        "started_at": started_at, "finished_at": finished_at,
        "policy": _policy(config, key), "probe_tag": probe_tag,
        "request_id_hint": request_id_hint, "capture_reason": reason,
        "expected_model": request_body.get("model") if isinstance(request_body, dict) else None,
        "relay_model": response_body.get("model") if isinstance(response_body, dict) else None,
        "request_digest": None, "response_digest": None, "compared_fields": [],
    }
    if reason:
        return result
    try:
        wanted = normalize_request(request_body, "anthropic")
        received = normalize_response(response_body, "anthropic")
        result["request_digest"] = _digest(key, wanted)
        result["response_digest"] = _digest(key, received)
        result["compared_fields"] = sorted([f"input.{item}" for item in wanted if item != "schema_version"]
                                         + [f"output.{item}" for item in received if item != "schema_version"])
    except (ValueError, TypeError, AttributeError, OverflowError, UnicodeError, RecursionError):
        result["capture_reason"] = "UNSUPPORTED_CLIENT_FORMAT"
    return result


def finding(item, status, reason, **extra):
    return {
        "schema_version": 1, "audit_id": item["audit_id"], "mode": item["mode"],
        "status": status, "reason": reason, "association_method": None,
        "expected_model": item["expected_model"], "relay_model": item["relay_model"],
        "aws_model_id": None, "resolved_model_id": None, "aws_request_id": None,
        "request_match": None, "response_match": None, "compared_fields": item["compared_fields"],
        **{field: datetime.fromtimestamp(item[field], timezone.utc).isoformat()
           for field in ("started_at", "finished_at")},
        **extra,
    }


def _role_matches(arn, allowed):
    if not isinstance(arn, str):
        return False
    if arn in allowed:
        return True
    match = re.fullmatch(r"arn:(aws(?:-[a-z]+)*):sts::(\d{12}):assumed-role/([^/]+)/[^/]+", arn)
    if not match:
        return False
    partition, account, role = match.groups()
    prefix = f"arn:{partition}:iam::{account}:role/"
    return any(value.startswith(prefix) and value.rsplit("/", 1)[-1] == role for value in allowed)


def _eligible(row, item, config):
    if not isinstance(row, dict) or row.get("schemaType") != "ModelInvocationLog":
        return False
    identity = row.get("identity")
    if (
        row.get("accountId") != config.aws_account or row.get("region") != config.aws_region
        or not isinstance(identity, dict) or not _role_matches(identity.get("arn"), config.allowed_roles)
        or not isinstance(row.get("requestId"), str) or not row["requestId"]
    ):
        return False
    try:
        timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return False
        return item["started_at"] - config.clock_skew <= timestamp.timestamp() <= item["finished_at"] + config.clock_skew
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def _contains_probe(body, tag):
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return False
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"text": content}]
        if isinstance(content, list) and any(
            isinstance(block, dict) and isinstance(block.get("text"), str)
            and block["text"].splitlines() and block["text"].splitlines()[0] == f"Audit tag: {tag}"
            for block in content
        ):
            return True
    return False


def _resolve(model_id, config):
    if not isinstance(model_id, str) or not model_id:
        return None
    mapped = config.profile_models.get(model_id, ())
    if mapped:
        values = {_resolve_basic(model) for model in mapped}
        return next(iter(values)) if None not in values and len(values) == 1 else None
    return _resolve_basic(model_id)


def _resolve_basic(model_id):
    if not isinstance(model_id, str):
        return None
    base = model_id.split("foundation-model/", 1)[1] if "foundation-model/" in model_id else model_id
    # Cross-region aliases start with a geographic component. They require an explicit mapping.
    if re.fullmatch(r"(?:anthropic|amazon|deepseek|qwen|minimax|moonshot|moonshotai|zai|meta|mistral|openai|xai|google|cohere|nvidia|ai21|writer)\.[\w.:-]+", base):
        return base
    return None


def _dialect(row, input_body):
    if row["operation"] in ("Converse", "ConverseStream"):
        return "converse"
    if row["operation"] in ("InvokeModel", "InvokeModelWithResponseStream"):
        if isinstance(input_body, dict) and isinstance(input_body.get("anthropic_version"), str):
            return "anthropic"
        # A native payload needs a separately verified provider-specific adapter.
        return None
    return None


def reconcile(item, config, key, records, hydrate):
    if item["policy"] != _policy(config, key):
        return finding(item, "UNVERIFIED", "CONFIG_CHANGED")
    if item["capture_reason"]:
        return finding(item, "UNVERIFIED", item["capture_reason"])
    unique = {}
    candidates = []
    unresolved = False
    input_fetches = 0
    for row in records:
        if not _eligible(row, item, config):
            continue
        rid = row["requestId"]
        if rid in unique and unique[rid] != row:
            return finding(item, "UNVERIFIED", "CONFLICTING_AWS_RECORDS")
        unique[rid] = row
    for rid, row in unique.items():
        if row.get("schemaVersion") != "1.0" or row.get("operation") not in {
            "Converse", "ConverseStream", "InvokeModel", "InvokeModelWithResponseStream"
        }:
            unresolved = True
            continue
        raw_input = row.get("input")
        input_body = raw_input.get("inputBodyJson") if isinstance(raw_input, dict) else None
        if input_body is None:
            if isinstance(raw_input, dict) and raw_input.get("inputBodyS3Path") is not None:
                input_fetches += 1
                if input_fetches > 8:
                    return finding(item, "UNVERIFIED", "BODY_QUERY_LIMIT")
            try:
                hydrated = hydrate(row, fields=("input",))
                input_body = hydrated.get("input", {}).get("inputBodyJson")
            except Exception as exc:
                if not hasattr(exc, "reason"):
                    raise
                unresolved = True
                continue
        if not isinstance(input_body, dict):
            unresolved = True
            continue
        tag_match = bool(item["probe_tag"] and _contains_probe(input_body, item["probe_tag"]))
        if item["probe_tag"] and not tag_match:
            continue
        hint_match = bool(item["request_id_hint"] and rid == item["request_id_hint"])
        dialect = _dialect(row, input_body)
        try:
            request_match = _digest(key, normalize_request(input_body, dialect)) == item["request_digest"]
        except (ValueError, TypeError, AttributeError, OverflowError, UnicodeError, RecursionError):
            request_match = None
        if request_match is None and not (tag_match or hint_match):
            unresolved = True
        if tag_match or hint_match or request_match is True:
            candidates.append((row, dialect, request_match,
                               "probe_tag" if tag_match else "request_id_hint" if hint_match else "request_digest"))
    if len(candidates) > 1:
        return finding(item, "UNVERIFIED", "AMBIGUOUS_MATCH")
    if not candidates:
        return finding(item, "UNVERIFIED", "EVIDENCE_INCOMPLETE" if unresolved else "NO_MATCHING_RECORD")
    row, dialect, request_match, method = candidates[0]
    model = _resolve(row.get("modelId"), config)
    details = {
        "aws_model_id": row.get("modelId"), "resolved_model_id": model,
        "aws_request_id": row["requestId"], "association_method": method,
        "request_match": request_match,
    }
    if unresolved:
        return finding(item, "UNVERIFIED", "EVIDENCE_INCOMPLETE", **details)
    try:
        output_body = hydrate(row, fields=("output",)).get("output", {}).get("outputBodyJson")
    except Exception as exc:
        if not hasattr(exc, "reason"):
            raise
        return finding(item, "UNVERIFIED", exc.reason, **details)
    if not isinstance(output_body, (dict, list)):
        return finding(item, "UNVERIFIED", "BODY_NOT_AVAILABLE", **details)
    try:
        details["response_match"] = (
            _digest(key, normalize_response(output_body, dialect)) == item["response_digest"]
        )
    except (ValueError, TypeError, AttributeError, OverflowError, UnicodeError, RecursionError):
        return finding(item, "UNVERIFIED", "UNSUPPORTED_AWS_FORMAT", **details)
    if request_match is None:
        return finding(item, "UNVERIFIED", "UNSUPPORTED_AWS_FORMAT", **details)
    if not request_match or not details["response_match"]:
        reason = "REQUEST_MISMATCH" if not request_match else "RESPONSE_MISMATCH"
        return finding(item, "MISMATCH" if method == "probe_tag" else "UNVERIFIED", reason, **details)
    expected = config.model_map.get(item["expected_model"], ())
    allowed = {_resolve(value, config) for value in expected}
    if not model or not allowed or None in allowed:
        return finding(item, "UNVERIFIED", "MODEL_MAPPING_UNKNOWN", **details)
    if model not in allowed:
        return finding(item, "MISMATCH", "MODEL_MISMATCH", **details)
    return finding(item, "VERIFIED", "MATCHED_AWS_RECORD", **details)
