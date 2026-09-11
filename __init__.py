"""The Linux Monitor integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import LEGACY_CONF_GLANCES_PORT
from .coordinator import LinuxMonitorCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    # BUTTON forwards unconditionally and sets up nothing when the entry has
    # not opted in to privileged action -- the decision lives in button.py,
    # beside the entity it gates, rather than in a conditional platform list
    # that would silently stop reloading when the option is turned on.
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.UPDATE,
]

type LinuxMonitorConfigEntry = ConfigEntry[LinuxMonitorCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: LinuxMonitorConfigEntry) -> bool:
    coordinator = LinuxMonitorCoordinator(hass, entry)
    # BEFORE the first refresh, never after. That poll writes a first-seen
    # timestamp for every pending security package, so loading the persisted
    # clock afterwards would be overwritten by it and every patch age would
    # reset to zero on every Home Assistant restart.
    await coordinator.async_load_pending()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1 -> 2: strip the vestigial glances_port from entry data.

    STRIPPED RATHER THAN LEFT INERT, deliberately. Leaving it would mean
    .storage keeps advertising a port that no code path reads and no daemon
    is required to be listening on -- the next person to read that file, or
    the next session to derive inventory from it, would reasonably conclude
    this integration still talks to Glances. A dead key that reads as live
    configuration is exactly the kind of confidently-wrong artefact that
    propagates.

    Nothing else moves: host, hostname, ssh_user and ssh_key are unchanged,
    unique_id is untouched, and so no entity_id or unique_id is affected.
    """
    if entry.version > 2:
        # Downgraded from a future version -- refuse rather than guess.
        return False

    if entry.version == 1:
        data = {k: v for k, v in entry.data.items()
                if k != LEGACY_CONF_GLANCES_PORT}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info(
            "%s: migrated config entry to version 2 (dropped %s)",
            entry.title, LEGACY_CONF_GLANCES_PORT,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: LinuxMonitorConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: LinuxMonitorConfigEntry) -> None:
    """offline_expected and allow_install both live in options; a reload makes
    either take effect. allow_install NEEDS the reload rather than merely
    benefiting from one: it decides whether the update entity advertises
    INSTALL and whether the reboot button is created at all, and both of those
    are read once, at platform setup."""
    await hass.config_entries.async_reload(entry.entry_id)
