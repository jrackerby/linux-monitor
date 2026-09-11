"""Entry setup, unload, and the migration that strips a dead key.

The migration is the interesting one. GH-470 removed the Glances port, and the
key was STRIPPED from entry data rather than left inert: .storage would
otherwise go on advertising a port no code path reads and no daemon has to be
listening on, and the next person -- or the next session deriving inventory
from that file -- would reasonably conclude this integration still talks to
Glances. A dead key that reads as live configuration is exactly the kind of
confidently-wrong artefact that propagates.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.linux_monitor import async_migrate_entry
from custom_components.linux_monitor.const import (
    CONF_HOST,
    CONF_HOSTNAME,
    CONF_SSH_KEY,
    CONF_SSH_USER,
    DOMAIN,
    LEGACY_CONF_GLANCES_PORT,
)

V1_DATA = {
    CONF_HOST: "203.0.113.5",
    CONF_HOSTNAME: "testhost",
    CONF_SSH_USER: "monitor",
    CONF_SSH_KEY: "/config/.ssh/test_key",
    LEGACY_CONF_GLANCES_PORT: 61208,
}


async def test_v1_loses_the_glances_port_and_nothing_else(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=V1_DATA, version=1, unique_id="testhost"
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert LEGACY_CONF_GLANCES_PORT not in entry.data
    # HOST, HOSTNAME, SSH_USER and SSH_KEY are untouched -- unique_id is built
    # from the hostname, so moving any of them would rename every entity.
    assert entry.data[CONF_HOST] == V1_DATA[CONF_HOST]
    assert entry.data[CONF_HOSTNAME] == V1_DATA[CONF_HOSTNAME]
    assert entry.data[CONF_SSH_USER] == V1_DATA[CONF_SSH_USER]
    assert entry.data[CONF_SSH_KEY] == V1_DATA[CONF_SSH_KEY]
    assert entry.unique_id == "testhost"


async def test_an_entry_already_at_2_is_left_alone(hass) -> None:
    data = {k: v for k, v in V1_DATA.items() if k != LEGACY_CONF_GLANCES_PORT}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=2)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.data == data
    assert entry.version == 2


async def test_a_downgrade_is_refused_rather_than_guessed_at(hass) -> None:
    """An entry written by a FUTURE version. Refusing leaves it broken and
    visible; guessing at its shape would quietly corrupt it."""
    entry = MockConfigEntry(domain=DOMAIN, data=V1_DATA, version=3)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is False


async def test_setup_stores_the_coordinator_as_typed_runtime_data(
    hass, mounted
) -> None:
    """runtime_data rather than hass.data[DOMAIN] -- and loaded BEFORE the
    first refresh, or that poll writes a fresh first-seen for every pending
    package and every patch age resets to zero on every restart."""
    from custom_components.linux_monitor.coordinator import LinuxMonitorCoordinator

    entry = await mounted()
    assert isinstance(entry.runtime_data, LinuxMonitorCoordinator)
    assert entry.runtime_data.hostname == "testhost"
    assert entry.runtime_data.data["online"] is True
