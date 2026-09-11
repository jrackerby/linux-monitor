"""Buttons -- the privileged host controls.

ONE BUTTON, AND IT ONLY EXISTS WHEN THE ENTRY OPTED IN. Reboot needs the same
passwordless sudo the apt upgrade does, and const.py's CONF_ALLOW_INSTALL is
the single grant covering both. On an entry that has not opted in the button
is ABSENT rather than present-and-refusing: a control that is visible but
always fails trains an operator to ignore what it says.

NAMED "Reboot host", NOT "Reboot". kiosk_pi registers button.<host>_reboot
against a device carrying the same name on the four kiosk hosts, and Home
Assistant mints an entity_id from device name plus entity name -- a colliding
id is taken as _2 and never reclaimed even after the other entity goes away
(TOOLS.md). update.py's header carries the same note for the same reason.

A REBOOT CANNOT BE VERIFIED BY READ-BACK, BY CONSTRUCTION: it tears down the
transport it was issued over, so ssh cannot return cleanly and a non-zero exit
here is not evidence of failure. The proof is uptime resetting on a later poll
-- which sensor.py's reboot counter already watches for, independently of
whether the reboot was asked for from here.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LinuxMonitorConfigEntry
from .const import REBOOT_TIMEOUT
from .coordinator import REBOOT_CMD
from .entity import LinuxMonitorEntity

_LOGGER = logging.getLogger(__name__)

# Zero for the same reason as the read-only platforms: the entity is
# coordinator-driven and implements no async_update.
#
# IT DOES NOT SERIALISE THIS PLATFORM'S ACTION AGAINST THE OTHER'S, and
# nothing about this constant could. Home Assistant builds one semaphore per
# (config entry, platform), so a limit here would never stand between a reboot
# press and an apt install in flight -- they live in different platforms. That
# guard is coordinator.install_in_progress, which button.py reads before it
# will issue a reboot (#11).
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LinuxMonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.allow_install:
        return
    async_add_entities([LinuxMonitorRebootButton(coordinator)])


class LinuxMonitorRebootButton(LinuxMonitorEntity, ButtonEntity):
    _attr_name = "Reboot host"
    _attr_icon = "mdi:restart-alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "reboot")

    async def async_press(self) -> None:
        host = self.coordinator.hostname
        _LOGGER.warning("%s: remote reboot requested", host)
        # async_exec already refuses on an offline_expected entry and logs why.
        ok, _out, _kind = await self.coordinator.async_exec(
            REBOOT_CMD, REBOOT_TIMEOUT
        )
        _LOGGER.info(
            "%s: reboot command issued (ssh ok=%s). A non-zero exit here is "
            "not evidence of failure — the reboot drops the transport. Watch "
            "the reboot counter for the uptime reset that proves it.",
            host, ok,
        )
