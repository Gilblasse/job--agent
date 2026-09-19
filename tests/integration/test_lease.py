"""The publisher lease and the request queue.

Two runners must never publish at once; a runner that loses its lease must find out
from its own writes, not from anyone tidying up; and however many browser tabs click
"Search jobs", one request waits.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from jobagent.infra.store import LEASE_SECONDS, Lease, LeaseLost, Store

T0 = datetime(2026, 9, 14, 12, 0, 0)
EXPIRED = T0 + timedelta(seconds=LEASE_SECONDS + 60)


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


class TestTakingTheLease:
    def test_the_second_taker_is_refused_while_the_first_is_fresh(self, store):
        assert store.take_lease(T0) is not None
        assert store.take_lease(T0 + timedelta(seconds=30)) is None

    def test_an_expired_lease_can_be_taken(self, store):
        assert store.take_lease(T0) is not None
        assert store.take_lease(EXPIRED) is not None

    def test_concurrent_takers_on_one_file_yield_exactly_one_lease(self, tmp_path):
        path = str(tmp_path / "shared.sqlite3")
        Store(path).close()
        won: list[bool] = []
        barrier = threading.Barrier(4)

        def attempt():
            store = Store(path)
            barrier.wait()
            won.append(store.take_lease(T0) is not None)
            store.close()

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert won.count(True) == 1

    def test_renewal_pushes_expiry_and_a_stranger_cannot_renew(self, store):
        lease = store.take_lease(T0)
        assert store.renew_lease(lease, T0 + timedelta(seconds=200))
        assert store.take_lease(T0 + timedelta(seconds=LEASE_SECONDS + 30)) is None
        stranger = store.take_lease(EXPIRED + timedelta(seconds=500))
        assert stranger is not None
        assert not store.renew_lease(lease, EXPIRED + timedelta(seconds=500))


class TestTakeover:
    def test_takeover_invalidates_the_previous_owner_without_any_reconciliation(self, store):
        first = store.take_lease(T0)
        first = store.consume_request(first, T0, "schedule")
        second = store.take_lease(EXPIRED)
        assert second is not None

        # The old owner cannot renew...
        assert not store.renew_lease(first, EXPIRED)
        # ...cannot write through the guard, in a transaction or a batch...
        with pytest.raises(LeaseLost):
            store.abandon_stale_runs(first, EXPIRED)
        with pytest.raises(LeaseLost):
            store._batch([
                *store.guard_statements(first, EXPIRED),
                ("INSERT INTO run_sources (run_id, source, status) VALUES (1, 'x', 'ok')", ()),
            ])
        assert store.conn.execute("SELECT COUNT(*) AS n FROM run_sources").fetchone()["n"] == 0
        # ...and cannot report success; its request was closed by the takeover itself.
        assert store.finish_request(first, "succeeded", "done", 0, EXPIRED) is False
        old = store.get_request(first.request_id)
        assert old["status"] == "failed" and "taken over" in old["note"]

        # The new owner works normally.
        second = store.consume_request(second, EXPIRED, "schedule")
        assert store.finish_request(second, "succeeded", "ok", 5, EXPIRED)
        assert store.get_request(second.request_id)["rows_written"] == 5

    def test_an_expired_lease_refuses_to_write_even_if_nobody_took_it(self, store):
        lease = store.take_lease(T0)
        lease = store.consume_request(lease, T0, "schedule")
        assert not store.renew_lease(lease, EXPIRED)
        with pytest.raises(LeaseLost):
            store.update_new_count(1, lease, EXPIRED)
        assert store.finish_request(lease, "succeeded", "", 0, EXPIRED) is False

    def test_every_guarded_write_renews_the_lease(self, store):
        """A long publish keeps itself alive; the heartbeat thread is only a fallback."""
        lease = store.take_lease(T0)
        lease = store.consume_request(lease, T0, "schedule")
        later = T0 + timedelta(seconds=LEASE_SECONDS - 10)
        store.abandon_stale_runs(lease, later)  # any guarded write
        renewed_until = (later + timedelta(seconds=LEASE_SECONDS)).isoformat()
        assert store.lease_row()["expires_at"] == renewed_until
        assert store.renew_lease(lease, later + timedelta(seconds=LEASE_SECONDS - 5))
        # A stranger's guard neither passes nor renews.
        with pytest.raises(LeaseLost):
            store.abandon_stale_runs(Lease("someone-else"), later)

    def test_finishing_releases_the_lease_early(self, store):
        lease = store.take_lease(T0)
        lease = store.consume_request(lease, T0, "schedule")
        assert store.finish_request(lease, "succeeded", "", 0, T0)
        assert store.take_lease(T0 + timedelta(seconds=1)) is not None


class TestQueue:
    def test_a_second_request_attaches_to_the_waiting_one(self, store):
        first, attached = store.create_queued("web", "uid-a", T0)
        assert not attached
        second, attached = store.create_queued("web", "uid-b", T0 + timedelta(seconds=1))
        assert attached and second == first
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM run_requests WHERE status = 'queued'"
        ).fetchone()["n"] == 1

    def test_concurrent_clicks_produce_one_queued_request(self, tmp_path):
        path = str(tmp_path / "shared.sqlite3")
        Store(path).close()
        results: list[tuple[str, bool]] = []
        barrier = threading.Barrier(4)

        def click():
            store = Store(path)
            barrier.wait()
            results.append(store.create_queued("web", None, T0))
            store.close()

        threads = [threading.Thread(target=click) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len({request_id for request_id, _ in results}) == 1
        assert [attached for _, attached in results].count(False) == 1

    def test_the_runner_consumes_the_queued_request_with_its_priority(self, store):
        request_id, _ = store.create_queued("web", "uid-first", T0)
        lease = store.take_lease(T0)
        lease = store.consume_request(lease, T0, "schedule")
        assert lease.request_id == request_id
        row = store.get_request(request_id)
        assert row["status"] == "running" and row["priority_uid"] == "uid-first"
        assert row["lease_token"] == lease.token
        assert store.active_request(T0)["id"] == request_id

    def test_the_runner_opens_a_scheduled_request_when_nothing_waits(self, store):
        lease = store.take_lease(T0)
        lease = store.consume_request(lease, T0, "schedule")
        row = store.get_request(lease.request_id)
        assert row["origin"] == "schedule" and row["status"] == "running"

    def test_a_cancelled_request_is_not_claimed(self, store):
        request_id, _ = store.create_queued("web", None, T0)
        assert store.cancel_queued(request_id, T0)
        lease = store.consume_request(store.take_lease(T0), T0, "schedule")
        assert lease.request_id != request_id
        assert store.get_request(request_id)["status"] == "cancelled"
        assert not store.cancel_queued(request_id, T0)

    def test_after_a_cancel_a_new_request_can_queue(self, store):
        first, _ = store.create_queued("web", None, T0)
        store.cancel_queued(first, T0)
        second, attached = store.create_queued("web", None, T0)
        assert second != first and not attached

    def test_reconciliation_only_labels_what_is_already_over(self, store):
        stale, _ = store.create_queued("web", None, T0 - timedelta(hours=3))
        lease = store.take_lease(T0)
        lease = store.consume_request(lease, T0, "schedule")  # consumes the stale one
        assert lease.request_id == stale
        # A runner that never finished and whose lease expired.
        dead = store.take_lease(EXPIRED)
        dead = store.consume_request(dead, EXPIRED, "schedule")
        much_later = EXPIRED + timedelta(hours=1)
        fresh, _ = store.create_queued("web", None, much_later)

        store.reconcile_requests(much_later)
        assert store.get_request(dead.request_id)["status"] == "failed"
        assert "stopped responding" in store.get_request(dead.request_id)["note"]
        assert store.get_request(fresh)["status"] == "queued"
        assert store.active_request(much_later) is None
