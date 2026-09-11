"""The wiring: an entry set up for real, with all four platforms mounted.

Everything else in this suite exercises a class in isolation. This module is
the only one that proves the parts are connected -- that the platform forwards
happen, that entities reach the registry under the ids the frozen unique_id
scheme mints, that DeviceInfo is populated from the ssh read, and that the two
entities LAW.md §11 contracts for stay available when their subject does not.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.linux_monitor.const import (
    CONF_ALLOW_INSTALL,
    CONF_OFFLINE_EXPECTED,
    DOMAIN,
)
from custom_components.linux_monitor.coordinator import LinuxMonitorCoordinator

from .conftest import ssh_answers


async def test_every_platform_mounts_and_reads(hass, mounted) -> None:
    entry = await mounted()
    assert entry.state is entry.state.LOADED

    # The arithmetic, end to end from the KEY=value text.
    assert hass.states.get("sensor.testhost_cpu").state == "50.0"
    assert hass.states.get("sensor.testhost_memory").state == "25.0"
    assert hass.states.get("sensor.testhost_disk").state == "40.0"
    assert hass.states.get("sensor.testhost_temperature").state == "45.1"
    assert hass.states.get("sensor.testhost_load_average_1m").state == "0.15"
    assert hass.states.get("sensor.testhost_kernel_running").state == (
        "6.12.101+deb13-amd64"
    )
    assert hass.states.get("sensor.testhost_updates_pending").state == "4"
    assert hass.states.get("sensor.testhost_security_updates_pending").state == "2"

    health = hass.states.get("binary_sensor.testhost_health")
    assert health.state == "off"
    assert health.attributes["disposition"] == "ok"

    assert hass.states.get("update.testhost_system_updates") is not None


async def test_the_attributes_that_carry_the_identity_behind_a_number(
    hass, mounted
) -> None:
    """A bare count does not tell you whether it is libnss3 or a font, and a
    temperature with no named source cannot be told from one read off the
    wrong chip."""
    await mounted()
    assert hass.states.get("sensor.testhost_temperature").attributes["source"] == (
        "coretemp"
    )
    disk = hass.states.get("sensor.testhost_disk").attributes
    assert disk["mnt_point"] == "/" and disk["fs_type"] == "ext4"
    sec = hass.states.get("sensor.testhost_security_updates_pending").attributes
    assert sec["packages"] == "libssl3,curl"
    load = hass.states.get("sensor.testhost_load_average_1m").attributes
    assert load["cores"] == 4 and load["min15"] == 0.25


async def test_device_info_comes_off_the_ssh_read(hass, mounted) -> None:
    """manufacturer, model and sw_version are read from the host, not typed in
    at config time. No configuration_url: it used to point at a Glances web UI
    that dies with the daemon this integration no longer needs, and a dead
    link on a device page is worse than none."""
    await mounted()
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "testhost")}
    )
    assert device is not None
    assert device.name == "testhost"
    assert device.manufacturer == "Debian GNU/Linux 13"
    assert device.model == "Intel(R) Core(TM) i5-9500T"
    assert device.sw_version == "6.12.101+deb13-amd64"
    assert device.configuration_url is None


async def test_unique_ids_are_hostname_plus_key(hass, mounted) -> None:
    """FROZEN. They feed every entity_id in the registry, so a change here is
    a rename of everything downstream."""
    await mounted()
    registry = er.async_get(hass)
    entity = registry.async_get("sensor.testhost_cpu")
    assert entity is not None
    assert entity.unique_id == "testhost_cpu"
    assert registry.async_get("binary_sensor.testhost_health").unique_id == (
        "testhost_health"
    )


async def test_the_reboot_button_does_not_exist_without_the_grant(
    hass, mounted
) -> None:
    """ABSENT rather than present-and-refusing: a control that is visible but
    always fails trains an operator to ignore what it says."""
    await mounted()
    assert hass.states.get("button.testhost_reboot_host") is None


async def test_the_reboot_button_appears_once_the_grant_is_given(
    hass, mounted
) -> None:
    await mounted(options={CONF_ALLOW_INSTALL: True})
    assert hass.states.get("button.testhost_reboot_host") is not None


async def test_a_host_that_never_answers_still_reports(hass, mounted) -> None:
    """THE CONTRACT. Individual readings go unavailable, but health and the
    reboot counter do not -- a monitor that disappears with its subject cannot
    report the subject down, and an unavailable entity's attributes vanish
    with it."""

    async def dead(script, timeout):  # noqa: ARG001
        return None, "", "timeout"

    await mounted(raw=dead)

    assert hass.states.get("sensor.testhost_cpu").state == STATE_UNAVAILABLE
    health = hass.states.get("binary_sensor.testhost_health")
    assert health.state != STATE_UNAVAILABLE
    assert health.attributes["ssh_ok"] is False
    counter = hass.states.get("sensor.testhost_reboot_count")
    assert counter.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    # The update entity too -- an unknown patch state must be SAID, and an
    # unavailable entity cannot say it.
    assert hass.states.get("update.testhost_system_updates").state != (
        STATE_UNAVAILABLE
    )


async def test_an_offline_expected_host_scores_clean(hass, mounted) -> None:
    """Named and dispositioned rather than reported as a fault."""
    await mounted(options={CONF_OFFLINE_EXPECTED: True})
    health = hass.states.get("binary_sensor.testhost_health")
    assert health.state == "off"
    assert health.attributes["disposition"] == "offline_expected"
    assert health.attributes["offline_expected"] is True


async def test_unloading_removes_every_platform(hass, mounted) -> None:
    entry = await mounted()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is entry.state.NOT_LOADED
    assert hass.states.get("sensor.testhost_cpu") is None


async def test_changing_an_option_reloads_the_entry(hass, mounted) -> None:
    """allow_install NEEDS the reload rather than merely benefiting from one:
    it decides whether the update entity advertises INSTALL and whether the
    reboot button is created at all, and both are read once, at setup."""
    entry = await mounted()
    assert hass.states.get("button.testhost_reboot_host") is None

    with patch.object(LinuxMonitorCoordinator, "_ssh_raw", ssh_answers()):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_ALLOW_INSTALL: True}
        )
        await hass.async_block_till_done()

    assert hass.states.get("button.testhost_reboot_host") is not None
