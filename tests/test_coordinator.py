"""Coordinator behaviour that no amount of static reading proves.

Three contracts are under test, and all three are ones a regression would
break SILENTLY:
  * the coordinator never raises, whatever the transport does;
  * a refused credential is counted separately from an unreachable host and
    raises a reauth flow exactly once;
  * the per-package first-seen clock is keyed on the package NAME, so one
    package arriving does not restart another's clock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.linux_monitor.const import (
    CONF_OFFLINE_EXPECTED,
    SSH_AUTH_FAIL_DWELL,
)

AUTH_STDERR = "monitor@host: Permission denied (publickey)."
UNREACHABLE_STDERR = "ssh: connect to host h port 22: No route to host"
GOOD_FAST = "HOSTNAME=testhost\nUPTIME_SECS=100.0\nCORES=4\n__END__\n"


def _raw(rc, out="", err=""):
    """Stand in for _ssh_raw, which is the only thing that touches a
    subprocess. Everything above it is the code under test."""
    return AsyncMock(return_value=(rc, out, err))


async def test_a_dead_transport_never_raises(hass, coordinator_factory) -> None:
    """The never-raise contract. Raising takes every entity unavailable and an
    unavailable entity's attributes vanish -- which is how a broken collector
    reads green."""
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", _raw(None, "", "timeout")):
        data = await c._async_update_data()
    assert data["online"] is False
    assert data["ssh_ok"] is False
    assert data["offline_expected"] is False


async def test_an_offline_expected_host_does_no_io_at_all(
    hass, coordinator_factory
) -> None:
    """It scores clean rather than red: a host that is meant to be off is not
    a fault, and it is not a thing to go and poll either."""
    c = coordinator_factory(options={CONF_OFFLINE_EXPECTED: True})
    with patch.object(c, "_ssh_raw", _raw(0, GOOD_FAST)) as raw:
        data = await c._async_update_data()
    raw.assert_not_called()
    assert data["offline_expected"] is True
    assert data["online"] is False
    assert data["auth_failed"] is False


async def test_a_refused_credential_raises_a_reauth_flow_exactly_once(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with (
        patch.object(c, "_ssh_raw", _raw(255, "", AUTH_STDERR)),
        patch.object(c.entry, "async_start_reauth") as start,
    ):
        # Below the dwell: counted, but nothing shown to the operator yet.
        for _ in range(SSH_AUTH_FAIL_DWELL - 1):
            data = await c._async_update_data()
            assert data["auth_failed"] is False
        start.assert_not_called()

        # At the dwell.
        data = await c._async_update_data()
        assert data["auth_failed"] is True
        assert start.call_count == 1

        # And on for ever after, without asking again every 60 seconds.
        for _ in range(4):
            await c._async_update_data()
        assert start.call_count == 1


async def test_an_unreachable_host_never_raises_a_reauth_flow(
    hass, coordinator_factory
) -> None:
    """THE WHOLE POINT OF #13, in reverse: a reauth card the operator cannot
    satisfy is worse than no card, so a network fault must never produce one."""
    c = coordinator_factory()
    with (
        patch.object(c, "_ssh_raw", _raw(255, "", UNREACHABLE_STDERR)),
        patch.object(c.entry, "async_start_reauth") as start,
    ):
        for _ in range(SSH_AUTH_FAIL_DWELL + 3):
            data = await c._async_update_data()
    start.assert_not_called()
    assert data["auth_failed"] is False


async def test_a_timeout_does_not_clear_the_auth_count(
    hass, coordinator_factory
) -> None:
    """A host that has stopped answering cannot testify that its key is fine.
    Clearing here would let a flapping host mask a revoked key for ever."""
    c = coordinator_factory()
    with (
        patch.object(c, "_ssh_raw", _raw(255, "", AUTH_STDERR)),
        patch.object(c.entry, "async_start_reauth"),
    ):
        for _ in range(SSH_AUTH_FAIL_DWELL - 1):
            await c._async_update_data()
    before = c._auth_fails
    assert before == SSH_AUTH_FAIL_DWELL - 1

    with patch.object(c, "_ssh_raw", _raw(None, "", "timeout")):
        await c._async_update_data()
    assert c._auth_fails == before, "a timeout must not exonerate the credential"


async def test_a_good_poll_clears_the_auth_count(hass, coordinator_factory) -> None:
    """A new key landing on the host must put the entry back to normal without
    anyone restarting anything."""
    c = coordinator_factory()
    with (
        patch.object(c, "_ssh_raw", _raw(255, "", AUTH_STDERR)),
        patch.object(c.entry, "async_start_reauth"),
    ):
        for _ in range(SSH_AUTH_FAIL_DWELL):
            await c._async_update_data()
    assert c._auth_fails >= SSH_AUTH_FAIL_DWELL

    with patch.object(c, "_ssh_raw", _raw(0, GOOD_FAST)):
        data = await c._async_update_data()
    assert c._auth_fails == 0
    assert data["auth_failed"] is False
    assert data["online"] is True


async def test_a_host_key_failure_is_not_a_credential_failure(
    hass, coordinator_factory
) -> None:
    """No reauth form can edit known_hosts, so raising one would be an ask the
    operator cannot answer."""
    c = coordinator_factory()
    with (
        patch.object(c, "_ssh_raw", _raw(255, "", "Host key verification failed.")),
        patch.object(c.entry, "async_start_reauth") as start,
    ):
        for _ in range(SSH_AUTH_FAIL_DWELL + 2):
            data = await c._async_update_data()
    start.assert_not_called()
    assert data["auth_failed"] is False
    assert data["online"] is False


# --- the first-seen clock ---------------------------------------------------


async def test_the_first_seen_clock_is_keyed_on_the_package_name(
    hass, coordinator_factory
) -> None:
    """Count-keyed, a second package landing would restart the first's clock
    and clearing one would restart the other's. Both directions under-report
    age, which is the direction that hides the problem."""
    c = coordinator_factory()
    with patch.object(c, "_save", AsyncMock()):
        await c._track_pending({"SECURITY_PKGS": "libssl3"})
        first = dict(c._pending_since)
        assert set(first) == {"libssl3"}

        await c._track_pending({"SECURITY_PKGS": "libssl3,curl"})
        assert set(c._pending_since) == {"libssl3", "curl"}
        assert c._pending_since["libssl3"] == first["libssl3"], (
            "a new package restarted an existing package's clock"
        )

        await c._track_pending({"SECURITY_PKGS": "libssl3"})
        assert set(c._pending_since) == {"libssl3"}
        assert c._pending_since["libssl3"] == first["libssl3"]


async def test_a_host_that_did_not_answer_changes_nothing(
    hass, coordinator_factory
) -> None:
    """An unread poll is not an empty set. Treating it as one would clear the
    clock on exactly the hosts that stopped reporting."""
    c = coordinator_factory()
    with patch.object(c, "_save", AsyncMock()):
        await c._track_pending({"SECURITY_PKGS": "libssl3"})
        before = dict(c._pending_since)
        await c._track_pending({})  # SECURITY_PKGS absent -- nobody asked
    assert c._pending_since == before


async def test_security_pkgs_tells_nobody_asked_from_none_pending(
    hass, coordinator_factory
) -> None:
    """Collapsing those two is the stale-zero shape this integration exists to
    refuse."""
    c = coordinator_factory()
    assert c.security_pkgs({}) is None, "absent key must read as 'nobody asked'"
    assert c.security_pkgs({"SECURITY_PKGS": ""}) == [], (
        "an empty answer is the host saying it has none"
    )
    assert c.security_pkgs({"SECURITY_PKGS": "a,b"}) == ["a", "b"]


async def test_pending_since_is_the_oldest_clock(hass, coordinator_factory) -> None:
    c = coordinator_factory()
    assert c.pending_since is None
    with patch.object(c, "_save", AsyncMock()):
        await c._track_pending({"SECURITY_PKGS": "a"})
        oldest = c.pending_since
        await c._track_pending({"SECURITY_PKGS": "a,b"})
    assert c.pending_since == oldest


# --- a reboot the streak tracker cannot see ---------------------------------


async def test_an_uptime_reset_counts_a_reboot_even_with_no_missed_poll(
    hass, coordinator_factory
) -> None:
    """A crash-loop that self-heals inside the dwell is invisible to the
    health sensor. Uptime only ever increases within one boot, so a decrease
    is proof regardless of whether a miss was observed."""
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", _raw(0, "HOSTNAME=t\nUPTIME_SECS=9000.0\n__END__\n")):
        await c._async_update_data()
    assert c.reboot_count == 0

    with patch.object(c, "_ssh_raw", _raw(0, "HOSTNAME=t\nUPTIME_SECS=12.0\n__END__\n")):
        await c._async_update_data()
    assert c.reboot_count == 1

    with patch.object(c, "_ssh_raw", _raw(0, "HOSTNAME=t\nUPTIME_SECS=72.0\n__END__\n")):
        await c._async_update_data()
    assert c.reboot_count == 1, "a rising uptime is not a second reboot"


@pytest.mark.parametrize("missing", ["", "UPTIME_SECS=\n"])
async def test_an_unreadable_uptime_never_counts_a_reboot(
    hass, coordinator_factory, missing
) -> None:
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", _raw(0, "HOSTNAME=t\nUPTIME_SECS=9000.0\n__END__\n")):
        await c._async_update_data()
    with patch.object(c, "_ssh_raw", _raw(0, f"HOSTNAME=t\n{missing}__END__\n")):
        await c._async_update_data()
    assert c.reboot_count == 0
