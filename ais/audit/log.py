"""An append-only, tamper-evident record of everything the pipeline did.

Two properties beyond "we wrote some rows":

*Append-only.* SQLite triggers reject ``UPDATE`` and ``DELETE`` on the event
table outright, so the ordinary ways of quietly editing history fail loudly.

*Tamper-evident.* Each row carries a SHA-256 over its own content and the hash
of the row before it. Changing or removing any row breaks every hash after it,
and :meth:`AuditLog.verify` says exactly where. Deleting the whole database is
still possible -- this is evidence of tampering, not prevention of it -- but a
selective edit that hides one bad approval is not.

The log is the only durable artefact of a run: sandboxes are destroyed and
reports are regenerated, but this is what you read six months later to answer
"who approved that, and what did the sandbox say at the time?".
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ais.models import Stage, to_json, utc_now

#: Seed for the first row's ``prev_hash``, so the chain has a defined origin.
GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    backend      TEXT NOT NULL,
    isolated     INTEGER NOT NULL,
    baseline_sha TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    request_id TEXT,
    ts         TEXT NOT NULL,
    stage      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS events_by_request ON events(request_id, id);

-- The log is append-only. These triggers are not a security boundary against a
-- determined attacker with the file, but they turn an accidental or casual
-- rewrite into an error instead of a silent success.
CREATE TRIGGER IF NOT EXISTS events_are_immutable
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'the audit log is append-only: events cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS events_cannot_be_deleted
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'the audit log is append-only: events cannot be deleted');
END;
"""


@dataclass(frozen=True)
class ChainStatus:
    """The result of verifying the hash chain."""

    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str | None = None

    def describe(self) -> str:
        if self.ok:
            return f"audit chain intact across {self.checked} event(s)"
        return f"audit chain BROKEN at event {self.broken_at}: {self.reason}"


def _digest(prev_hash: str, run_id: str, request_id: str | None, ts: str, stage: str, actor: str, payload: str) -> str:
    material = "|".join([prev_hash, run_id, request_id or "", ts, stage, actor, payload])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only event log backed by SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self._subscribers: list[Callable[[dict], None]] = []

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        """Call ``listener`` with every event as it is appended.

        The log is already the canonical record of what the pipeline is doing,
        so a live view of a run is a second *reader* of that record rather than
        a parallel event system that could disagree with it. The UI uses this;
        nothing else needs to.

        Listeners are called after the row is committed, so a listener that
        raises cannot lose an event -- and it is never allowed to fail the write
        either, because a display problem must not become an audit problem.
        """
        self._subscribers.append(listener)

    def _publish(self, event: dict) -> None:
        for listener in list(self._subscribers):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a viewer must never break the log
                pass

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def start_run(self, run_id: str, backend: str, isolated: bool, baseline_sha: str | None, notes: str = "") -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, backend, isolated, baseline_sha, notes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, utc_now(), backend, int(isolated), baseline_sha, notes),
        )
        self._connection.commit()

    def finish_run(self, run_id: str) -> None:
        self._connection.execute(
            "UPDATE runs SET finished_at = ? WHERE run_id = ?", (utc_now(), run_id)
        )
        self._connection.commit()

    def record(
        self,
        run_id: str,
        stage: Stage | str,
        actor: str,
        payload: Any,
        request_id: str | None = None,
    ) -> str:
        """Append one event and return its hash.

        ``actor`` names the component responsible -- ``mediator``, ``verifier``,
        ``reviewer:cli`` -- so the log answers "who did this" and not only "what
        happened".
        """
        stage_value = stage.value if isinstance(stage, Stage) else str(stage)
        body = payload if isinstance(payload, str) else to_json(payload)
        timestamp = utc_now()
        previous = self.head_hash()
        digest = _digest(previous, run_id, request_id, timestamp, stage_value, actor, body)

        self._connection.execute(
            "INSERT INTO events (run_id, request_id, ts, stage, actor, payload, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, request_id, timestamp, stage_value, actor, body, previous, digest),
        )
        self._connection.commit()
        self._publish(
            {
                "run_id": run_id,
                "request_id": request_id,
                "ts": timestamp,
                "stage": stage_value,
                "actor": actor,
                "payload": body,
                "hash": digest,
            }
        )
        return digest

    def head_hash(self) -> str:
        row = self._connection.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        return row["hash"] if row else GENESIS

    # -- reading -----------------------------------------------------------

    def events(self, run_id: str | None = None, request_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM events"
        clauses, parameters = [], []
        if run_id:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if request_id:
            clauses.append("request_id = ?")
            parameters.append(request_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return list(self._connection.execute(query + " ORDER BY id", parameters))

    def runs(self) -> list[sqlite3.Row]:
        return list(self._connection.execute("SELECT * FROM runs ORDER BY started_at DESC"))

    def timeline(self, run_id: str) -> Iterator[tuple[str, str, str, dict]]:
        """``(ts, stage, actor, payload)`` for a run, oldest first."""
        for row in self.events(run_id=run_id):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {"raw": row["payload"]}
            yield row["ts"], row["stage"], row["actor"], payload

    # -- integrity ---------------------------------------------------------

    def verify(self) -> ChainStatus:
        """Recompute the whole chain and report the first row that does not match."""
        previous = GENESIS
        checked = 0
        for row in self._connection.execute("SELECT * FROM events ORDER BY id"):
            if row["prev_hash"] != previous:
                return ChainStatus(
                    ok=False,
                    checked=checked,
                    broken_at=row["id"],
                    reason="prev_hash does not match the previous event -- a row was "
                    "removed, reordered, or inserted",
                )
            expected = _digest(
                previous,
                row["run_id"],
                row["request_id"],
                row["ts"],
                row["stage"],
                row["actor"],
                row["payload"],
            )
            if expected != row["hash"]:
                return ChainStatus(
                    ok=False,
                    checked=checked,
                    broken_at=row["id"],
                    reason="content does not match its recorded hash -- the row was edited",
                )
            previous = row["hash"]
            checked += 1
        return ChainStatus(ok=True, checked=checked)

    def count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
