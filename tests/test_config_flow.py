"""The config flow -- and the rule this repo is the worked example of.

`test-before-configure` is not an abstraction here. An earlier version of this
integration probed a Glances daemon for a hostname while polling over ssh, so
an entry was created against an ssh account that did not exist on that
machine, validated green, and went dark weeks later when the daemon it WAS
relying on broke. Every test below exists to keep a probe pointed at the
channel the entry will actually use.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.linux_monitor.const import (
    CONF_ALLOW_INSTALL,
    CONF_HOST,
    CONF_HOSTNAME,
    CONF_OFFLINE_EXPECTED,
    CONF_SSH_KEY,
    CONF_SSH_USER,
    DOMAIN,
)


PROBE = "custom_components.linux_monitor.config_flow._probe_hostname"
SUDO_PROBE = "custom_components.linux_monitor.config_flow._probe_sudo"
SETUP = "custom_components.linux_monitor.async_setup_entry"

async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


# --- user step --------------------------------------------------------------


async def test_user_flow_asks_the_host_its_own_name(
    hass, enable_custom_integrations, user_input, creds
) -> None:
    """The hostname is READ FROM THE HOST over the credential being stored,
    not taken on trust from whatever was typed in the address box."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with (
        patch(PROBE, return_value=creds["hostname"]) as probe,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == creds["hostname"]
    assert result["data"] == {
        CONF_HOST: creds["host"],
        CONF_HOSTNAME: creds["hostname"],
        CONF_SSH_USER: creds["ssh_user"],
        CONF_SSH_KEY: creds["ssh_key"],
    }
    # offline_expected is an OPTION, never entry data -- it is changeable
    # without recreating the entry.
    assert result["options"] == {CONF_OFFLINE_EXPECTED: False}
    # Probed over the account, key and address the entry will poll with.
    probe.assert_called_once_with(creds["host"], creds["ssh_user"], creds["ssh_key"])


async def test_user_flow_refuses_when_the_probe_cannot_reach_the_host(
    hass, enable_custom_integrations, user_input, creds
) -> None:
    """An unreachable host is a refusal, not a pass with a blank name."""
    result = await _start(hass)
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_offline_host_must_be_named_by_hand(
    hass, enable_custom_integrations, user_input, creds
) -> None:
    """A host that is deliberately powered down cannot be asked its name, so
    the flow says so rather than inventing one."""
    result = await _start(hass)
    with patch(PROBE, return_value=None) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**user_input, CONF_OFFLINE_EXPECTED: True, CONF_HOSTNAME: ""},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOSTNAME: "hostname_required_offline"}
    probe.assert_not_called()


async def test_an_offline_host_with_a_typed_name_is_accepted_without_probing(
    hass, enable_custom_integrations, user_input, creds
) -> None:
    result = await _start(hass)
    with (
        patch(PROBE, return_value=None) as probe,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**user_input, CONF_OFFLINE_EXPECTED: True, CONF_HOSTNAME: "darkhost"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "darkhost"
    assert result["options"] == {CONF_OFFLINE_EXPECTED: True}
    probe.assert_not_called()


async def test_the_same_host_cannot_be_added_twice(
    hass, enable_custom_integrations, entry_factory, user_input, creds
) -> None:
    """Keyed on the host's OWN name, so one machine added under two addresses
    is still one entry."""
    entry_factory().add_to_hass(hass)
    result = await _start(hass)
    with patch(PROBE, return_value=creds["hostname"]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- reauth -----------------------------------------------------------------


async def test_reauth_stores_a_new_key_once_the_host_accepts_it(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    entry = entry_factory()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with (
        patch(PROBE, return_value=creds["hostname"]) as probe,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SSH_USER: "monitor2", CONF_SSH_KEY: "/config/.ssh/new_key"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SSH_USER] == "monitor2"
    assert entry.data[CONF_SSH_KEY] == "/config/.ssh/new_key"
    # Unchanged -- reauth replaces the credential, never the subject.
    assert entry.data[CONF_HOST] == creds["host"]
    assert entry.data[CONF_HOSTNAME] == creds["hostname"]
    probe.assert_called_once_with(creds["host"], "monitor2", "/config/.ssh/new_key")


async def test_reauth_refuses_a_credential_the_host_will_not_take(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    """Storing it unprobed would leave the operator sure they had fixed it."""
    entry = entry_factory()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SSH_USER: creds["ssh_user"], CONF_SSH_KEY: "/config/.ssh/still_wrong"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_SSH_KEY] == creds["ssh_key"], "nothing may be stored on a refusal"


async def test_reauth_refuses_a_credential_that_reaches_a_different_machine(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    """THE ADDRESS WAS REASSIGNED, or the key went on the wrong box. Storing
    it would leave this entry quietly monitoring something else under the old
    unique_id, which is worse than refusing."""
    entry = entry_factory()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(PROBE, return_value="someone-elses-host"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SSH_USER: creds["ssh_user"], CONF_SSH_KEY: "/config/.ssh/new_key"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_host"}
    assert entry.data[CONF_SSH_KEY] == creds["ssh_key"]


async def test_reauth_accepts_a_hostname_differing_only_in_case(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    """Hostnames are case-insensitive; refusing on case alone would reject a
    working credential and leave the operator with no way back in."""
    entry = entry_factory()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(PROBE, return_value=creds["hostname"].upper()), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SSH_USER: creds["ssh_user"], CONF_SSH_KEY: "/config/.ssh/new_key"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


# --- options ----------------------------------------------------------------


async def test_the_sudo_grant_is_probed_before_it_is_stored(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    entry = entry_factory()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # SETUP is patched because storing an option fires the update listener,
    # which reloads the entry -- and a real setup here would spawn ssh.
    with patch(SUDO_PROBE, return_value=True) as probe, patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_OFFLINE_EXPECTED: False, CONF_ALLOW_INSTALL: True},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALLOW_INSTALL] is True
    probe.assert_called_once_with(creds["host"], creds["ssh_user"], creds["ssh_key"])


@pytest.mark.parametrize("probe_answer", [False])
async def test_a_host_that_cannot_be_asked_is_not_a_yes(
    hass, enable_custom_integrations, entry_factory, probe_answer, creds
) -> None:
    """_probe_sudo returns False on a transport failure too. The question is
    whether the grant may be STORED, and an unreachable host is not a yes."""
    entry = entry_factory()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(SUDO_PROBE, return_value=probe_answer):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_OFFLINE_EXPECTED: False, CONF_ALLOW_INSTALL: True},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ALLOW_INSTALL: "no_sudo"}
    assert entry.options.get(CONF_ALLOW_INSTALL) is False


async def test_withdrawing_the_grant_is_never_gated(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    """Turning it OFF must work from a host that cannot be reached to confirm
    it -- otherwise one network fault locks permission ON."""
    entry = entry_factory(options={CONF_ALLOW_INSTALL: True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(SUDO_PROBE, return_value=False) as probe, patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_OFFLINE_EXPECTED: False, CONF_ALLOW_INSTALL: False},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALLOW_INSTALL] is False
    probe.assert_not_called()


async def test_an_already_granted_entry_is_not_reprobed(
    hass, enable_custom_integrations, entry_factory, creds
) -> None:
    """Re-probing would let one transient network fault revoke a grant that
    was proven when it mattered."""
    entry = entry_factory(options={CONF_ALLOW_INSTALL: True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(SUDO_PROBE, return_value=False) as probe, patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_OFFLINE_EXPECTED: True, CONF_ALLOW_INSTALL: True},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALLOW_INSTALL] is True
    probe.assert_not_called()
