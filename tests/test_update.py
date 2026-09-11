"""The update entity: refusals, and the artefact a successful run leaves.

Every refusal RAISES rather than returning quietly -- a patch control that
declines silently leaves the operator believing the host was patched. #8 is
the other half: after a successful run the host reboots and takes /tmp with
it, so the log has to land somewhere that survives and its tail has to reach
the entity.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.linux_monitor.const import (
    CONF_ALLOW_INSTALL,
    CONF_OFFLINE_EXPECTED,
)
from custom_components.linux_monitor.coordinator import SSH_OK
from custom_components.linux_monitor.update import LinuxMonitorUpdate

LOG_PATH = "/home/monitor/.cache/linux_monitor/apt-upgrade.log"

APT_OK = (
    "APT_RC=0\n"
    f"LOG_PATH={LOG_PATH}\n"
    "REMOVED=0\n"
    "KEPT_BACK=0\n"
    "REMAINING=1\n"
    "TAIL=Setting up libssl3 (3.5.2) ... Processing triggers for man-db ...\n"
    "__END__\n"
)
APT_FAILED = (
    "APT_RC=100\n"
    f"LOG_PATH={LOG_PATH}\n"
    "REMOVED=0\n"
    "KEPT_BACK=0\n"
    "REMAINING=4\n"
    "TAIL=E: Could not get lock /var/lib/dpkg/lock-frontend\n"
    "__END__\n"
)

DATA_SLOW = {
    "UPGRADABLE": "4",
    "SECURITY": "2",
    "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
    "SECURITY_PKGS": "libssl3,curl",
}

POLLED = {
    "offline_expected": False,
    "auth_failed": False,
    "online": True,
    "ssh_ok": True,
    "ssh": {"UNAME": "6.12.101+deb13-amd64"},
    "metrics": {"uptime_secs": 9000.0},
    "slow": DATA_SLOW,
    "hostname_configured": "testhost",
    "ssh_fails": 0,
}


@pytest.fixture
def entity(coordinator_factory):
    def _make(*, options=None, data=None):
        c = coordinator_factory(
            options={CONF_ALLOW_INSTALL: True, **(options or {})}
        )
        c.data = {**POLLED, **(data or {})}
        e = LinuxMonitorUpdate(c)
        # Not added to a platform: async_write_ha_state has nothing to write
        # to, and none of the behaviour under test depends on it.
        e.async_write_ha_state = lambda: None
        return e, c

    return _make


# --- refusals ---------------------------------------------------------------


async def test_it_refuses_an_offline_expected_host(hass, entity) -> None:
    """Its patch state is UNKNOWN, not current -- the flag says it was never
    asked, which is not a claim that it is patched."""
    e, c = entity(
        options={CONF_OFFLINE_EXPECTED: True},
        data={"offline_expected": True},
    )
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="offline_expected"):
            await e.async_install(None, False)
    ex.assert_not_called()


async def test_it_refuses_a_host_that_has_not_opted_in(hass, entity) -> None:
    e, c = entity(options={CONF_ALLOW_INSTALL: False})
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="opted in"):
            await e.async_install(None, False)
    ex.assert_not_called()


async def test_it_refuses_a_host_that_has_not_reported(hass, entity) -> None:
    """Refusing to upgrade a host whose current state is unknown."""
    e, c = entity(data={"slow": {}})
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="not reported"):
            await e.async_install(None, False)
    ex.assert_not_called()


async def test_it_refuses_when_nothing_is_pending(hass, entity) -> None:
    e, c = entity(
        data={
            "slow": {
                "UPGRADABLE": "0",
                "SECURITY": "0",
                "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
            }
        }
    )
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="nothing pending"):
            await e.async_install(None, False)
    ex.assert_not_called()


async def test_it_refuses_a_second_concurrent_run(hass, entity) -> None:
    """_attr_in_progress greys the control in the UI; it does not stop a
    service call arriving by another route."""
    e, c = entity()
    c.install_in_progress = True
    with patch.object(c, "async_exec", AsyncMock()) as ex:
        with pytest.raises(HomeAssistantError, match="already mid"):
            await e.async_install(None, False)
    ex.assert_not_called()


# --- the run ----------------------------------------------------------------


async def test_a_successful_run_records_the_log_path_and_tail_then_reboots(
    hass, entity
) -> None:
    e, c = entity()
    calls: list[tuple] = []

    async def fake_exec(script, timeout):
        calls.append((script, timeout))
        if len(calls) == 1:
            return True, APT_OK, SSH_OK
        return True, "", SSH_OK  # the chained reboot

    with (
        patch.object(c, "async_exec", side_effect=fake_exec),
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        await e.async_install(None, False)

    assert len(calls) == 2, "the reboot is chained on exit 0"
    # #8: the artefact survives the reboot, and its LOCATION comes back from
    # the host rather than being assumed.
    assert c.last_install_log == LOG_PATH
    assert not c.last_install_log.startswith("/tmp/")
    assert "libssl3" in c.last_install_tail
    assert c.install_applied_at is not None
    assert c.install_in_progress is False, "the flag must be cleared"

    attrs = e.extra_state_attributes
    assert attrs["last_install_log"] == LOG_PATH
    assert "libssl3" in attrs["last_install_tail"]


async def test_a_tail_carrying_carriage_returns_reaches_the_attribute_whole(
    hass, entity
) -> None:
    r"""#20, AT THE ENTITY. The pipeline collapses \r before the value is
    echoed, so this shape should not arrive any more -- but every fixture in
    this suite was hand-written with clean \n, which is precisely why the
    truncation reached production unseen. A \r in a value is now the host's
    data and survives to the attribute rather than cutting it at 21
    characters.
    """
    e, c = entity()
    raw = (
        "APT_RC=0\n"
        f"LOG_PATH={LOG_PATH}\n"
        "REMOVED=0\n"
        "KEPT_BACK=0\n"
        "REMAINING=0\n"
        "TAIL=(Reading database ...\r(Reading database ... 45%\r"
        "Setting up libssl3:amd64 (3.5.2) ...\n"
        "__END__\n"
    )
    with (
        patch.object(c, "async_exec", AsyncMock(return_value=(True, raw, SSH_OK))),
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        await e.async_install(None, False)

    assert c.last_install_tail == (
        "(Reading database ...\r(Reading database ... 45%\r"
        "Setting up libssl3:amd64 (3.5.2) ..."
    )
    assert c.last_install_log == LOG_PATH


async def test_a_failed_apt_run_raises_does_not_reboot_and_keeps_its_words(
    hass, entity
) -> None:
    """The tail is recorded BEFORE the return-code check, because a failed run
    is the one an operator most needs the words from."""
    e, c = entity()
    with (
        patch.object(
            c, "async_exec", AsyncMock(return_value=(True, APT_FAILED, SSH_OK))
        ) as ex,
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        with pytest.raises(HomeAssistantError, match="exited 100"):
            await e.async_install(None, False)
    assert ex.call_count == 1, "a non-zero apt exit must not chain a reboot"
    assert c.last_install_log == LOG_PATH
    assert "dpkg/lock-frontend" in c.last_install_tail
    assert c.install_in_progress is False


async def test_a_truncated_read_says_the_host_may_be_part_configured(
    hass, entity
) -> None:
    """No end marker means the transport died mid-dpkg. Saying 'nothing ran'
    would be a guess, and the dangerous one."""
    e, c = entity()
    with (
        patch.object(
            c, "async_exec", AsyncMock(return_value=(True, "APT_RC=0\n", SSH_OK))
        ),
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        with pytest.raises(HomeAssistantError, match="part-configured"):
            await e.async_install(None, False)
    assert c.install_in_progress is False


async def test_a_previous_runs_tail_is_never_read_as_this_ones(
    hass, entity
) -> None:
    """Cleared BEFORE the run. It would read as this run's most convincingly
    in exactly the case the attribute exists for -- a run that returned
    nothing."""
    e, c = entity()
    c.last_install_tail = "words from the run before"
    c.last_install_log = "/somewhere/old.log"
    with (
        patch.object(c, "async_exec", AsyncMock(return_value=(False, "", SSH_OK))),
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        with pytest.raises(HomeAssistantError):
            await e.async_install(None, False)
    assert c.last_install_tail is None
    assert c.last_install_log is None


# --- readings ---------------------------------------------------------------


async def test_an_unreported_host_reads_unknown_never_up_to_date(
    hass, entity
) -> None:
    e, _c = entity(data={"slow": {}, "ssh": {}})
    assert e.installed_version is None
    assert e.latest_version is None


async def test_a_stale_kernel_owes_a_reboot(hass, entity) -> None:
    """uname is RAM and dpkg is disk: remediated on disk, still exploitable in
    memory."""
    e, _c = entity(
        data={
            "ssh": {"UNAME": "6.12.100+deb13-amd64"},
            "slow": {
                "UPGRADABLE": "0",
                "SECURITY": "0",
                "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
            },
        }
    )
    assert e._reboot_owed() is True


async def test_a_library_upgrade_that_predates_the_boot_owes_nothing(
    hass, entity
) -> None:
    e, c = entity()
    c.install_applied_at = dt_util.utcnow() - timedelta(hours=6)
    # uptime 9000s ~ 2.5h, so the host booted AFTER the upgrade landed.
    assert e._reboot_owed() is False


async def test_an_upgrade_the_host_has_not_booted_since_owes_a_reboot(
    hass, entity
) -> None:
    """A plain library upgrade leaves the superseded code mapped in every
    process already running, so this is not kernel-only."""
    e, c = entity()
    c.install_applied_at = dt_util.utcnow() - timedelta(minutes=5)
    assert e._reboot_owed() is True


async def test_an_upgrade_with_no_readable_boot_time_is_not_assumed_rebooted(
    hass, entity
) -> None:
    e, c = entity(data={"metrics": {}})
    c.install_applied_at = dt_util.utcnow() - timedelta(hours=6)
    assert e._reboot_owed() is True


async def test_an_offline_host_is_dispositioned_not_called_patched(
    hass, entity
) -> None:
    """Equal versions so it scores as nothing-pending, with a disposition
    saying it was never asked."""
    e, _c = entity(
        options={CONF_OFFLINE_EXPECTED: True}, data={"offline_expected": True}
    )
    assert e.installed_version == e.latest_version
    attrs = e.extra_state_attributes
    assert attrs["disposition"] == "offline_expected"
    assert attrs["updates_pending"] is None
    assert attrs["last_install_log"] is None


# --- the published version strings ------------------------------------------


def test_an_offline_host_is_never_called_up_to_date(entity) -> None:
    """installed == latest so it scores as nothing-pending, and the summary
    says plainly that it is a disposition rather than a patch state."""
    e, _c = entity(
        options={CONF_OFFLINE_EXPECTED: True}, data={"offline_expected": True}
    )
    assert e.installed_version == e.latest_version
    assert "does not mean current" in e.release_summary


def test_an_unread_host_reads_unknown_and_says_so(entity) -> None:
    e, _c = entity(data={"slow": {}, "ssh": {}})
    assert e.latest_version is None
    assert e.release_summary == "Patch state unknown — the host did not answer."


def test_a_current_host_publishes_the_running_kernel_as_latest(entity) -> None:
    e, _c = entity(
        data={
            "ssh": {"UNAME": "6.12.101+deb13-amd64"},
            "slow": {
                "UPGRADABLE": "0",
                "SECURITY": "0",
                "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
            },
        }
    )
    assert e.latest_version == e.installed_version
    assert e.release_summary == "Up to date, and running the newest installed kernel."


def test_pending_packages_are_counted_into_the_target(entity) -> None:
    e, _c = entity()
    assert e.latest_version == "6.12.101+deb13-amd64 +4 pkg (2 security)"
    summary = e.release_summary
    assert "4 package(s) upgradable" in summary
    assert "2 from a security archive" in summary


def test_pending_without_security_omits_the_security_clause(entity) -> None:
    e, _c = entity(
        data={
            "slow": {
                "UPGRADABLE": "3",
                "SECURITY": "0",
                "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
            }
        }
    )
    assert e.latest_version == "6.12.101+deb13-amd64 +3 pkg"
    assert "security archive" not in e.release_summary


def test_a_stale_kernel_is_named_in_the_summary(entity) -> None:
    """Remediated on disk, still exploitable in memory -- uname is RAM and
    dpkg is disk."""
    e, _c = entity(
        data={
            "ssh": {"UNAME": "6.12.100+deb13-amd64"},
            "slow": {
                "UPGRADABLE": "0",
                "SECURITY": "0",
                "KERNEL_INSTALLED": "6.12.101+deb13-amd64",
            },
        }
    )
    assert "reboot required" in e.release_summary


@pytest.mark.parametrize(
    ("raw", "expected", "why"),
    [
        ("1", True, "installed"),
        ("0", False, "absent from the host"),
        (None, None, "NOBODY ASKED -- never False"),
        ("", None, "an empty answer is not a no"),
        ("nonsense", None, "unparseable is not a no"),
    ],
)
def test_unattended_upgrades_reporting(entity, raw, expected, why) -> None:
    """'nothing is auto-patching here' is a claim about the host, and the host
    is the only thing that can answer it."""
    slow = dict(DATA_SLOW)
    if raw is not None:
        slow["UNATTENDED"] = raw
    e, _c = entity(data={"slow": slow})
    assert e.extra_state_attributes["unattended_upgrades"] is expected, why


def test_the_update_entity_never_goes_unavailable(entity) -> None:
    """An unknown patch state must be SAID, and an unavailable entity's
    attributes vanish with it."""
    e, _c = entity(data={"online": False, "ssh": {}, "slow": {}})
    assert e.available is True


def test_install_is_only_advertised_where_the_grant_exists(entity) -> None:
    from homeassistant.components.update import UpdateEntityFeature

    allowed, _c = entity()
    refused, _c2 = entity(options={CONF_ALLOW_INSTALL: False})
    assert allowed.supported_features & UpdateEntityFeature.INSTALL
    assert not (refused.supported_features & UpdateEntityFeature.INSTALL)


# --- the two findings a successful run can still carry ----------------------


async def test_a_removal_during_upgrade_is_said_out_loud(
    hass, entity, caplog
) -> None:
    """apt-get upgrade is not supposed to be able to remove a package. If it
    ever does, that is a finding, not a statistic."""
    e, c = entity()
    out = APT_OK.replace("REMOVED=0", "REMOVED=2")
    with (
        patch.object(c, "async_exec", AsyncMock(return_value=(True, out, SSH_OK))),
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        await e.async_install(None, False)
    assert "REMOVED 2 package(s)" in caplog.text


async def test_packages_kept_back_are_reported_not_escalated(
    hass, entity, caplog
) -> None:
    """Escalating to full-upgrade can remove packages on a host with no
    console, so they are reported and left."""
    e, c = entity()
    out = APT_OK.replace("KEPT_BACK=0", "KEPT_BACK=1")
    with (
        patch.object(c, "async_exec", AsyncMock(return_value=(True, out, SSH_OK))),
        patch.object(c, "_save", AsyncMock()),
        patch.object(c, "async_request_refresh", AsyncMock()),
    ):
        await e.async_install(None, False)
    assert "kept back" in caplog.text
    assert "Not escalating" in caplog.text
