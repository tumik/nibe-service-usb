from __future__ import annotations

import asyncio

from test_core_fakes import FakeClock, make_config, make_state, wait_for

from nibe_service_usb.core.watchdog import Watchdog


def make(tmp_path, on_timeout=None):
    config = make_config(tmp_path)  # watchdog.timeout = 300
    mono = FakeClock(0.0)
    wall = FakeClock(1_760_000_000.0)
    state, bus, rec = make_state()
    calls: list[int] = []

    async def default_timeout() -> None:
        calls.append(1)

    dog = Watchdog(config, state, on_timeout or default_timeout, clock=mono, wall=wall)
    return dog, state, rec, mono, wall, calls


async def test_timeout_reverts_once_and_publishes_status(tmp_path) -> None:
    dog, state, rec, mono, wall, calls = make(tmp_path)
    assert state.watchdog.timeout == 300
    dog.heartbeat()
    mono.advance(300)
    await dog.check()
    assert calls == [] and state.watchdog.ha_alive is True  # exactly the timeout: not yet
    mono.advance(1)
    wall.advance(301)
    await dog.check()
    assert calls == [1]
    assert state.watchdog.ha_alive is False
    assert state.watchdog.reverted_at == wall()
    assert rec.of("status_changed")[-1]["watchdog"]["ha_alive"] is False
    await dog.check()  # still silent: no second revert
    mono.advance(1000)
    await dog.check()
    assert calls == [1]


async def test_heartbeat_keeps_it_alive(tmp_path) -> None:
    dog, state, rec, mono, wall, calls = make(tmp_path)
    for _ in range(5):
        mono.advance(200)
        wall.advance(200)
        dog.heartbeat()
        await dog.check()
    assert calls == []
    assert state.watchdog.ha_alive is True
    assert state.watchdog.last_heartbeat == wall()


async def test_recovery_marks_alive_without_restoring_anything(tmp_path) -> None:
    dog, state, rec, mono, wall, calls = make(tmp_path)
    dog.heartbeat()
    mono.advance(400)
    await dog.check()
    assert state.watchdog.ha_alive is False
    reverted_at = state.watchdog.reverted_at
    rec.clear()
    dog.heartbeat()
    assert state.watchdog.ha_alive is True
    await asyncio.sleep(0)  # let the status task run
    await wait_for(lambda: rec.of("status_changed"))
    assert rec.of("status_changed")[0]["watchdog"]["ha_alive"] is True
    assert state.watchdog.reverted_at == reverted_at  # history kept
    assert calls == [1]  # nothing re-applied
    # and the next silence reverts again
    mono.advance(301)
    await dog.check()
    assert calls == [1, 1]


async def test_failing_callback_does_not_break_the_watchdog(tmp_path) -> None:
    async def boom() -> None:
        raise RuntimeError("revert failed")

    dog, state, rec, mono, *_ = make(tmp_path, boom)
    dog.heartbeat()
    mono.advance(301)
    await dog.check()
    assert state.watchdog.ha_alive is False
    assert rec.of("status_changed")


async def test_no_revert_before_first_heartbeat(tmp_path) -> None:
    """A server that starts without any HA connection must never revert."""
    dog, state, rec, mono, wall, calls = make(tmp_path)
    assert state.watchdog.ha_alive is False
    mono.advance(10_000)
    await dog.check()
    assert calls == [] and state.watchdog.reverted_at is None
    dog.heartbeat()
    assert state.watchdog.ha_alive is True
    mono.advance(301)
    await dog.check()
    assert calls == [1]


async def test_background_task_checks_periodically_and_stops(tmp_path) -> None:
    config = make_config(tmp_path)
    mono = FakeClock(0.0)
    state, bus, rec = make_state()
    calls: list[int] = []

    async def on_timeout() -> None:
        calls.append(1)

    dog = Watchdog(config, state, on_timeout, clock=mono, check_interval=0.01)
    await dog.start()
    try:
        dog.heartbeat()
        await asyncio.sleep(0.05)
        assert calls == []
        mono.advance(500)
        await wait_for(lambda: calls == [1])
    finally:
        await dog.stop()
    assert dog._task is None
