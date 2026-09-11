"""Buttons -- the privileged host controls.

ONE BUTTON, AND IT ONLY EXISTS WHEN THE ENTRY OPTED IN. Reboot needs the same
passwordless sudo the apt upgrade does, and const.py's CONF_ALLOW_INSTALL is
the single grant covering both. On an entry that has not opted in the button
is ABSENT rather than present-and-refusing: a control that is visible but
always fails trains an operator to ignore what it says.

NAMED "Reboot host", NOT "Reboot". Another integration on the same host may
already register button.<host>_reboot against a device carrying the same name,
and Home Assistant mints an entity_id from device name plus entity name -- a
colliding id is taken as _2 and never reclaimed, even after the other entity
goes away. update.py's header carries the same note for the same reason.

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LinuxMonitorConfigEntry
from .const import REBOOT_TIMEOUT
from .coordinator import REBOOT_CMD, SSH_TRANSPORT_LOST
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
        """RAISES on every refusal knowable before the command goes out, and
        on every failure that proves it never ran.

        It used to log at INFO and raise nothing, so a press against an
        unreachable host -- or one silently refused because the entry is
        marked offline_expected -- looked in the UI exactly like a press that
        worked. update.async_install has raised on every refusal since it was
        written, for the reason that applies just as hard here: a control that
        declines quietly leaves the operator believing the host did the thing
        (#11).

        THE ONE FAILURE THAT IS NOT A FAILURE is the transport dropping
        mid-command, because that is what a SUCCESSFUL reboot looks like from
        this end -- the command tore down the ssh session carrying its own
        result, so there is no clean return to wait for. That is the only
        class allowed through, and it is a class rather than "any error"
        precisely so a refused sudo or a dead host cannot hide inside it.

        A genuinely dropped network at the wrong instant is indistinguishable
        from that and will read as issued. Irreducible from here: the proof of
        a reboot was never this call's return value, it is the uptime reset a
        later poll sees, which is what the reboot counter watches.
        """
        host = self.coordinator.hostname

        if self.coordinator.offline_expected:
            raise HomeAssistantError(
                f"{host} is marked offline_expected and is not polled. "
                "Refusing to reboot a host the integration is not watching — "
                "clear the flag first."
            )
        if not self.coordinator.allow_install:
            # The button is not created without the grant, so reaching this
            # means the option was withdrawn while this entity was still
            # mounted. Refuse rather than act on a grant that is gone.
            raise HomeAssistantError(
                f"{host} has not opted in to remote patching and reboot. "
                "Enable it in this entry's options; it is only accepted once "
                "sudo answers over this entry's own SSH credential."
            )
        if self.coordinator.install_in_progress:
            raise HomeAssistantError(
                f"{host} is mid apt-get upgrade. Refusing to reboot it — a "
                "reboot through dpkg leaves a part-configured system, and the "
                "upgrade chains its own reboot when it finishes."
            )

        _LOGGER.warning("%s: remote reboot requested", host)
        ok, _out, kind = await self.coordinator.async_exec(
            REBOOT_CMD, REBOOT_TIMEOUT
        )
        if ok or kind == SSH_TRANSPORT_LOST:
            _LOGGER.info(
                "%s: reboot command issued (ssh ok=%s, %s). Proof is the "
                "uptime reset a later poll sees, which the reboot counter "
                "watches — not this return.",
                host, ok, kind,
            )
            return

        raise HomeAssistantError(
            f"{host}: the reboot was NOT issued. ssh failed before the "
            f"command could run ({kind}). This is not the transport dropping "
            "underneath a reboot that worked — that is a different failure "
            "and is allowed through. Check the host and this entry's "
            "credential; the reboot counter will not move."
        )
