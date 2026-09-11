"""_probe: the one function `test-before-configure` is actually about.

Every other config-flow test patches this out, which is right for those tests
and leaves the interesting part unexercised. What matters here is not that a
probe returns a hostname -- it is that it goes out over THE ENTRY'S OWN
CREDENTIAL and that every way of failing lands on None rather than on
something a caller would store.

The worked example is what happens when it does not: a flow that
asked a Glances daemon for a hostname while the integration polled over ssh
created an entry against an account that did not exist on that machine, read
healthy for weeks on the other channel, and went dark the day the daemon
broke.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.linux_monitor.config_flow import (
    _probe,
    _probe_hostname,
    _probe_sudo,
)
from custom_components.linux_monitor.const import (
    DEFAULT_KNOWN_HOSTS,
    SSH_FAST_TIMEOUT,
)

HOST = "203.0.113.5"
USER = "monitor"
KEY = "/config/.ssh/test_key"
SCRIPT = 'echo "HOSTNAME=$(hostname)"\necho "__END__"\n'


def _proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = rc
    proc.wait = AsyncMock()
    return proc


async def test_the_probe_goes_out_over_the_entrys_own_credential() -> None:
    """THE RULE, asserted on the argv rather than on the answer. A probe that
    reaches the right host by the wrong channel certifies nothing."""
    proc = _proc(b"HOSTNAME=testhost\n__END__\n")
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        parsed = await _probe(HOST, USER, KEY, SCRIPT, SSH_FAST_TIMEOUT)

    assert parsed == {"HOSTNAME": "testhost"}
    argv = spawn.call_args.args
    assert argv[0] == "ssh"
    assert "-i" in argv and KEY in argv, "the entry's own key"
    assert f"{USER}@{HOST}" in argv, "the entry's own account and address"
    assert "BatchMode=yes" in argv, "must never block on a prompt"
    assert f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}" in argv
    assert SCRIPT in argv


async def test_a_spawn_failure_is_none() -> None:
    """ssh missing from the container, or fork failing."""
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("no ssh")):
        assert await _probe(HOST, USER, KEY, SCRIPT, SSH_FAST_TIMEOUT) is None


async def test_a_timeout_is_none_and_the_process_is_killed() -> None:
    """A probe left running would hold the config flow open indefinitely."""
    proc = _proc()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _probe(HOST, USER, KEY, SCRIPT, 1) is None
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_a_non_zero_exit_is_none() -> None:
    """Refused login, no such account, unreachable -- ssh says so in the code
    and none of them is a result worth storing."""
    proc = _proc(b"", b"Permission denied (publickey).", rc=255)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _probe(HOST, USER, KEY, SCRIPT, SSH_FAST_TIMEOUT) is None


async def test_a_reply_without_the_end_marker_is_none() -> None:
    """SAME CONTRACT AS THE COORDINATOR'S. A truncated read must not present
    as a read that found nothing -- here it would present as a host with no
    name, which the caller would then refuse for the wrong reason."""
    proc = _proc(b"HOSTNAME=testhost\n")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _probe(HOST, USER, KEY, SCRIPT, SSH_FAST_TIMEOUT) is None


async def test_an_empty_but_complete_reply_parses_to_an_empty_mapping() -> None:
    """Distinct from None: the host answered, it just said nothing."""
    proc = _proc(b"__END__\n")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _probe(HOST, USER, KEY, SCRIPT, SSH_FAST_TIMEOUT) == {}


async def test_undecodable_output_does_not_raise() -> None:
    """A host mid-boot can emit anything at all on that channel."""
    proc = _proc(b"HOSTNAME=caf\xff\n__END__\n")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        parsed = await _probe(HOST, USER, KEY, SCRIPT, SSH_FAST_TIMEOUT)
    assert parsed is not None and "HOSTNAME" in parsed


# --- the two callers --------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "expected", "why"),
    [
        (b"HOSTNAME=testhost\n__END__\n", "testhost", "the host's own name"),
        (b"HOSTNAME=\n__END__\n", None, "an empty name is not a name"),
        (b"HOSTNAME=   \n__END__\n", None, "nor is whitespace"),
        (b"__END__\n", None, "nor is a missing key"),
        # The probe ITSELF failing, rather than answering with no name: no end
        # marker means a truncated read, and the caller must not be handed a
        # blank hostname it would then refuse for the wrong reason.
        (b"HOSTNAME=testhost\n", None, "a truncated reply is not a name"),
    ],
)
async def test_probe_hostname(stdout, expected, why) -> None:
    proc = _proc(stdout)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _probe_hostname(HOST, USER, KEY) == expected, why


@pytest.mark.parametrize(
    ("stdout", "rc", "expected", "why"),
    [
        (b"SUDO=1\n__END__\n", 0, True, "granted"),
        (b"SUDO=0\n__END__\n", 0, False, "refused"),
        (b"__END__\n", 0, False, "absent is not granted"),
        (b"SUDO=1\n", 0, False, "a truncated yes is not a yes"),
        (b"", 255, False, "AN UNREACHABLE HOST IS NOT A YES"),
    ],
)
async def test_probe_sudo(stdout, rc, expected, why) -> None:
    """False on EVERY failure, transport included. The question being answered
    is 'may this option be stored as True', and a host that could not be asked
    has not said yes."""
    proc = _proc(stdout, rc=rc)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _probe_sudo(HOST, USER, KEY) is expected, why
