from __future__ import annotations

import pytest
from test_core_fakes import FakeClock, make_state

from nibe_service_usb.config import SafetyConfig
from nibe_service_usb.core.safety import Guards


def _setup(**safety):
    clock = FakeClock(1000.0)
    state, _, _ = make_state(clock=clock)
    cfg = SafetyConfig(**safety)
    return state, Guards(cfg), clock


def _set(state, clock, **values):
    for name, value in values.items():
        state.set_sensor(name, value, clock())


def test_all_clear() -> None:
    state, guards, clock = _setup()
    _set(state, clock, bt2_flow=35.0, bt1_outdoor=0.0)
    assert guards.check(state) is None


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"bt2_flow": 19.9, "bt1_outdoor": 0.0}, "safety_supply_temp"),
        ({"bt2_flow": 20.0, "bt1_outdoor": 0.0}, None),  # equal to the limit is fine
        ({"bt2_flow": 35.0, "bt1_outdoor": -15.1}, "safety_outdoor_temp"),
        ({"bt2_flow": 10.0, "bt1_outdoor": -20.0}, "safety_supply_temp"),  # supply wins over outdoor
    ],
)
def test_default_limits(values, expected) -> None:
    state, guards, clock = _setup()
    _set(state, clock, **values)
    assert guards.check(state) == expected


def test_room_guard_is_inactive_without_limit_and_first_when_set() -> None:
    state, guards, clock = _setup()
    _set(state, clock, bt2_flow=35.0, bt1_outdoor=0.0, bt50_room=5.0)
    assert guards.check(state) is None
    state, guards, clock = _setup(min_room_temp=18.0)
    _set(state, clock, bt2_flow=10.0, bt1_outdoor=0.0, bt50_room=17.0)
    assert guards.check(state) == "safety_room_temp"
    assert "bt50_room" in guards.active_sensors()


def test_hysteresis_keeps_guard_tripped_until_limit_plus_hysteresis() -> None:
    state, guards, clock = _setup(min_supply_temp=20.0, hysteresis=1.0)
    _set(state, clock, bt1_outdoor=0.0, bt2_flow=19.0)
    assert guards.check(state) == "safety_supply_temp"
    for value in (20.0, 20.5, 21.0):  # not above limit + hysteresis yet
        _set(state, clock, bt2_flow=value)
        assert guards.check(state) == "safety_supply_temp"
    _set(state, clock, bt2_flow=21.1)
    assert guards.check(state) is None
    _set(state, clock, bt2_flow=20.5)  # untripped again: only below the limit trips
    assert guards.check(state) is None
    _set(state, clock, bt2_flow=19.9)
    assert guards.check(state) == "safety_supply_temp"


def test_stale_and_missing_data() -> None:
    state, guards, clock = _setup(max_sensor_age=900.0)
    assert guards.check(state) == "stale_data"  # never read
    _set(state, clock, bt2_flow=35.0, bt1_outdoor=0.0)
    assert guards.check(state) is None
    clock.advance(901)
    assert guards.check(state) == "stale_data"
    _set(state, clock, bt2_flow=35.0, bt1_outdoor=None)  # open sensor
    assert guards.check(state) == "stale_data"


def test_tripped_guard_beats_stale_and_survives_it() -> None:
    state, guards, clock = _setup()
    _set(state, clock, bt2_flow=10.0, bt1_outdoor=0.0)
    assert guards.check(state) == "safety_supply_temp"
    clock.advance(2000)
    assert guards.check(state) == "safety_supply_temp"  # still tripped, data now stale


def test_disabled_limit_ignores_staleness() -> None:
    state, guards, clock = _setup(min_supply_temp=None, min_outdoor_temp=None)
    assert guards.check(state) is None
    assert guards.active_sensors() == []
