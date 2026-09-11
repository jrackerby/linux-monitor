"""The coordinator's transport, its store, and the slow block's cadence.

These are the parts every other test patches out, which is right for those
tests and leaves the machinery underneath unexercised.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util import dt as dt_util

from custom_components.linux_monitor.const import (
    CONF_OFFLINE_EXPECTED,
    DEFAULT_KNOWN_HOSTS,
    SLOW_INTERVAL,
    SLOW_RETRY_INTERVAL,
    STORE_SHAPE,
    STORE_SHAPE_KEY,
    TRANSPORT_FAIL_DWELL,
)
from custom_components.linux_monitor.coordinator import SSH_OK, SSH_REMOTE

from .conftest import FAST_OUT, SLOW_OUT, ssh_answers


def _proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = rc
    proc.wait = AsyncMock()
    return proc


# --- the transport ----------------------------------------------------------


async def test_ssh_is_invoked_with_this_entrys_credential(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with patch(
        "asyncio.create_subprocess_exec", return_value=_proc(b"out", b"")
    ) as spawn:
        rc, out, err = await c._ssh_raw("echo hi", 10)

    assert (rc, out, err) == (0, "out", "")
    argv = spawn.call_args.args
    assert argv[0] == "ssh"
    assert c.ssh_key in argv and f"{c.ssh_user}@{c.host}" in argv
    assert "BatchMode=yes" in argv, "a poll must never block on a prompt"
    assert f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}" in argv


async def test_a_spawn_failure_reports_rc_none_rather_than_raising(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("no ssh")):
        rc, out, err = await c._ssh_raw("echo hi", 10)
    assert rc is None and out == "" and "no ssh" in err


async def test_a_timeout_kills_the_process(hass, coordinator_factory) -> None:
    """A poll that leaves ssh running would accumulate one stuck subprocess a
    minute against a host that stopped answering."""
    c = coordinator_factory()
    proc = _proc()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        rc, _out, err = await c._ssh_raw("echo hi", 1)
    assert rc is None and err == "timeout"
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_undecodable_output_does_not_raise(hass, coordinator_factory) -> None:
    c = coordinator_factory()
    with patch("asyncio.create_subprocess_exec", return_value=_proc(b"\xff\xfe")):
        rc, out, _err = await c._ssh_raw("echo hi", 10)
    assert rc == 0 and isinstance(out, str)


# --- async_exec -------------------------------------------------------------


async def test_async_exec_refuses_an_offline_expected_host(
    hass, coordinator_factory
) -> None:
    """It refuses before any i/o, so a host declared powered-down is never
    poked by a button press."""
    c = coordinator_factory(options={CONF_OFFLINE_EXPECTED: True})
    with patch.object(c, "_ssh_raw", AsyncMock()) as raw:
        ok, out, kind = await c.async_exec("reboot", 5)
    assert (ok, out, kind) == (False, "", SSH_REMOTE)
    raw.assert_not_called()


async def test_async_exec_returns_the_classification_with_the_result(
    hass, coordinator_factory
) -> None:
    """Third element rather than an attribute on self: a caller reading it off
    the coordinator would be reading whatever the last call left there."""
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", AsyncMock(return_value=(0, "out", ""))):
        assert await c.async_exec("true", 5) == (True, "out", SSH_OK)

    with patch.object(
        c, "_ssh_raw", AsyncMock(return_value=(1, "", "sudo: a password is required"))
    ):
        ok, _out, kind = await c.async_exec("sudo -n reboot", 5)
    assert ok is False and kind == SSH_REMOTE


# --- the streak tracker -----------------------------------------------------


async def test_recovery_is_logged_once_after_a_real_outage(
    hass, coordinator_factory, caplog
) -> None:
    """Logged at the crossing and at the recovery, never every 60s -- a
    warning that repeats every poll is as unreadable as no warning."""
    c = coordinator_factory()
    streak = 0
    for _ in range(TRANSPORT_FAIL_DWELL):
        streak = c._track("ssh", False, streak)
    assert streak == TRANSPORT_FAIL_DWELL

    caplog.clear()
    assert c._track("ssh", True, streak) == 0
    assert "recovered after" in caplog.text


async def test_a_flap_below_the_dwell_recovers_silently(
    hass, coordinator_factory, caplog
) -> None:
    c = coordinator_factory()
    streak = c._track("ssh", False, 0)
    caplog.clear()
    assert c._track("ssh", True, streak) == 0
    assert "recovered after" not in caplog.text


# --- the persisted clock ----------------------------------------------------


async def test_nothing_stored_leaves_the_clock_empty(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with patch.object(c._store, "async_load", AsyncMock(return_value=None)):
        await c.async_load_pending()
    assert c._pending_since == {}
    assert c.install_applied_at is None


async def test_a_stored_payload_is_restored(hass, coordinator_factory) -> None:
    when = dt_util.utcnow().replace(microsecond=0)
    payload = {
        STORE_SHAPE_KEY: STORE_SHAPE,
        "pending": {"libssl3": when.isoformat()},
        "install_applied_at": when.isoformat(),
    }
    c = coordinator_factory()
    with patch.object(c._store, "async_load", AsyncMock(return_value=payload)):
        await c.async_load_pending()
    assert c._pending_since == {"libssl3": when.isoformat()}
    assert c.install_applied_at == when


async def test_an_unrecognised_shape_is_ignored_not_reinterpreted(
    hass, coordinator_factory, caplog
) -> None:
    """Losing the clock is recoverable; misreading someone else's payload as
    package names is not."""
    c = coordinator_factory()
    payload = {STORE_SHAPE_KEY: 99, "pending": {"whatever": "x"}}
    with patch.object(c._store, "async_load", AsyncMock(return_value=payload)):
        await c.async_load_pending()
    assert c._pending_since == {}
    assert "expected" in caplog.text


async def test_a_payload_that_is_not_a_mapping_is_ignored(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with patch.object(c._store, "async_load", AsyncMock(return_value=["nope"])):
        await c.async_load_pending()
    assert c._pending_since == {}


async def test_both_halves_go_out_in_one_write(hass, coordinator_factory) -> None:
    """Two async_save calls against one Store race, and the loser silently
    drops the other half."""
    c = coordinator_factory()
    when = dt_util.utcnow()
    with patch.object(c._store, "async_save", AsyncMock()) as save:
        await c.async_set_install_applied(when)

    save.assert_awaited_once()
    payload = save.await_args.args[0]
    assert payload[STORE_SHAPE_KEY] == STORE_SHAPE
    assert "pending" in payload
    assert payload["install_applied_at"] == when.isoformat()


async def test_clearing_the_applied_timestamp_persists_too(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with patch.object(c._store, "async_save", AsyncMock()) as save:
        await c.async_set_install_applied(None)
    assert save.await_args.args[0]["install_applied_at"] is None


# --- the slow block's cadence -----------------------------------------------


async def test_the_slow_block_runs_on_the_first_poll(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", ssh_answers()):
        data = await c._async_update_data()
    assert data["slow"]["UPGRADABLE"] == "4"
    assert c._slow_at is not None


async def test_the_slow_block_is_not_re_run_every_minute(
    hass, coordinator_factory
) -> None:
    """apt list --upgradable costs real time and a host's patch state does not
    move minute to minute; its reachability does."""
    c = coordinator_factory()
    calls: list[str] = []

    async def counting(script, timeout):  # noqa: ARG001
        calls.append("slow" if "KERNEL_INSTALLED" in script else "fast")
        return 0, SLOW_OUT if "KERNEL_INSTALLED" in script else FAST_OUT, ""

    with patch.object(c, "_ssh_raw", counting):
        await c._async_update_data()
        await c._async_update_data()

    assert calls.count("fast") == 2
    assert calls.count("slow") == 1, "the second poll must reuse the cached read"


async def test_an_unreadable_slow_block_empties_it_rather_than_going_stale(
    hass, coordinator_factory, caplog
) -> None:
    """Serving the previous numbers for six more hours is the stale-zero shape
    in its most misleading form."""
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", ssh_answers()):
        await c._async_update_data()
    assert c._slow

    c._slow_at = dt_util.utcnow() - SLOW_INTERVAL - timedelta(minutes=1)
    caplog.clear()

    async def slow_fails(script, timeout):  # noqa: ARG001
        if "KERNEL_INSTALLED" in script:
            return 255, "", "No route to host"
        return 0, FAST_OUT, ""

    with patch.object(c, "_ssh_raw", slow_fails):
        data = await c._async_update_data()

    assert data["slow"] == {}
    assert c._slow_failed is True
    assert "apt and kernel" in caplog.text


async def test_a_failed_slow_block_retries_sooner_and_says_when_it_recovers(
    hass, coordinator_factory, caplog
) -> None:
    c = coordinator_factory()
    c._slow_failed = True
    c._slow_at = dt_util.utcnow() - SLOW_RETRY_INTERVAL - timedelta(minutes=1)
    caplog.clear()

    with patch.object(c, "_ssh_raw", ssh_answers()):
        data = await c._async_update_data()

    assert data["slow"]["UPGRADABLE"] == "4"
    assert c._slow_failed is False
    assert "readable again" in caplog.text


async def test_the_slow_block_is_skipped_when_the_host_is_not_answering(
    hass, coordinator_factory
) -> None:
    """No point paying for apt against a host whose fast block just failed."""
    c = coordinator_factory()
    calls: list[str] = []

    async def fast_fails(script, timeout):  # noqa: ARG001
        calls.append("slow" if "KERNEL_INSTALLED" in script else "fast")
        return 255, "", "No route to host"

    with patch.object(c, "_ssh_raw", fast_fails):
        await c._async_update_data()

    assert "slow" not in calls


async def test_invalidate_slow_forces_the_next_poll_to_re_read(
    hass, coordinator_factory
) -> None:
    """After a patch run the cached numbers describe the host BEFORE the
    upgrade -- a pending count the operator had just cleared."""
    c = coordinator_factory()
    with patch.object(c, "_ssh_raw", ssh_answers()):
        await c._async_update_data()
    assert c._slow

    c.invalidate_slow()
    assert c._slow == {} and c._slow_at is None and c._slow_failed is False


async def test_restore_reboot_count_is_taken_verbatim(
    hass, coordinator_factory
) -> None:
    c = coordinator_factory()
    c.restore_reboot_count(12)
    assert c.reboot_count == 12
