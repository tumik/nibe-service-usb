from __future__ import annotations

import logging

import pytest

from nibe_service_usb.core.errors import CommandError
from nibe_service_usb.core.events import EventBus


async def test_sync_and_async_callbacks_in_order() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def first(topic: str, data: dict) -> None:
        calls.append(f"a:{topic}:{data['x']}")

    def second(topic: str, data: dict) -> None:
        calls.append(f"s:{topic}:{data['x']}")

    bus.subscribe("t", first)
    bus.subscribe("t", second)
    await bus.publish("t", {"x": 1})
    assert calls == ["a:t:1", "s:t:1"]


async def test_other_topic_not_called() -> None:
    bus = EventBus()
    calls: list[str] = []
    bus.subscribe("a", lambda t, d: calls.append(t))
    await bus.publish("b", {})
    assert calls == []


async def test_unsubscribe() -> None:
    bus = EventBus()
    calls: list[str] = []
    unsub = bus.subscribe("a", lambda t, d: calls.append(t))
    await bus.publish("a", {})
    unsub()
    unsub()  # idempotent
    await bus.publish("a", {})
    assert calls == ["a"]


async def test_exceptions_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    calls: list[str] = []

    def bad_sync(topic: str, data: dict) -> None:
        raise RuntimeError("boom sync")

    async def bad_async(topic: str, data: dict) -> None:
        raise RuntimeError("boom async")

    bus.subscribe("a", bad_sync)
    bus.subscribe("a", bad_async)
    bus.subscribe("a", lambda t, d: calls.append("ok"))
    with caplog.at_level(logging.ERROR):
        await bus.publish("a", {})
    assert calls == ["ok"]
    assert "boom sync" in caplog.text
    assert "boom async" in caplog.text


async def test_async_callbacks_are_sequential() -> None:
    import asyncio

    bus = EventBus()
    order: list[str] = []

    async def slow(topic: str, data: dict) -> None:
        order.append("slow-start")
        await asyncio.sleep(0.01)
        order.append("slow-end")

    async def fast(topic: str, data: dict) -> None:
        order.append("fast")

    bus.subscribe("a", slow)
    bus.subscribe("a", fast)
    await bus.publish("a", {})
    assert order == ["slow-start", "slow-end", "fast"]


async def test_wildcard_subscriber() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("*", lambda t, d: seen.append(t))
    await bus.publish("a", {})
    await bus.publish("b", {})
    assert seen == ["a", "b"]


def test_command_error() -> None:
    err = CommandError("rate_limited", "slow down")
    assert err.code == "rate_limited"
    assert err.message == "slow down"
    assert "rate_limited" in str(err)
    assert CommandError("x").message == ""
