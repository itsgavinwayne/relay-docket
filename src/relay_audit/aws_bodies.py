"""Hydrate Bedrock invocation bodies from the configured S3 location."""

from contextlib import closing
import gzip
from io import BytesIO
import json
from math import isfinite
import re
import zlib

from botocore.exceptions import BotoCoreError, ClientError


class EvidenceError(Exception):
    """Sanitized error safe to persist as an evidence reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Invalid JSON constant")


def _finite_float(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("JSON number exceeds supported range")
    return number


def load_evidence_json(payload: bytes):
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )


def _body_key(reference, bucket: str | None, prefix: str) -> str:
    # Use the literal AWS-published URI. Avoid URL normalization and key decoding.
    prefix = prefix.strip("/")
    if not bucket or not prefix or not isinstance(reference, str):
        raise EvidenceError("BODY_NOT_AVAILABLE")
    location = f"s3://{bucket}/"
    key = reference.removeprefix(location)
    if (
        key == reference
        or not key.startswith(f"{prefix}/")
        or any(part in ("", ".", "..") for part in key.split("/"))
        or re.search(r"[%?#\\\x00-\x1f\x7f]", key)
        or not key.endswith((".json", ".json.gz"))
    ):
        raise EvidenceError("BODY_NOT_AVAILABLE")
    return key


def _fetch(use_s3, bucket, key, account, limit):
    try:
        params = {"Bucket": bucket, "Key": key}
        if account:
            params["ExpectedBucketOwner"] = account
        response = use_s3().get_object(**params)
        with closing(response["Body"]) as stream:
            size = response["ContentLength"]
            if type(size) is not int or not 0 <= size <= limit:
                raise ValueError("Object size limit")
            payload = stream.read(limit + 1)
        if len(payload) != size:
            raise ValueError("Unexpected object size")
        if key.endswith(".gz") or payload.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=BytesIO(payload)) as compressed:
                payload = compressed.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("Decoded body size limit")
        decoded = load_evidence_json(payload)
        if not isinstance(decoded, (dict, list)):
            raise ValueError("Unexpected body schema")
        return decoded
    except (BotoCoreError, ClientError, OSError, ValueError, KeyError, EOFError, zlib.error, RecursionError):
        raise EvidenceError("BODY_NOT_AVAILABLE") from None


def hydrate_bodies(record: dict, config, use_s3, fields: tuple[str, ...]) -> dict:
    result = record.copy()
    for field in fields:
        section = record.get(field)
        if not isinstance(section, dict):
            continue
        reference = section.get(f"{field}BodyS3Path")
        if reference is None:
            continue
        body_key = f"{field}BodyJson"
        if section.get(body_key) is not None:
            raise EvidenceError("BODY_NOT_AVAILABLE")
        media_type = section.get(f"{field}ContentType", "application/json")
        if (
            not isinstance(media_type, str)
            or media_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise EvidenceError("BODY_NOT_AVAILABLE")
        key = _body_key(reference, config.s3_bucket, config.s3_prefix)
        body = _fetch(use_s3, config.s3_bucket, key, config.aws_account, config.max_body_bytes)
        result[field] = {**section, body_key: body}
    return result
