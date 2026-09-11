"""The health sensor's rungs.

Two rung classes, and the difference between them is the whole design: an
ABSENT reading dwells, because it might be a flap that heals itself, while a
KNOWN-BAD reading trips at once, because the host answered and the answer was
wrong. A regression that collapses them does not raise -- it just makes the
sensor slow to notice a full disk, or noisy about a reboot.
"""

from __future__ import annotations

import pytest

from custom_components.linux_monitor.binary_sensor import LinuxMonitorHealth
from custom_components.linux_monitor.const import TRANSPORT_FAIL_DWELL

ONLINE = {
    "offline_expected": False,
    "auth_failed": False,
    "online": True,
    "ssh_ok": True,
    "ssh_fails": 0,
    "metrics": {"cpu_percent": 5.0, "fs": [{"mnt_point": "/", "percent": 40.0}]},
    "slow": {},
}


@pytest.fixture
def health(coordinator_factory):
    def _make(**over):
        c = coordinator_factory()
        c.data = {**ONLINE, **over}
        return LinuxMonitorHealth(c)

    return _make


def test_a_healthy_host_is_off_with_no_reasons(health) -> None:
    entity = health()
    assert entity.is_on is False
    assert entity.extra_state_attributes["reasons"] == []
    assert entity.extra_state_attributes["disposition"] == "ok"


def test_it_never_goes_unavailable(health) -> None:
    """The contract this entity exists for."""
    assert health(online=False, ssh_fails=99).available is True
    assert health(offline_expected=True).available is True


def test_a_missed_poll_below_the_dwell_is_a_flap_not_a_fault(health) -> None:
    """Three minutes is longer than a routine reboot or one transient ssh
    timeout, and reporting those would train the household to ignore it."""
    entity = health(online=False, ssh_fails=TRANSPORT_FAIL_DWELL - 1)
    assert entity.is_on is False
    assert entity.extra_state_attributes["disposition"] == "transport_flap"


def test_at_the_dwell_it_reports_unreachable_and_names_the_count(health) -> None:
    entity = health(online=False, ssh_fails=TRANSPORT_FAIL_DWELL)
    assert entity.is_on is True
    assert entity.extra_state_attributes["reasons"] == [
        f"unreachable:{TRANSPORT_FAIL_DWELL}_polls"
    ]
    assert entity.extra_state_attributes["disposition"] == "unreachable"


def test_a_refused_credential_trips_without_waiting_for_the_dwell(health) -> None:
    """A KNOWN-BAD reading, not an absent one: the host was reached and it
    refused us. A revoked key does not heal itself, so waiting the extra poll
    only delays the one message that names the cause (#13)."""
    entity = health(online=False, auth_failed=True, ssh_fails=1)
    assert entity.is_on is True
    assert entity.extra_state_attributes["reasons"] == ["ssh_auth_failed:1_polls"]
    assert entity.extra_state_attributes["disposition"] == "auth_failed"
    assert entity.extra_state_attributes["auth_failed"] is True


def test_auth_failed_is_named_separately_from_unreachable(health) -> None:
    """The two send an operator to different places, and reading one as the
    other is the defect #13 was filed for."""
    auth = health(online=False, auth_failed=True, ssh_fails=TRANSPORT_FAIL_DWELL)
    unreachable = health(online=False, ssh_fails=TRANSPORT_FAIL_DWELL)
    assert auth.extra_state_attributes["disposition"] == "auth_failed"
    assert unreachable.extra_state_attributes["disposition"] == "unreachable"
    assert auth.extra_state_attributes["reasons"] != (
        unreachable.extra_state_attributes["reasons"]
    )


def test_a_full_filesystem_trips_immediately_and_names_the_mount(health) -> None:
    entity = health(
        metrics={"cpu_percent": 5.0, "fs": [{"mnt_point": "/", "percent": 95.5}]}
    )
    assert entity.is_on is True
    assert entity.extra_state_attributes["reasons"] == ["disk:/:95.5"]
    assert entity.extra_state_attributes["disposition"] == "problem"


def test_every_real_filesystem_is_checked_not_just_root(health) -> None:
    """findmnt --real gives / and the boot partition on these hosts; Glances
    used to return / four times over and never mention /boot at all."""
    entity = health(
        metrics={
            "cpu_percent": 5.0,
            "fs": [
                {"mnt_point": "/", "percent": 40.0},
                {"mnt_point": "/boot", "percent": 97.0},
            ],
        }
    )
    assert entity.extra_state_attributes["reasons"] == ["disk:/boot:97.0"]


def test_a_pegged_cpu_trips(health) -> None:
    entity = health(
        metrics={"cpu_percent": 99.0, "fs": [{"mnt_point": "/", "percent": 10.0}]}
    )
    assert entity.extra_state_attributes["reasons"] == ["cpu:99.0"]


def test_an_unreadable_metric_is_not_a_problem(health) -> None:
    """None must never compare as over-threshold. A monitor that reports a
    fault because it could not read is worse than one that says nothing."""
    entity = health(metrics={"cpu_percent": None, "fs": None})
    assert entity.is_on is False
    assert entity.extra_state_attributes["reasons"] == []


def test_an_offline_expected_host_scores_clean_even_when_stale(health) -> None:
    """A host that is meant to be off is not a fault, and the flag wins over
    every other rung."""
    entity = health(offline_expected=True, online=False, ssh_fails=99)
    assert entity.is_on is False
    assert entity.extra_state_attributes["reasons"] == []
    assert entity.extra_state_attributes["disposition"] == "offline_expected"


def test_the_attributes_carry_the_dwell_so_a_surface_can_explain_itself(
    health,
) -> None:
    attrs = health().extra_state_attributes
    assert attrs["transport_fail_dwell"] == TRANSPORT_FAIL_DWELL
    assert attrs["host"]
    assert attrs["ssh_missed_polls"] == 0
