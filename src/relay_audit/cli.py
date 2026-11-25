"""Command line entry point for the relay and evidence tools."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import secrets
import sys
import time

from .config import load_profile, require_relay

STATUSES = ("PENDING", "VERIFIED", "MISMATCH", "UNVERIFIED")
EXIT = {"VERIFIED": 0, "MISMATCH": 2, "UNVERIFIED": 3}


def parser():
    result = argparse.ArgumentParser(prog="relay-audit", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("relay-audit.toml"))
    common.add_argument("--profile", default="default")
    commands = result.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", parents=[common], help="Check local settings and AWS log access")
    doctor.add_argument("--json", action="store_true")
    commands.add_parser("serve", parents=[common], help="Run the proxy and background auditor")
    check = commands.add_parser("check", parents=[common], help="Send one tagged synthetic request")
    check.add_argument("--model", required=True)
    check.add_argument("--json", action="store_true")
    report = commands.add_parser("report", parents=[common], help="Read local findings")
    report.add_argument("--json", action="store_true")
    report.add_argument("--status", choices=STATUSES)
    report.add_argument("--hours", type=int, default=24)
    report.add_argument("--limit", type=int, default=100)
    report.add_argument("--id")
    return result


def show(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return
    rows = value.get("findings") if isinstance(value, dict) else None
    if rows is None and value.get("audit_id"):
        rows = [value]
    if rows is None:
        for field, entry in value.items():
            print(f"{field:22} {entry}")
        return
    if "stats" in value:
        stats = value["stats"]
        print(f"Observed {stats['observed']} · sampled {stats['sampled']} · dropped {stats['dropped']} · {stats['statuses']}")
    if not rows:
        print("No matching audits")
        return
    print("AUDIT ID                         STATUS       REASON                    AWS MODEL")
    for row in rows:
        print(f"{row['audit_id']:32} {row['status']:12} {row['reason']:25} "
              f"{json.dumps(row['aws_model_id'], ensure_ascii=True)}")


def create_server(config, store, key, reader, *, transport=None):
    from .core import capture
    from .proxy import create_app
    from .worker import run_worker

    @asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(store, config, key, reader, stop))
        try:
            yield
        finally:
            stop.set()
            try:
                await asyncio.wait_for(worker, 5)
            except TimeoutError:
                pass

    async def on_capture(**data):
        finished_at = data["finished_at"]
        if secrets.randbelow(10_000) >= config.sample_basis_points:
            await asyncio.to_thread(store.observe, config.name, finished_at)
            return

        def persist():
            item = capture(config, key, **data)
            store.enqueue(item, finished_at + config.log_wait, limit=config.max_queue,
                          profile=config.name, passive=True)
        await asyncio.to_thread(persist)

    return create_app(config, on_capture, transport=transport, lifespan=lifespan)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        print(f"relay-audit: configuration or storage error ({type(exc).__name__})", file=sys.stderr)
        # Config validation messages are generated locally and never include credential values.
        if type(exc) is ValueError:
            print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"relay-audit: {getattr(exc, 'reason', type(exc).__name__)}", file=sys.stderr)
        return 1


def run(args):
    from .aws_logs import AWSReader, EvidenceError
    from .store import Store, installation_key

    config = load_profile(args.config, args.profile)
    if args.command == "doctor":
        result = {"profile": config.name, "aws_region": config.aws_region,
                  "log_group": config.log_group, "relay_key_present": bool(config.relay_key),
                  "local_token_set": len(config.local_token) >= 24, "model_aliases": sorted(config.model_map)}
        try:
            AWSReader(config).doctor()
            result["log_access"] = "OK"
        except EvidenceError as exc:
            result["log_access"] = exc.reason
        show(result, args.json)
        return 0 if result["log_access"] == "OK" and result["relay_key_present"] else 1
    if args.command in {"serve", "check"}:
        require_relay(config, local=args.command == "serve")
    key = installation_key(config.state_dir)
    store = Store(config.state_dir / "audit.db")
    if args.command == "report":
        if not 1 <= args.hours <= 24 * 366 or not 1 <= args.limit <= 10000:
            raise ValueError("hours must be 1–8784 and limit must be 1–10000")
        since = 0 if args.id else time.time() - 3600 * args.hours
        show({"findings": store.report(config.name, since=since, status=args.status,
                                       limit=args.limit, audit_id=args.id),
              "stats": store.stats(config.name, time.time() - 3600 * args.hours)}, args.json)
        return 0
    reader = AWSReader(config)
    if args.command == "serve":
        import uvicorn
        uvicorn.run(create_server(config, store, key, reader), host="127.0.0.1",
                    port=config.port, access_log=False)
        return 0
    if args.model not in config.model_map:
        raise ValueError("Set the exact allowed model IDs for the requested alias in model_map")
    from .probe import send_probe
    from .worker import process_one
    item = send_probe(config, key, args.model)
    due = item["finished_at"] + config.log_wait
    store.enqueue(item, due, limit=config.max_queue, profile=config.name)
    deadline = due + 180
    while True:
        result = store.report(config.name, audit_id=item["audit_id"])[0]
        if result["status"] != "PENDING":
            break
        if time.time() >= deadline:
            from .core import finding
            result = finding(item, "UNVERIFIED", "WORKER_TIMEOUT")
            break
        remaining = due - time.time()
        if remaining > 0:
            time.sleep(min(1, remaining))
        elif not process_one(store, config, key, reader):
            time.sleep(0.5)
    show(result, args.json)
    return EXIT[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
