"""Tiny in-process publish/subscribe bus.

Topics: ``sensor_changed``, ``status_changed``, ``pump_state``, ``shutdown``.
A subscriber may also use the topic ``"*"`` to receive every topic.
Callbacks receive ``(topic, data)``.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Callback = Callable[[str, dict], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = {}

    def subscribe(self, topic: str, callback: Callback) -> Callable[[], None]:
        """Register ``callback`` for ``topic``; returns a function that unsubscribes it."""
        self._subscribers.setdefault(topic, []).append(callback)

        def unsubscribe() -> None:
            callbacks = self._subscribers.get(topic)
            if callbacks and callback in callbacks:
                callbacks.remove(callback)

        return unsubscribe

    async def publish(self, topic: str, data: dict) -> None:
        """Call subscribers in registration order, awaiting async ones one at a time.

        Exceptions raised by a callback are logged and do not reach the
        publisher or the other callbacks.
        """
        callbacks = list(self._subscribers.get(topic, ())) + list(self._subscribers.get("*", ()))
        for callback in callbacks:
            try:
                result = callback(topic, data)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Event subscriber failed for topic %r", topic)
