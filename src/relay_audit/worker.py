"""Background evidence collection and retry."""

import asyncio
import logging
import time

from .aws_logs import EvidenceError
from .core import finding, reconcile

LOG = logging.getLogger(__name__)
TRANSIENT = {"LOG_UNAVAILABLE", "LOG_THROTTLED", "LOG_SCAN_TIMEOUT", "BODY_NOT_AVAILABLE", "EVIDENCE_INCOMPLETE"}


def inspect(item, config, key, reader, *, fresh=False):
    if item["capture_reason"]:
        return finding(item, "UNVERIFIED", item["capture_reason"])
    try:
        records = reader.scan(item["started_at"] - config.clock_skew,
                              item["finished_at"] + config.clock_skew,
                              not_before=None if fresh else item["finished_at"] + config.log_wait)
        return reconcile(item, config, key, records, reader.hydrate)
    except EvidenceError as exc:
        return finding(item, "UNVERIFIED", exc.reason)


def process_one(store, config, key, reader):
    task = store.claim(config.name, time.time(), lease_seconds=900)
    if task is None:
        return False
    item, lease = task
    for attempt in range(3):
        result = inspect(item, config, key, reader, fresh=attempt > 0)
        if result["reason"] not in TRANSIENT or attempt == 2:
            break
        time.sleep(2 ** attempt)
    store.finish(item["audit_id"], lease, result, time.time())
    return True


async def run_worker(store, config, key, reader, stop):
    next_prune = 0.0
    while not stop.is_set():
        try:
            now = time.time()
            if now >= next_prune:
                await asyncio.to_thread(store.prune, config.name, now - config.retention_days * 86400)
                next_prune = now + 3600
            if await asyncio.to_thread(process_one, store, config, key, reader):
                continue
        except Exception as exc:
            LOG.error("Audit worker iteration failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), 0.75)
        except TimeoutError:
            pass
