"""In-process event bus for streaming a run to the console.

`Executor.run` is a closed loop: it prints progress and returns one string at
the end. The only machine-readable progress signal was `graph.json` being
rewritten twice per wave, which a client could poll — coarse, and it cannot
distinguish "this wave just started" from "this wave just finished".

This module is the seam. `flow.py` takes an optional `emit` callback and calls
it at six points; the API server subscribes per session and relays to SSE.
Nothing here is required: with no subscriber, `emit` is a no-op and the CLI
behaves exactly as before.

Three properties the run loop depends on:

  - **Emitting never raises.** A console bug must not fail a node. Every
    publish path swallows its own errors.
  - **Emitting never blocks.** `put_nowait` onto unbounded per-subscriber
    queues; a slow reader grows its own queue rather than stalling the DAG.
  - **Late subscribers catch up.** Runs are started by one request and
    streamed by a second, so the first events usually land before anyone is
    listening. Each session keeps a replay buffer; a new subscriber drains it
    before receiving live events, and `seq` lets a client detect a gap.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, AsyncIterator

# Per-session replay depth. A 60-node run (flow.MAX_NODES) emits roughly
# 3 events per node plus one per wave, so this holds a whole run with room
# to spare.
REPLAY_DEPTH = 512


class _Channel:
    """One session's event history plus its live subscribers."""

    def __init__(self) -> None:
        self.buffer: deque[dict] = deque(maxlen=REPLAY_DEPTH)
        self.subscribers: set[asyncio.Queue] = set()
        self.seq = 0
        self.done = False


class EventBus:
    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}

    def _channel(self, session_id: str) -> _Channel:
        ch = self._channels.get(session_id)
        if ch is None:
            ch = self._channels[session_id] = _Channel()
        return ch

    # ── publish ──────────────────────────────────────────────────────────

    def publish(self, session_id: str, event_type: str, **payload: Any) -> None:
        """Record an event and fan it out. Safe to call from any coroutine."""
        ch = self._channel(session_id)
        ch.seq += 1
        event = {
            "seq": ch.seq,
            "type": event_type,
            "ts": time.time(),
            "session_id": session_id,
            **payload,
        }
        ch.buffer.append(event)
        if event_type in ("run_complete", "run_failed", "run_cancelled"):
            ch.done = True
        for q in list(ch.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                # A subscriber whose queue is unusable is dropped rather than
                # allowed to propagate into the run loop.
                ch.subscribers.discard(q)

    def emitter(self, session_id: str):
        """A bound `emit(type, **payload)` to hand to `Executor.run`.

        Wrapped so that nothing the console does can surface as a node
        failure — the run is the product, the stream is a view of it.
        """
        def emit(event_type: str, **payload: Any) -> None:
            try:
                self.publish(session_id, event_type, **payload)
            except Exception:
                pass
        return emit

    # ── subscribe ────────────────────────────────────────────────────────

    async def subscribe(self, session_id: str,
                        after_seq: int = 0) -> AsyncIterator[dict]:
        """Yield buffered events past `after_seq`, then live ones.

        Ends when the run reaches a terminal event. Registration happens
        before the replay is read, so an event published mid-replay is
        queued rather than lost; the seq filter then drops the duplicate.
        """
        ch = self._channel(session_id)
        q: asyncio.Queue = asyncio.Queue()
        ch.subscribers.add(q)
        try:
            last = after_seq
            for event in list(ch.buffer):
                if event["seq"] > last:
                    last = event["seq"]
                    yield event
            if ch.done and q.empty():
                return
            while True:
                event = await q.get()
                if event["seq"] <= last:
                    continue  # already replayed
                last = event["seq"]
                yield event
                if event["type"] in ("run_complete", "run_failed", "run_cancelled"):
                    return
        finally:
            ch.subscribers.discard(q)

    # ── introspection ────────────────────────────────────────────────────

    def is_live(self, session_id: str) -> bool:
        ch = self._channels.get(session_id)
        return ch is not None and not ch.done

    def history(self, session_id: str) -> list[dict]:
        ch = self._channels.get(session_id)
        return list(ch.buffer) if ch else []

    def forget(self, session_id: str) -> None:
        """Drop a finished session's channel once nobody is reading it."""
        ch = self._channels.get(session_id)
        if ch and ch.done and not ch.subscribers:
            del self._channels[session_id]


bus = EventBus()

__all__ = ["EventBus", "bus", "REPLAY_DEPTH"]
