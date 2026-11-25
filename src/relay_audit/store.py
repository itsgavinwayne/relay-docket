"""Bounded persistent audit queue with expiring ownership leases."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from .core import finding


def installation_key(directory: Path) -> bytes:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("Use a private state directory with mode 0700")
    path = directory / "hmac.key"
    if not path.exists():
        temp = directory / f".key-{secrets.token_hex(12)}"
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
        finally:
            temp.unlink(missing_ok=True)
    if path.is_symlink() or not path.is_file():
        raise ValueError("HMAC key must be a regular file")
    path.chmod(0o600)
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid local HMAC key")
    return key


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS audits (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, created REAL NOT NULL,
                    due REAL NOT NULL, lease TEXT, lease_until REAL, payload TEXT, result TEXT
                );
                CREATE INDEX IF NOT EXISTS audit_queue ON audits(profile, due) WHERE result IS NULL;
                CREATE INDEX IF NOT EXISTS audit_report ON audits(profile, created);
                CREATE TABLE IF NOT EXISTS counters (
                    profile TEXT NOT NULL, day INTEGER NOT NULL, observed INTEGER NOT NULL,
                    sampled INTEGER NOT NULL, dropped INTEGER NOT NULL, PRIMARY KEY(profile, day)
                );
            """)
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(self, item, due, *, limit, profile="default", passive=False):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM audits WHERE id=?", (item["audit_id"],)).fetchone():
                return False
            pending = connection.execute(
                "SELECT count(*) FROM audits WHERE profile=? AND result IS NULL", (profile,)
            ).fetchone()[0]
            dropped = pending >= limit and not item.get("capture_reason")
            reason = "QUEUE_FULL" if dropped else item.get("capture_reason")
            result = json.dumps(finding(item, "UNVERIFIED", reason)) if reason else None
            connection.execute(
                "INSERT INTO audits(id,profile,created,due,payload,result) VALUES(?,?,?,?,?,?)",
                (item["audit_id"], profile, item["finished_at"], due, None if result else json.dumps(item), result),
            )
            if passive:
                self._count(connection, profile, item["finished_at"], 1, int(dropped))
            return not dropped

    @staticmethod
    def _count(connection, profile, when, sampled, dropped):
        connection.execute("""
            INSERT INTO counters VALUES(?,?,?,?,?)
            ON CONFLICT(profile,day) DO UPDATE SET observed=observed+1,
                sampled=sampled+excluded.sampled, dropped=dropped+excluded.dropped
        """, (profile, int(when // 86400), 1, sampled, dropped))

    def observe(self, profile, when):
        with self.connect() as connection:
            self._count(connection, profile, when, 0, 0)

    def claim(self, profile, now, lease_seconds=120):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT id,payload FROM audits WHERE profile=? AND result IS NULL AND due<=?
                AND (lease_until IS NULL OR lease_until<=?) ORDER BY due LIMIT 1
            """, (profile, now, now)).fetchone()
            if row is None:
                return None
            lease = secrets.token_hex(16)
            connection.execute("UPDATE audits SET lease=?,lease_until=? WHERE id=?",
                               (lease, now + lease_seconds, row[0]))
            return json.loads(row[1]), lease

    def finish(self, audit_id, lease, result, now):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE audits SET result=?, payload=NULL, lease=NULL,lease_until=NULL
                WHERE id=? AND lease=? AND lease_until>? AND result IS NULL
            """, (json.dumps(result), audit_id, lease, now))
            return cursor.rowcount == 1

    def report(self, profile, *, since=0, status=None, limit=100, audit_id=None):
        sql = "SELECT payload,result FROM audits WHERE profile=? AND created>=?"
        parameters = [profile, since]
        if audit_id:
            sql += " AND id=?"
            parameters.append(audit_id)
        if status == "PENDING":
            sql += " AND result IS NULL"
        elif status:
            sql += " AND json_extract(result,'$.status')=?"
            parameters.append(status)
        sql += " ORDER BY created DESC, id DESC LIMIT ?"
        with self.connect() as connection:
            return [json.loads(result) if result else finding(json.loads(payload), "PENDING", "AWAITING_LOGS")
                    for payload, result in connection.execute(sql, (*parameters, limit))]

    def stats(self, profile, since):
        with self.connect() as connection:
            counts = connection.execute("""
                SELECT coalesce(sum(observed),0),coalesce(sum(sampled),0),coalesce(sum(dropped),0)
                FROM counters WHERE profile=? AND day>=?
            """, (profile, int(since // 86400))).fetchone()
            statuses = dict(connection.execute("""
                SELECT coalesce(json_extract(result,'$.status'),'PENDING'),count(*)
                FROM audits WHERE profile=? AND created>=?
                AND coalesce(json_extract(result,'$.mode'),json_extract(payload,'$.mode'))='passive'
                GROUP BY 1
            """, (profile, int(since // 86400) * 86400)))
        observed, sampled, dropped = counts
        failures = statuses.get("UNVERIFIED", 0)
        return {"window_start_utc_day": int(since // 86400), "observed": observed, "sampled": sampled,
                "dropped": dropped, "statuses": statuses, "sampling_rate": sampled / observed if observed else None,
                "unverified_rate": failures / sampled if sampled else None}

    def prune(self, profile, cutoff):
        with self.connect() as connection:
            connection.execute("DELETE FROM audits WHERE profile=? AND created<?", (profile, cutoff))
            connection.execute("DELETE FROM counters WHERE profile=? AND day<?", (profile, int(cutoff // 86400)))
