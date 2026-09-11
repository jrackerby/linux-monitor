"""The read-only sensors.

Nothing here parses -- coordinator._metrics has already turned KEY=value text
into typed readings or None by the time a value_fn runs. What these tests
protect is that None SURVIVES: a value_fn that turns an unread metric into 0
publishes a confident number nobody measured, which is the defect this
integration was written to refuse.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.linux_monitor.sensor import (
    SENSORS,
    LinuxMonitorRebootCount,
    LinuxMonitorSensor,
    _apt_age_hours,
    _fs_root,
)

DATA = {
    "offline_expected": False,
    "online": True,
    "metrics": {
        "cpu_percent": 50.0,
        "mem_percent": 25.0,
        "temp_c": 45.1,
        "temp_source": "coretemp",
        "load1": 0.15,
        "load5": 0.2,
        "load15": 0.25,
        "cores": 4,
        "kernel": "6.12.101+deb13-amd64",
        "fs": [
            {
                "device_name": "/dev/sda1",
                "fs_type": "ext4",
                "mnt_point": "/",
                "size": 100000,
                "used": 40000,
                "free": 60000,
                "percent": 40.0,
            }
        ],
    },
    "slow": {
        "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
        "UPGRADABLE": "4",
        "SECURITY": "2",
        "SECURITY_PKGS": "libssl3,curl",
    },
}

BY_KEY = {d.key: d for d in SENSORS}


@pytest.fixture
def sensor(coordinator_factory):
    def _make(key, data=None):
        c = coordinator_factory()
        c.data = data if data is not None else DATA
        return LinuxMonitorSensor(c, BY_KEY[key])

    return _make


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("cpu", 50.0),
        ("memory", 25.0),
        ("temperature", 45.1),
        ("disk", 40.0),
        ("load_1m", 0.15),
        ("kernel_running", "6.12.101+deb13-amd64"),
        ("kernel_installed", "6.12.101+deb13-amd64"),
        ("updates_pending", 4),
        ("security_updates_pending", 2),
    ],
)
def test_each_reading(sensor, key, expected) -> None:
    assert sensor(key).native_value == expected


@pytest.mark.parametrize("key", sorted(BY_KEY))
def test_every_reading_is_none_when_the_host_said_nothing(sensor, key) -> None:
    """THE CONTRACT, asserted over every sensor rather than a chosen few --
    one value_fn quietly defaulting to 0 is exactly what this would catch."""
    empty = {"offline_expected": False, "online": False, "metrics": {}, "slow": {}}
    assert sensor(key, empty).native_value is None


def test_an_individual_reading_goes_unavailable_with_its_source(sensor) -> None:
    """Unlike health and the reboot counter, these SHOULD disappear: a CPU
    percentage from a host that did not answer would be a stale number
    presented as current."""
    entity = sensor("cpu", {"online": False, "metrics": {}, "slow": {}})
    assert entity.available is False


def test_the_attributes_that_name_what_a_number_is_about(sensor) -> None:
    assert sensor("temperature").extra_state_attributes == {"source": "coretemp"}
    disk = sensor("disk").extra_state_attributes
    assert disk["mnt_point"] == "/" and disk["size"] == 100000
    assert "percent" not in disk, "the state must not be repeated as an attribute"
    load = sensor("load_1m").extra_state_attributes
    assert load == {"min5": 0.2, "min15": 0.25, "cores": 4}
    assert sensor("security_updates_pending").extra_state_attributes == {
        "packages": "libssl3,curl"
    }


def test_uptime_reports_a_boot_instant(sensor) -> None:
    """A boot INSTANT, not a duration string that has to be regex'd back."""
    from datetime import datetime

    data = {**DATA, "metrics": {**DATA["metrics"], "boot_time": datetime(2026, 9, 1)}}
    assert sensor("uptime", data).native_value == datetime(2026, 9, 1)


# --- the helpers ------------------------------------------------------------


def test_fs_root_prefers_the_root_filesystem() -> None:
    data = {"metrics": {"fs": [{"mnt_point": "/boot"}, {"mnt_point": "/"}]}}
    assert _fs_root(data)["mnt_point"] == "/"


def test_fs_root_falls_back_to_the_first_row() -> None:
    """A host with no `/` is odd rather than impossible, and reporting its
    first real filesystem beats reporting nothing."""
    data = {"metrics": {"fs": [{"mnt_point": "/srv"}]}}
    assert _fs_root(data)["mnt_point"] == "/srv"


def test_fs_root_is_none_when_nothing_was_read() -> None:
    assert _fs_root({"metrics": {"fs": []}}) is None
    assert _fs_root({"metrics": {}}) is None
    assert _fs_root({"metrics": {"fs": "not a list"}}) is None


def test_apt_age_is_none_when_the_host_did_not_answer() -> None:
    """A zero computed from month-old lists is not a zero, it is an
    unanswered question."""
    assert _apt_age_hours({"slow": {}}) is None
    assert _apt_age_hours({"slow": {"APT_LISTS_MTIME": ""}}) is None
    assert _apt_age_hours({"slow": {"APT_LISTS_MTIME": "not a number"}}) is None


def test_apt_age_is_hours_since_the_mtime() -> None:
    from homeassistant.util import dt as dt_util

    two_hours_ago = int(dt_util.utcnow().timestamp()) - 7200
    age = _apt_age_hours({"slow": {"APT_LISTS_MTIME": str(two_hours_ago)}})
    assert age == pytest.approx(2.0, abs=0.1)


# --- the reboot counter -----------------------------------------------------


def test_the_reboot_counter_never_goes_unavailable(coordinator_factory) -> None:
    """A monitor that goes unavailable exactly when its subject reboots cannot
    report the reboot."""
    c = coordinator_factory()
    c.data = {"online": False, "metrics": {}, "slow": {}}
    assert LinuxMonitorRebootCount(c).available is True


def test_the_reboot_counter_reads_the_coordinator(coordinator_factory) -> None:
    c = coordinator_factory()
    c.data = DATA
    c.reboot_count = 3
    assert LinuxMonitorRebootCount(c).native_value == 3


async def test_the_count_survives_a_home_assistant_restart(hass, mounted) -> None:
    """An HA restart must not read as the crash-loop having stopped."""
    mock_restore_cache(hass, (State("sensor.testhost_reboot_count", "7"),))
    entry = await mounted()
    assert hass.states.get("sensor.testhost_reboot_count").state == "7"
    assert entry.runtime_data.reboot_count == 7


@pytest.mark.parametrize("restored", ["unknown", STATE_UNAVAILABLE, "not a number"])
async def test_an_unusable_restored_value_is_ignored(hass, mounted, restored) -> None:
    """Rather than crashing the platform or resetting the count to something
    invented."""
    mock_restore_cache(hass, (State("sensor.testhost_reboot_count", restored),))
    entry = await mounted()
    assert entry.runtime_data.reboot_count == 0
