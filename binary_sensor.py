"""Health binary_sensor.

Never goes unavailable -- see entity.py's docstring on why: a health sensor
that disappears with the host cannot report the host being down, and its
attributes would vanish with it.

Two rung classes, same shape as kiosk_pi: an ABSENT reading (ssh unreachable)
DWELLS past TRANSPORT_FAIL_DWELL consecutive misses; KNOWN-BAD readings
(disk/cpu over threshold) trip immediately -- the host answered and the
answer was wrong, nothing to wait for.

GH-470, ONE TRANSPORT NOW. The Glances daemon is gone, so glances_fails, the
min(glances_fails, ssh_fails) floor that used to require BOTH transports to
have failed before calling a host unreachable, and the glances_ok /
glances_missed_polls attributes are all removed rather than left publishing
a constant. With a single transport `online` and `ssh_ok` are the same fact,
which is why there is no longer a separate ssh_unreachable rung underneath an
online host: a host that is online answered ssh, so its ssh streak is zero
and that rung could never fire. Deleted rather than left as dead code.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LinuxMonitorConfigEntry
from .const import CPU_PROBLEM_PCT, DISK_PROBLEM_PCT, TRANSPORT_FAIL_DWELL
from .entity import LinuxMonitorEntity

# Every entity in this integration is coordinator-driven: none implements
# async_update, so there is no per-entity poll for this to throttle and 0 is
# the correct declaration. Undeclared is not the same as zero -- it says
# nothing, which is what jrackerby/linux-monitor#12 was about.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LinuxMonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([LinuxMonitorHealth(entry.runtime_data)])


class LinuxMonitorHealth(LinuxMonitorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_name = "Health"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "health")

    @property
    def available(self) -> bool:
        return True

    def _reasons(self) -> list[str]:
        data = self.coordinator.data or {}

        # A host that is meant to be off scores clean, never red.
        if data.get("offline_expected"):
            return []

        s_fails = int(data.get("ssh_fails") or 0)
        metrics = data.get("metrics") or {}

        if not data.get("online"):
            # AN AUTH REFUSAL IS A KNOWN-BAD READING, NOT AN ABSENT ONE, so it
            # belongs in the rung class that trips immediately: the host was
            # reached and it refused this entry's credential. The dwell below
            # exists because an absent reading might be a flap that heals
            # itself -- a revoked key does not heal itself, and waiting the
            # extra poll only delays the one message that names the cause.
            if data.get("auth_failed"):
                return [f"ssh_auth_failed:{s_fails}_polls"]
            # Below the dwell this is a flap, not a fault -- three minutes is
            # longer than a routine reboot or one transient ssh timeout.
            if s_fails >= TRANSPORT_FAIL_DWELL:
                return [f"unreachable:{s_fails}_polls"]
            return []

        reasons: list[str] = []

        # EVERY real filesystem, not just /. findmnt --real gives / and the
        # boot partition on these hosts; Glances used to return / four times
        # over and never mentioned /boot at all.
        rows = metrics.get("fs")
        if isinstance(rows, list):
            for entry in rows:
                pct = entry.get("percent")
                if isinstance(pct, (int, float)) and pct > DISK_PROBLEM_PCT:
                    reasons.append(f"disk:{entry.get('mnt_point')}:{pct}")

        cpu = metrics.get("cpu_percent")
        if isinstance(cpu, (int, float)) and cpu > CPU_PROBLEM_PCT:
            reasons.append(f"cpu:{cpu}")

        return reasons

    @property
    def is_on(self) -> bool:
        return bool(self._reasons())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        reasons = self._reasons()
        s_fails = int(data.get("ssh_fails") or 0)

        if data.get("offline_expected"):
            disposition = "offline_expected"
        elif data.get("auth_failed"):
            # Named separately from "unreachable" on purpose: the two send an
            # operator to different places, and reading one as the other is
            # the whole defect #13 was filed for.
            disposition = "auth_failed"
        elif reasons:
            disposition = "unreachable" if not data.get("online") else "problem"
        elif s_fails:
            disposition = "transport_flap"
        else:
            disposition = "ok"

        return {
            "disposition": disposition,
            "reasons": reasons,
            "offline_expected": bool(data.get("offline_expected")),
            "ssh_ok": bool(data.get("ssh_ok")),
            "ssh_missed_polls": s_fails,
            # True once ssh itself has said permission-denied on
            # SSH_AUTH_FAIL_DWELL consecutive polls. A reauth card is in front
            # of the operator by the time this is set.
            "auth_failed": bool(data.get("auth_failed")),
            "transport_fail_dwell": TRANSPORT_FAIL_DWELL,
            "host": self.coordinator.host,
        }
