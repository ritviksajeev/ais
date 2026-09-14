"""The audit log: append-only, and tamper-evident when it is not."""

from __future__ import annotations

import sqlite3

import pytest

from ais.audit import AuditLog
from ais.audit.log import GENESIS
from ais.models import Stage


@pytest.fixture
def log(tmp_path):
    instance = AuditLog(tmp_path / "audit.db")
    instance.start_run("run-1", "docker", True, "abc123")
    yield instance
    instance.close()


def fill(log, count=5):
    for index in range(count):
        log.record("run-1", Stage.REQUEST_RECEIVED, "editor", {"i": index}, f"req-{index}")


class TestRecording:
    def test_events_are_stored_in_order(self, log):
        fill(log, 3)
        assert [row["request_id"] for row in log.events()] == ["req-0", "req-1", "req-2"]

    def test_the_first_event_chains_from_genesis(self, log):
        fill(log, 1)
        assert log.events()[0]["prev_hash"] == GENESIS

    def test_each_event_chains_from_the_last(self, log):
        fill(log, 4)
        rows = log.events()
        for previous, current in zip(rows, rows[1:]):
            assert current["prev_hash"] == previous["hash"]

    def test_the_actor_is_recorded(self, log):
        log.record("run-1", Stage.DECIDED, "reviewer:cli", {"approved": True}, "req-x")
        assert log.events()[-1]["actor"] == "reviewer:cli"

    def test_a_run_is_recorded_with_its_backend_and_isolation(self, log):
        run = log.runs()[0]
        assert run["backend"] == "docker" and bool(run["isolated"]) is True


class TestAppendOnly:
    def test_updates_are_rejected(self, log):
        fill(log, 2)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            log._connection.execute("UPDATE events SET payload = 'x' WHERE id = 1")

    def test_deletes_are_rejected(self, log):
        fill(log, 2)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            log._connection.execute("DELETE FROM events WHERE id = 1")


class TestIntegrity:
    def test_an_untouched_chain_verifies(self, log):
        fill(log, 6)
        status = log.verify()
        assert status.ok and status.checked == 6

    def test_an_empty_log_verifies(self, log):
        assert log.verify().ok

    def test_editing_a_row_breaks_the_chain(self, tmp_path, log):
        fill(log, 5)
        path = log.path
        log.close()

        # Drop the triggers first: this is the attacker who already has the file.
        connection = sqlite3.connect(str(path))
        connection.executescript(
            "DROP TRIGGER events_are_immutable;"
            "UPDATE events SET payload = '{\"i\":99}' WHERE id = 3;"
        )
        connection.commit()
        connection.close()

        reopened = AuditLog(path)
        status = reopened.verify()
        assert not status.ok and status.broken_at == 3
        assert "edited" in status.reason
        reopened.close()

    def test_removing_a_row_breaks_the_chain(self, tmp_path, log):
        fill(log, 5)
        path = log.path
        log.close()

        connection = sqlite3.connect(str(path))
        connection.executescript(
            "DROP TRIGGER events_cannot_be_deleted; DELETE FROM events WHERE id = 3;"
        )
        connection.commit()
        connection.close()

        reopened = AuditLog(path)
        status = reopened.verify()
        assert not status.ok and status.broken_at == 4
        assert "removed" in status.reason
        reopened.close()

    def test_the_chain_survives_reopening_the_database(self, tmp_path, log):
        fill(log, 3)
        path = log.path
        log.close()
        reopened = AuditLog(path)
        reopened.record("run-1", Stage.APPLIED, "mediator", {"commit": "deadbeef"}, "req-9")
        assert reopened.verify().ok
        reopened.close()


class TestReading:
    def test_events_can_be_filtered_by_request(self, log):
        fill(log, 4)
        assert len(log.events(request_id="req-2")) == 1

    def test_the_timeline_decodes_payloads(self, log):
        log.record("run-1", Stage.VERIFIED, "verifier", {"verdict": "BLOCK"}, "req-1")
        _, stage, actor, payload = list(log.timeline("run-1"))[-1]
        assert stage == "verified" and actor == "verifier" and payload["verdict"] == "BLOCK"
