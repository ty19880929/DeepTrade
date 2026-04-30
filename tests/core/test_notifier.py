"""Tests for the IM notification orchestration layer (DESIGN §18.5).

These tests use in-process fake ChannelPlugin objects (no real plugin install)
to exercise NoopNotifier / MultiplexNotifier / AsyncDispatchNotifier directly.
End-to-end install + push of the stdout reference channel is covered in
``tests/core/test_plugin_manager.py`` style tests for the channel.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from deeptrade.core.notifier import (
    AsyncDispatchNotifier,
    MultiplexNotifier,
    NoopNotifier,
)
from deeptrade.core.run_status import RunStatus
from deeptrade.plugins_api.notify import NotificationPayload

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(run_id: str = "r") -> NotificationPayload:
    return NotificationPayload(
        plugin_id="p", run_id=run_id, status=RunStatus.SUCCESS, title="t", summary="s"
    )


@dataclass
class _FakeMeta:
    plugin_id: str = "fake"


class _RecordingChannel:
    """Captures push() invocations into ``received``."""

    def __init__(self, plugin_id: str = "rec", *, sleep: float = 0.0, raises: bool = False) -> None:
        self.metadata = _FakeMeta(plugin_id=plugin_id)
        self.received: list[NotificationPayload] = []
        self._sleep = sleep
        self._raises = raises

    def push(self, ctx: Any, payload: NotificationPayload) -> None:  # noqa: ARG002
        if self._sleep > 0:
            time.sleep(self._sleep)
        if self._raises:
            raise RuntimeError("boom")
        self.received.append(payload)


# ---------------------------------------------------------------------------
# NoopNotifier
# ---------------------------------------------------------------------------


def test_noop_notifier_disabled_and_swallows_push() -> None:
    n = NoopNotifier()
    assert n.is_enabled() is False
    n.push(_payload())  # must not raise
    n.join(timeout=0.1)  # must not raise


# ---------------------------------------------------------------------------
# MultiplexNotifier — fan-out + per-channel exception isolation
# ---------------------------------------------------------------------------


def test_multiplex_fans_out_to_all_channels() -> None:
    a, b = _RecordingChannel("a"), _RecordingChannel("b")
    mux = MultiplexNotifier([(a, None), (b, None)])
    assert mux.is_enabled() is True
    mux.push(_payload("x"))
    assert len(a.received) == 1 and a.received[0].run_id == "x"
    assert len(b.received) == 1


def test_multiplex_isolates_failing_channel() -> None:
    """One channel raising must NOT prevent another from receiving the payload."""
    bad = _RecordingChannel("bad", raises=True)
    good = _RecordingChannel("good")
    mux = MultiplexNotifier([(bad, None), (good, None)])
    mux.push(_payload("y"))  # bad raises internally, mux swallows
    assert good.received and good.received[0].run_id == "y"


def test_multiplex_empty_channels_disabled() -> None:
    mux = MultiplexNotifier([])
    assert mux.is_enabled() is False


# ---------------------------------------------------------------------------
# AsyncDispatchNotifier — async semantics
# ---------------------------------------------------------------------------


def test_async_push_returns_immediately_and_drains_on_join() -> None:
    """push() must NOT block on inner.push; join() drains the queue."""
    sink = _RecordingChannel("sink", sleep=0.05)
    async_notifier = AsyncDispatchNotifier(MultiplexNotifier([(sink, None)]))
    t0 = time.perf_counter()
    async_notifier.push(_payload("a"))
    async_notifier.push(_payload("b"))
    async_notifier.push(_payload("c"))
    elapsed = time.perf_counter() - t0
    # Three pushes with a 50ms-per-push sink: if it were synchronous we'd be
    # at >150ms. The async push must finish in well under one sink-sleep.
    assert elapsed < 0.05, f"async push appears to be blocking: {elapsed:.3f}s"
    async_notifier.join(timeout=2.0)
    run_ids = {p.run_id for p in sink.received}
    assert run_ids == {"a", "b", "c"}
    assert async_notifier.dispatched_count == 3


def test_async_disabled_when_inner_disabled() -> None:
    n = AsyncDispatchNotifier(NoopNotifier())
    assert n.is_enabled() is False
    n.push(_payload())  # no-op, no thread spawned
    n.join(timeout=0.1)  # must not block


def test_async_queue_overflow_drops_payload() -> None:
    """Queue full → put_nowait raises, AsyncDispatchNotifier counts as dropped."""
    # Channel that holds the worker hostage so the queue can overflow.
    blocking = threading.Event()
    holding = threading.Event()

    class _Hold:
        metadata = _FakeMeta("hold")

        def push(self, ctx: Any, p: NotificationPayload) -> None:  # noqa: ARG002
            holding.set()
            blocking.wait(timeout=5.0)

    n = AsyncDispatchNotifier(
        MultiplexNotifier([(_Hold(), None)]), queue_size=2
    )
    n.push(_payload("first"))   # consumed by worker, blocks on Event
    holding.wait(timeout=2.0)    # ensure worker has picked up "first"
    n.push(_payload("q1"))       # → queue
    n.push(_payload("q2"))       # → queue (now full, size=2)
    n.push(_payload("drop"))     # queue.Full → dropped
    assert n.dropped_count == 1
    blocking.set()               # release worker so test can clean up
    n.join(timeout=3.0)


def test_async_worker_survives_inner_push_exception() -> None:
    """If inner.push raises, the worker keeps running for subsequent payloads."""
    raises_once = {"count": 0}

    class _OneShot:
        metadata = _FakeMeta("flake")

        def push(self, ctx: Any, p: NotificationPayload) -> None:  # noqa: ARG002
            raises_once["count"] += 1
            if raises_once["count"] == 1:
                raise RuntimeError("first call boom")
            # subsequent calls: silent success

    n = AsyncDispatchNotifier(MultiplexNotifier([(_OneShot(), None)]))
    n.push(_payload("a"))
    n.push(_payload("b"))
    n.push(_payload("c"))
    n.join(timeout=2.0)
    # Even though one channel raised on the first call, MultiplexNotifier
    # isolates it — so the worker treats every iteration as a successful
    # outer-push (the inner exception is logged inside Multiplex).
    assert n.dispatched_count == 3


def test_async_join_idempotent_before_first_push() -> None:
    n = AsyncDispatchNotifier(MultiplexNotifier([(_RecordingChannel(), None)]))
    n.join(timeout=0.1)  # never started worker — must be a no-op
    n.join(timeout=0.1)  # second call — still a no-op


def test_async_join_waits_for_in_flight_payload() -> None:
    """A payload mid-flight when join() is called must complete (or timeout)."""
    sink = _RecordingChannel("sink", sleep=0.2)
    n = AsyncDispatchNotifier(MultiplexNotifier([(sink, None)]))
    n.push(_payload("only"))
    n.join(timeout=2.0)
    assert sink.received and sink.received[0].run_id == "only"
