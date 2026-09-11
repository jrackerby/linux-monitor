"""_classify_ssh: what KIND of failure ssh just had.

The whole of #13 rests on this function, and every case below is real OpenSSH
stderr text rather than an invented string -- the point of the classifier is
that ssh's exit code throws the distinction away and only its words keep it.
"""

from __future__ import annotations

import pytest

from custom_components.linux_monitor.coordinator import (
    SSH_AUTH,
    SSH_HOST_KEY,
    SSH_OK,
    SSH_REMOTE,
    SSH_TRANSPORT_LOST,
    SSH_UNREACHABLE,
    _classify_ssh,
)

CASES: list[tuple[int | None, str, str, str]] = [
    (0, "", SSH_OK, "clean exit"),
    (0, "Warning: something", SSH_OK, "rc 0 beats any stderr"),
    (None, "", SSH_UNREACHABLE, "spawn failure or our own timeout"),
    (1, "apt-get: command not found", SSH_REMOTE, "the remote command's own code"),
    (100, "E: Could not get lock", SSH_REMOTE, "apt exit 100 is not a credential fault"),
    (255, "monitor@host: Permission denied (publickey).", SSH_AUTH, "rotated or revoked key"),
    (255, "Permission denied, please try again.", SSH_AUTH, "password prompt refused"),
    (
        255,
        "Received disconnect from 203.0.113.5 port 22:2: Too many authentication failures",
        SSH_AUTH,
        "key never offered",
    ),
    (
        255,
        "Warning: Identity file /config/.ssh/k not accessible: No such file or "
        "directory.\nPermission denied (publickey).",
        SSH_AUTH,
        "key file gone from under Home Assistant",
    ),
    (
        255,
        "no such identity: /config/.ssh/k: No such file or directory",
        SSH_AUTH,
        "no such identity",
    ),
    (
        255,
        "WARNING: UNPROTECTED PRIVATE KEY FILE!\nPermissions 0644 are too open.\n"
        "Permission denied (publickey).",
        SSH_AUTH,
        "key mode ssh refuses to use",
    ),
    (255, "Host key verification failed.", SSH_HOST_KEY, "known_hosts mismatch"),
    (
        255,
        "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
        "Host key verification failed.\nPermission denied (publickey).",
        SSH_HOST_KEY,
        "host key is read BEFORE the auth line sitting behind it",
    ),
    (
        255,
        "Connection to 203.0.113.5 closed by remote host.",
        SSH_TRANSPORT_LOST,
        "THIS IS WHAT A SUCCESSFUL REBOOT LOOKS LIKE",
    ),
    (255, "client_loop: send disconnect: Broken pipe", SSH_TRANSPORT_LOST, "broken pipe"),
    (255, "Connection reset by peer", SSH_TRANSPORT_LOST, "reset"),
    (255, "ssh: connect to host h port 22: No route to host", SSH_UNREACHABLE, "no route"),
    (255, "ssh: connect to host h port 22: Connection refused", SSH_UNREACHABLE, "sshd down"),
    (255, "ssh: connect to host h port 22: Connection timed out", SSH_UNREACHABLE, "dead host"),
    (
        255,
        "ssh: Could not resolve hostname h: Name or service not known",
        SSH_UNREACHABLE,
        "dns",
    ),
    (255, "PERMISSION DENIED (PUBLICKEY).", SSH_AUTH, "matching is case-insensitive"),
    (
        255,
        "some wording no OpenSSH release has ever emitted",
        SSH_UNREACHABLE,
        "an unrecognised 255 is never AUTH -- a reauth card nobody can satisfy "
        "is worse than the reverse",
    ),
]


@pytest.mark.parametrize(("rc", "stderr", "expected", "why"), CASES)
def test_classify(rc: int | None, stderr: str, expected: str, why: str) -> None:
    assert _classify_ssh(rc, stderr) == expected, why


def test_a_non_255_exit_carrying_permission_denied_is_remote_not_auth() -> None:
    """The single most expensive misclassification available here.

    A remote script that prints "Permission denied" and exits 1 has NOT failed
    to authenticate -- the login worked. Reading it as AUTH would put a reauth
    card in front of the operator for something no key can fix, and would do
    it on every poll.
    """
    assert _classify_ssh(1, "cat: /etc/shadow: Permission denied") == SSH_REMOTE


def test_a_timeout_is_never_auth() -> None:
    """rc is None when ssh was killed at our own timeout or never spawned. ssh
    said nothing, so nothing may be concluded about the credential."""
    assert _classify_ssh(None, "timeout") == SSH_UNREACHABLE


def test_every_class_is_reachable() -> None:
    """A table whose expectations never exercise a branch would pass while
    that branch was dead. Assert the table covers all six."""
    covered = {expected for _rc, _err, expected, _why in CASES}
    assert covered == {
        SSH_OK,
        SSH_AUTH,
        SSH_UNREACHABLE,
        SSH_HOST_KEY,
        SSH_TRANSPORT_LOST,
        SSH_REMOTE,
    }
