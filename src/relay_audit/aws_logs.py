"""Read complete CloudWatch evidence windows and hydrate Bedrock S3 references."""

from collections import OrderedDict
import json
from math import ceil, floor, isfinite
from threading import Lock
from time import monotonic, sleep as sleep_seconds, time

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
)

from .aws_bodies import EvidenceError, hydrate_bodies, load_evidence_json

__all__ = ["AWSReader", "EvidenceError"]

_SCAN_SECONDS = 20
_BUCKET_MS = 15_000
_CACHE_ENTRIES = 4
_CACHE_ENTRY_BYTES = 2 * 1024**2
_SCAN_BYTES = 16 * 1024**2
_MAX_EVENTS = 20_000
_RETRYABLE = {
    "InternalFailure", "InternalServerError", "OperationAbortedException",
    "RequestLimitExceeded", "ServiceUnavailable", "ServiceUnavailableException",
    "ThrottledException", "Throttling", "ThrottlingException", "TooManyRequestsException",
}
_ACCESS_DENIED = {
    "AccessDenied", "AccessDeniedException", "ExpiredToken", "ExpiredTokenException",
    "InvalidClientTokenId", "UnrecognizedClientException", "UnauthorizedOperation",
}
_CREDENTIAL_ERRORS = (
    CredentialRetrievalError, NoCredentialsError, PartialCredentialsError, ProfileNotFound,
)


class AWSReader:
    def __init__(self, config, *, logs=None, s3=None, sleep=sleep_seconds, clock=time):
        self.config = config
        self._clients = {"logs": logs, "s3": s3}
        self._session = None
        self._client_lock = Lock()
        self._scan_lock = Lock()
        self._sleep = sleep
        self._clock = clock
        self._cache: OrderedDict[tuple[int, int], tuple[float, list[tuple[int, bytes]]]] = OrderedDict()

    def _client(self, service: str):
        with self._client_lock:
            if self._clients[service] is None:
                if self._session is None:
                    self._session = boto3.Session(profile_name=self.config.aws_profile)
                self._clients[service] = self._session.client(
                    service,
                    region_name=self.config.aws_region,
                    config=Config(
                        connect_timeout=2,
                        read_timeout=3,
                        retries={"total_max_attempts": 1, "mode": "standard"},
                        ignore_configured_endpoint_urls=True,
                    ),
                )
            return self._clients[service]

    def _query(self, params: dict, deadline: float) -> dict:
        for delay in (0.0, 0.1, 0.2):
            if monotonic() + delay >= deadline:
                break
            if delay:
                self._sleep(delay)
            try:
                result = self._client("logs").filter_log_events(**params)
                if monotonic() >= deadline:
                    break
                return result
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in _ACCESS_DENIED:
                    raise EvidenceError("LOG_ACCESS_DENIED") from None
                if code not in _RETRYABLE:
                    raise EvidenceError("LOG_UNAVAILABLE") from None
            except _CREDENTIAL_ERRORS:
                raise EvidenceError("LOG_ACCESS_DENIED") from None
            except (ConnectionError, ConnectTimeoutError, ReadTimeoutError):
                continue
            except BotoCoreError:
                raise EvidenceError("LOG_UNAVAILABLE") from None
        raise EvidenceError("LOG_UNAVAILABLE")

    def doctor(self) -> None:
        self._query({"logGroupName": self.config.log_group, "limit": 1}, monotonic() + _SCAN_SECONDS)

    def _scan_events(self, start: int, end: int, deadline: float) -> tuple[list[tuple[int, bytes]], int]:
        params = {"logGroupName": self.config.log_group, "startTime": start, "endTime": end}
        tokens = set()
        events = []
        size = 0
        for _ in range(self.config.max_log_pages):
            page = self._query(params, deadline)
            for event in page.get("events", []):
                timestamp = event.get("timestamp")
                message = event.get("message")
                if type(timestamp) is not int or not isinstance(message, str):
                    raise EvidenceError("LOG_INCOMPLETE")
                if not start <= timestamp <= end:
                    continue
                try:
                    data = message.encode("utf-8")
                except UnicodeError:
                    raise EvidenceError("LOG_INCOMPLETE") from None
                size += len(data)
                if size > _SCAN_BYTES or len(events) >= _MAX_EVENTS:
                    raise EvidenceError("LOG_INCOMPLETE")
                events.append((timestamp, data))
            token = page.get("nextToken")
            if token is None:
                return events, size
            if not token or token in tokens:
                break
            tokens.add(token)
            params["nextToken"] = token
        raise EvidenceError("LOG_INCOMPLETE")

    def scan(self, start: float, end: float, *, not_before: float | None = None) -> list[dict]:
        """Scan after the evidence deadline, optionally sharing fresh nearby windows."""
        if not isfinite(start) or not isfinite(end) or not 0 <= start <= end:
            raise EvidenceError("LOG_INCOMPLETE")
        deadline = monotonic() + _SCAN_SECONDS
        start_ms, end_ms = floor(start * 1000), ceil(end * 1000)
        bounds = (start_ms, end_ms)
        if not self._scan_lock.acquire(timeout=_SCAN_SECONDS):
            raise EvidenceError("LOG_UNAVAILABLE")
        try:
            started = self._clock()
            if not_before is not None and (not isfinite(not_before) or started < not_before):
                raise EvidenceError("LOG_UNAVAILABLE")
            bucket = (start_ms // _BUCKET_MS * _BUCKET_MS, (end_ms // _BUCKET_MS + 1) * _BUCKET_MS - 1)
            use_cache = (
                not_before is not None
                and bucket[1] - bucket[0] < 3 * _BUCKET_MS
                and bucket[1] <= started * 1000
            )
            # Round outward by at most two buckets and reuse only scans begun
            # at or after this job's delivery deadline.
            if use_cache:
                bounds = bucket
                saved = self._cache.get(bounds)
                if saved is not None and saved[0] >= not_before:
                    self._cache.move_to_end(bounds)
                    return _decode_records(saved[1], start_ms, end_ms)

            events, size = self._scan_events(*bounds, deadline)
            records = _decode_records(events, start_ms, end_ms)
            if use_cache and size <= _CACHE_ENTRY_BYTES:
                self._cache[bounds] = (started, events)
                self._cache.move_to_end(bounds)
                if len(self._cache) > _CACHE_ENTRIES:
                    self._cache.popitem(last=False)
            return records
        finally:
            self._scan_lock.release()

    def hydrate(self, record: dict, *, fields: tuple[str, ...] = ("input", "output")) -> dict:
        return hydrate_bodies(record, self.config, lambda: self._client("s3"), fields)


def _decode_records(events: list[tuple[int, bytes]], start: int, end: int) -> list[dict]:
    records = []
    seen = {}
    for timestamp, message in events:
        if not start <= timestamp <= end:
            continue
        try:
            record = load_evidence_json(message)
            if not isinstance(record, dict):
                raise ValueError("Expected an object")
            key = tuple(record.get(field) for field in ("accountId", "region", "requestId"))
            if all(isinstance(part, str) and part for part in key):
                canonical = json.dumps(
                    record, sort_keys=True, allow_nan=False, separators=(",", ":")
                )
                if key in seen:
                    if seen[key] != canonical:
                        raise ValueError("Conflicting invocation records")
                    continue
                seen[key] = canonical
        except (ValueError, RecursionError):
            raise EvidenceError("LOG_INCOMPLETE") from None
        records.append(record)
    return records
