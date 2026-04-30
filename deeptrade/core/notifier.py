"""Notification orchestration — pure framework code, NO IM-protocol logic.

Three layers compose top-down:

    AsyncDispatchNotifier      ← async wrapper: queue + daemon worker + join
        └─ MultiplexNotifier   ← fan-out + per-channel exception isolation
              └─ ChannelPlugin instances (loaded from type=channel plugins)

The framework knows nothing about feishu / dingtalk / wechat-work. Channels
are delivered as plugins (``channels_builtin/`` mirrors ``strategies_builtin/``);
new channels = new plugin packages, zero framework change.

Top-level API (used by any plugin that needs to notify):

    from deeptrade import notify, notification_session

    notify(db, payload)                       # one-shot
    with notification_session(db) as ns:      # batch
        ns.push(p1)
        ns.push(p2)

If no channel plugins are enabled, both forms degrade to no-op silently.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.db import Database
    from deeptrade.core.plugin_manager import InstalledPlugin, PluginManager
    from deeptrade.plugins_api.channel import ChannelPlugin, PluginContext
    from deeptrade.plugins_api.notify import NotificationPayload

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Notifier(Protocol):
    """Handle exposed to callers (plugins or framework code).

    Implementations MUST guarantee ``push`` returns quickly (no synchronous
    HTTP) — the asynchrony is provided by ``AsyncDispatchNotifier``.
    """

    def is_enabled(self) -> bool: ...
    def push(self, payload: NotificationPayload) -> None: ...
    def join(self, timeout: float = 0.0) -> None: ...


# ---------------------------------------------------------------------------
# NoopNotifier — used when no channel plugin is installed/enabled
# ---------------------------------------------------------------------------


class NoopNotifier:
    """Returned by ``build_notifier`` when there are no enabled channels."""

    def is_enabled(self) -> bool:
        return False

    def push(self, payload: NotificationPayload) -> None:  # noqa: ARG002
        return

    def join(self, timeout: float = 0.0) -> None:  # noqa: ARG002
        return


# ---------------------------------------------------------------------------
# MultiplexNotifier — fan-out to all enabled channel plugins
# ---------------------------------------------------------------------------


class MultiplexNotifier:
    """Synchronous fan-out across one ChannelPlugin per channel.

    Per-channel ``push`` is wrapped in ``try``/``except``: a broken/slow
    channel never blocks or breaks the others. Consumed by
    ``AsyncDispatchNotifier`` on a background thread, so blocking on HTTP
    here is fine.
    """

    def __init__(self, channels: Sequence[tuple[ChannelPlugin, PluginContext]]) -> None:
        self._channels: list[tuple[ChannelPlugin, PluginContext]] = list(channels)

    def is_enabled(self) -> bool:
        return bool(self._channels)

    def push(self, payload: NotificationPayload) -> None:
        for ch, ctx in self._channels:
            try:
                ch.push(ctx, payload)
            except Exception as e:  # noqa: BLE001 — single-channel isolation
                pid = getattr(getattr(ch, "metadata", None), "plugin_id", "?")
                logger.warning("channel %s push failed: %s", pid, e)

    def join(self, timeout: float = 0.0) -> None:  # noqa: ARG002
        return


# ---------------------------------------------------------------------------
# AsyncDispatchNotifier — wraps a synchronous Notifier in a worker thread
# ---------------------------------------------------------------------------


class _Shutdown:
    """Sentinel value placed on the queue to signal worker shutdown."""


_SHUTDOWN = _Shutdown()


class AsyncDispatchNotifier:
    """Non-blocking adapter: ``push()`` enqueues and returns immediately;
    a daemon worker thread drains the queue and calls the inner notifier.

    Invariants:
        * ``push`` MUST NOT block on HTTP. ``put_nowait`` raises Queue.Full
          if the queue is saturated → drop + warn; never block the caller.
        * ``join(timeout)`` MUST be called before process exit to flush
          in-flight payloads (worker is daemon, would be killed otherwise).
        * Worker exceptions are caught — a broken inner notifier never kills
          the worker thread.
    """

    DEFAULT_QUEUE_SIZE = 16
    DEFAULT_JOIN_TIMEOUT = 10.0  # seconds

    def __init__(
        self,
        inner: Notifier,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._inner = inner
        self._queue: queue.Queue[NotificationPayload | _Shutdown] = queue.Queue(queue_size)
        self._dispatched_count = 0
        self._dropped_count = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._worker, name="deeptrade-notify", daemon=True
        )
        self._started = False

    def is_enabled(self) -> bool:
        return self._inner.is_enabled()

    def push(self, payload: NotificationPayload) -> None:
        if not self.is_enabled():
            return
        if not self._started:
            self._started = True
            self._thread.start()
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            with self._lock:
                self._dropped_count += 1
            logger.warning(
                "notify queue full (size=%d); dropping payload run_id=%s",
                self._queue.maxsize,
                payload.run_id,
            )

    def join(self, timeout: float = DEFAULT_JOIN_TIMEOUT) -> None:
        """Block until the queue drains or ``timeout`` seconds elapse.
        Always call this once before process exit. Idempotent."""
        if not self._started:
            return
        try:
            self._queue.put(_SHUTDOWN, timeout=max(0.1, timeout))
        except queue.Full:  # pragma: no cover
            logger.warning("notify queue full while signaling shutdown")
            return
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "notify worker did not finish within %.1fs; in-flight payload may be lost",
                timeout,
            )

    @property
    def dispatched_count(self) -> int:
        with self._lock:
            return self._dispatched_count

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if isinstance(item, _Shutdown):
                return
            try:
                self._inner.push(item)
            except Exception as e:  # noqa: BLE001 — keep worker alive
                logger.warning("notify worker caught inner.push exception: %s", e)
            else:
                with self._lock:
                    self._dispatched_count += 1


# ---------------------------------------------------------------------------
# Discovery + assembly
# ---------------------------------------------------------------------------


def build_notifier(db: Database, plugin_manager: PluginManager) -> Notifier:
    """Discover all enabled ``type=channel`` plugins and assemble a Notifier.

    Returns a ``NoopNotifier`` if no channels are enabled (zero-cost path).
    Returns an ``AsyncDispatchNotifier`` wrapping a ``MultiplexNotifier``
    otherwise. Channel plugin entrypoint load failures are logged and the
    affected channel is skipped — one bad channel never breaks the others.
    """
    from deeptrade.core.config import ConfigService  # avoid circular import
    from deeptrade.core.plugin_manager import _load_entrypoint
    from deeptrade.plugins_api.channel import PluginContext

    channel_records: list[InstalledPlugin] = [
        r for r in plugin_manager.list_all() if r.type == "channel" and r.enabled
    ]
    if not channel_records:
        return NoopNotifier()

    pairs: list[tuple[ChannelPlugin, PluginContext]] = []
    for rec in channel_records:
        try:
            instance = _load_entrypoint(Path(rec.install_path), rec.entrypoint, rec.metadata)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to load channel plugin %s: %s", rec.plugin_id, e)
            continue
        ctx = PluginContext(db=db, config=ConfigService(db), plugin_id=rec.plugin_id)
        pairs.append((instance, ctx))

    if not pairs:
        return NoopNotifier()
    return AsyncDispatchNotifier(MultiplexNotifier(pairs))


# ---------------------------------------------------------------------------
# Top-level user-facing API: notify(...) / notification_session(...)
# ---------------------------------------------------------------------------


def notify(db: Database, payload: NotificationPayload, *, timeout: float = 10.0) -> None:
    """Push a single ``NotificationPayload`` through all enabled channel plugins.

    Convenience one-shot: builds a notifier from the current plugin registry,
    pushes, then joins (waits for in-flight delivery up to ``timeout``).

    Silently no-op if no channel plugins are enabled. Per-channel failures
    are isolated and logged (never raised).

    For repeated calls in the same process (e.g. multiple payloads in one
    plugin run), prefer :func:`notification_session` to avoid rebuilding the
    notifier on every call.
    """
    from deeptrade.core.plugin_manager import PluginManager

    notifier = build_notifier(db, PluginManager(db))
    try:
        notifier.push(payload)
    finally:
        notifier.join(timeout=timeout)


@contextmanager
def notification_session(db: Database, *, timeout: float = 10.0) -> Iterator[Notifier]:
    """Context manager that yields a ``Notifier`` for batch push and joins on exit.

    Use this when a plugin will push multiple payloads in one run — the
    notifier (and its worker thread) is built once and reused.

    Example:
        with notification_session(db) as ns:
            ns.push(payload_a)
            ns.push(payload_b)
        # join + cleanup happen automatically here
    """
    from deeptrade.core.plugin_manager import PluginManager

    notifier = build_notifier(db, PluginManager(db))
    try:
        yield notifier
    finally:
        notifier.join(timeout=timeout)
