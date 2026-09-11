"""The reboot button. #11: it used to log and raise nothing.

A control that declines quietly leaves the operator believing the host did the
thing, which on a reboot control means believing a machine restarted when it
did not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.linux_monitor.button import LinuxMonitorRebootButton
from custom_components.linux_monitor.const import (
    CONF_ALLOW_INSTALL,
    CONF_OFFLINE_EXPECTED,
)
from custom_components.linux_monitor.coordinator import (
    SSH_AUTH,
    SSH_OK,
    SSH_REMOTE,
    SSH_TRANSPORT_LOST,
    SSH_UNREACHABLE,
)


@pytest.fixture
def button(coordinator_factory):
    def _make(*, options=None):
        c = coordinator_factory(
            options={CONF_ALLOW_INSTALL: True, **(options or {})}
        )
        return LinuxMonitorRebootButton(c), c

    return _make


async def test_it_refuses_a_host_marked_offline_expected(hass, button) -> None:
    entity, c = button(options={CONF_OFFLINE_EXPECTED: True})
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="offline_expected"):
            await entity.async_press()
    ex.assert_not_called()


async def test_it_refuses_when_the_grant_has_been_withdrawn(hass, button) -> None:
    """The button is not created without the grant, so reaching this means the
    option went away while the entity was still mounted."""
    entity, c = button(options={CONF_ALLOW_INSTALL: False})
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="opted in"):
            await entity.async_press()
    ex.assert_not_called()


async def test_it_refuses_a_host_mid_upgrade(hass, button) -> None:
    """A reboot driven through dpkg leaves a part-configured system, and the
    upgrade chains its own reboot when it finishes anyway."""
    entity, c = button()
    c.install_in_progress = True
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="mid apt-get upgrade"):
            await entity.async_press()
    ex.assert_not_called()


async def test_a_transport_that_dropped_is_a_reboot_that_worked(hass, button) -> None:
    """THE ONE FAILURE CLASS ALLOWED THROUGH. ssh cannot return cleanly from a
    command that tears down the session carrying its result."""
    entity, c = button()
    with patch.object(
        c, "async_exec", AsyncMock(return_value=(False, "", SSH_TRANSPORT_LOST))
    ):
        await entity.async_press()  # must not raise


async def test_a_clean_return_is_also_accepted(hass, button) -> None:
    entity, c = button()
    with patch.object(c, "async_exec", AsyncMock(return_value=(True, "", SSH_OK))):
        await entity.async_press()


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        (SSH_UNREACHABLE, "the host was never reached, so nothing rebooted"),
        (SSH_AUTH, "the credential was refused, so nothing rebooted"),
        (SSH_REMOTE, "sudo refused -- the command ran and declined"),
    ],
)
async def test_a_failure_that_proves_nothing_ran_raises(
    hass, button, kind, why
) -> None:
    """A class rather than 'any error', precisely so a refused sudo or a dead
    host cannot hide inside the transport-lost case."""
    entity, c = button()
    with patch.object(c, "async_exec", AsyncMock(return_value=(False, "", kind))):
        with pytest.raises(HomeAssistantError, match="NOT issued"):
            await entity.async_press()


async def test_the_refusals_are_checked_before_anything_is_sent(hass, button) -> None:
    """Ordering matters: a refusal discovered after the command went out is
    not a refusal."""
    entity, c = button(options={CONF_OFFLINE_EXPECTED: True})
    c.install_in_progress = True
    with patch.object(c, "_ssh_raw", AsyncMock()) as raw:
        with pytest.raises(HomeAssistantError):
            await entity.async_press()
    raw.assert_not_called()
