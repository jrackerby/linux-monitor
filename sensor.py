"""Read-only sensors for a generic monitored host.

Every value_fn reads coordinator.data["metrics"], which coordinator._metrics()
already turned from KEY=value text into typed readings or None. Nothing here
parses; a parse failure has already landed on None by the time it arrives.

KEYS ARE FROZEN. `key` feeds entity.py's unique_id (<hostname>_<key>) and
through it every entity_id in the registry, so the transport swap changed
where each value comes FROM and not one of the names below.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

# Every entity in this integration is coordinator-driven: none implements
# async_update, so there is no per-entity poll for this to throttle and 0 is
# the correct declaration. Undeclared is not the same as zero -- it says
# nothing, which is what jrackerby/linux-monitor#12 was about.
PARALLEL_UPDATES = 0

from . import LinuxMonitorConfigEntry
from .entity import LinuxMonitorEntity


def _m(data: dict[str, Any], key: str) -> Any:
    return (data.get("metrics") or {}).get(key)


def _fs_root(data: dict[str, Any]) -> dict[str, Any] | None:
    """The root filesystem, or the first one if this host somehow has no `/`.
    findmnt --real no longer returns the three bind-mount duplicates of / that
    Glances did, so this is now a search over two or three real rows."""
    rows = _m(data, "fs")
    if not isinstance(rows, list) or not rows:
        return None
    for entry in rows:
        if entry.get("mnt_point") == "/":
            return entry
    return rows[0]


def _int_or_none(raw: Any) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _apt_age_hours(data: dict[str, Any]) -> float | None:
    """A zero computed from month-old lists is not a zero, it is an
    unanswered question."""
    mtime = _int_or_none((data.get("slow") or {}).get("APT_LISTS_MTIME"))
    if mtime is None:
        return None
    delta = dt_util.utcnow().timestamp() - mtime
    return round(delta / 3600.0, 1)


@dataclass(frozen=True, kw_only=True)
class LinuxMonitorSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[LinuxMonitorSensorDescription, ...] = (
    LinuxMonitorSensorDescription(
        key="cpu",
        name="CPU",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        # Two /proc/stat samples taken one second apart inside a single ssh
        # round trip -- const.py CPU_SAMPLE_SECS argues that choice against
        # the alternative of delta-ing across polls.
        value_fn=lambda d: _m(d, "cpu_percent"),
    ),
    LinuxMonitorSensorDescription(
        key="memory",
        name="Memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: _m(d, "mem_percent"),
    ),
    LinuxMonitorSensorDescription(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: _m(d, "temp_c"),
        # WHICH chip answered. The amd64 hosts read coretemp and the Pis read
        # cpu_thermal, and a temperature with no named source cannot be told
        # apart from one read off the wrong sensor -- the exact failure
        # const.py CPU_HWMON_NAMES exists to prevent.
        attrs_fn=lambda d: {"source": _m(d, "temp_source")},
    ),
    LinuxMonitorSensorDescription(
        key="disk",
        name="Disk",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: (_fs_root(d) or {}).get("percent"),
        attrs_fn=lambda d: {
            k: v for k, v in (_fs_root(d) or {}).items()
            if k in ("device_name", "fs_type", "mnt_point", "size", "used", "free")
        },
    ),
    LinuxMonitorSensorDescription(
        key="load_1m",
        name="Load average 1m",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        # Deliberately unitless and threshold-free -- not a CPU percentage.
        value_fn=lambda d: _m(d, "load1"),
        attrs_fn=lambda d: {
            "min5": _m(d, "load5"),
            "min15": _m(d, "load15"),
            "cores": _m(d, "cores"),
        },
    ),
    LinuxMonitorSensorDescription(
        key="uptime",
        name="Uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # A boot INSTANT computed from /proc/uptime's seconds-since-boot.
        # Glances handed over a preformatted string that had to be regex'd
        # back into a duration; that parse, and its failure mode, are gone.
        value_fn=lambda d: _m(d, "boot_time"),
    ),
    LinuxMonitorSensorDescription(
        key="kernel_running",
        name="Kernel running",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _m(d, "kernel"),
    ),
    LinuxMonitorSensorDescription(
        key="kernel_installed",
        name="Kernel installed",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("slow") or {}).get("KERNEL_INSTALLED") or None,
    ),
    LinuxMonitorSensorDescription(
        key="updates_pending",
        name="Updates pending",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _int_or_none((d.get("slow") or {}).get("UPGRADABLE")),
    ),
    LinuxMonitorSensorDescription(
        key="security_updates_pending",
        name="Security updates pending",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _int_or_none((d.get("slow") or {}).get("SECURITY")),
        attrs_fn=lambda d: {
            "packages": (d.get("slow") or {}).get("SECURITY_PKGS") or None,
        },
    ),
    LinuxMonitorSensorDescription(
        key="apt_lists_age",
        name="Apt lists age",
        native_unit_of_measurement="h",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_apt_age_hours,
    ),
)


class LinuxMonitorSensor(LinuxMonitorEntity, SensorEntity):
    entity_description: LinuxMonitorSensorDescription

    def __init__(self, coordinator, description: LinuxMonitorSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        value = self.entity_description.value_fn(data)
        if isinstance(value, datetime):
            return value
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        if fn is None:
            return None
        return fn(self.coordinator.data or {})


class LinuxMonitorRebootCount(LinuxMonitorEntity, RestoreEntity, SensorEntity):
    """Counts uptime resets, not polls -- see coordinator._async_update_data
    for why the health binary_sensor's dwell/streak misses these.
    Restored across an HA restart of its own so a crash-loop mid-diagnosis
    does not read as resolved just because HA bounced too."""

    _attr_name = "Reboot count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "reboot_count")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            restored = int(float(last_state.state))
        except ValueError:
            return
        self.coordinator.restore_reboot_count(restored)

    @property
    def available(self) -> bool:
        """Never disappears with the host -- same reasoning as the health
        binary_sensor: a monitor that goes unavailable exactly when its
        subject reboots cannot report the reboot."""
        return True

    @property
    def native_value(self) -> int:
        return self.coordinator.reboot_count


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LinuxMonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [LinuxMonitorSensor(coordinator, description) for description in SENSORS]
        + [LinuxMonitorRebootCount(coordinator)]
    )
