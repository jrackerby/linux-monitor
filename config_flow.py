"""Config flow for Linux Monitor.

No DHCP step -- see const.py. Every host is added by hand.

GH-470: the Glances port field is gone, and so is the Glances probe behind
it. THE PROBE NOW USES THE SSH CREDENTIAL THE ENTRY WILL ACTUALLY POLL WITH,
which is not merely a port change -- it closes the hole that produced the
failure this whole change came out of. The old flow asked a Glances daemon
for the hostname and never touched ssh, so prodhost01 was created against a
`monitor` account that does not exist on that machine, the entry validated
green, and the defect only surfaced weeks later when the daemon it WAS
relying on broke. A config flow that does not exercise the transport is a
config flow that certifies nothing.

THE OPTIONS FLOW APPLIES THE SAME RULE TO THE SUDO GRANT. Turning on
allow_install is a claim that this entry's SSH account may patch and reboot
the host; it is proven with `sudo -n true` over that same account, key and
address before the option is stored. The default account is a read-only one
with no sudo at all, so without the probe the first evidence would be an
apt run failing at the moment an operator believed a host was being patched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_ALLOW_INSTALL,
    CONF_HOST,
    CONF_HOSTNAME,
    CONF_OFFLINE_EXPECTED,
    CONF_SSH_KEY,
    CONF_SSH_USER,
    DEFAULT_KNOWN_HOSTS,
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_USER,
    DOMAIN,
    SSH_FAST_TIMEOUT,
    SUDO_PROBE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# Same strict KEY=value plus __END__ contract as the coordinator's blocks, for
# the same reason: a truncated read must not read as a read that found nothing.
_PROBE_CMD = 'echo "HOSTNAME=$(hostname)"\necho "__END__"\n'

# What proves the SUDO half of the credential, run over the same account, key
# and host the privileged blocks will use. It is deliberately `true` and not a
# harmless-looking apt call: this must answer the permission question and
# change nothing while doing so.
_SUDO_PROBE_CMD = 'sudo -n true 2>/dev/null && echo "SUDO=1" || echo "SUDO=0"\necho "__END__"\n'


async def _probe(
    host: str, ssh_user: str, ssh_key: str, script: str, timeout: int
) -> dict[str, str] | None:
    """Run one KEY=value block over the ssh credential this entry will use.

    None on any failure -- unreachable, wrong key, no such account, refused
    login, or a reply with no end marker -- because a truncated read must not
    read as a read that found nothing. Every one of those is a reason not to
    store what the caller was about to store.
    """
    argv = [
        "ssh", "-i", ssh_key,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        f"{ssh_user}@{host}",
        script,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as err:
        _LOGGER.debug("%s: probe ssh spawn failed: %s", host, err)
        return None
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.debug("%s: probe ssh timed out", host)
        return None

    if proc.returncode != 0:
        _LOGGER.debug("%s: probe ssh rc=%s: %s", host, proc.returncode,
                      stderr.decode(errors="replace").strip()[:200])
        return None

    out = stdout.decode(errors="replace")
    if "__END__" not in out:
        return None
    parsed: dict[str, str] = {}
    for line in out.splitlines():
        if line == "__END__":
            break
        key, sep, val = line.partition("=")
        if sep:
            parsed[key.strip()] = val.strip()
    return parsed


async def _probe_hostname(host: str, ssh_user: str, ssh_key: str) -> str | None:
    """The host's own name, or None on any failure the caller surfaces as
    cannot_connect."""
    parsed = await _probe(host, ssh_user, ssh_key, _PROBE_CMD, SSH_FAST_TIMEOUT)
    if parsed is None:
        return None
    return (parsed.get("HOSTNAME") or "").strip() or None


async def _probe_sudo(host: str, ssh_user: str, ssh_key: str) -> bool:
    """Whether this entry's own SSH account has passwordless sudo.

    FALSE ON EVERY FAILURE, including a transport failure. The question being
    answered is "may this option be stored as True", and an unreadable host is
    not a yes. Storing the grant on a host that could not be asked is exactly
    the shape LAW 9 names: a green check taken on a channel other than the one
    that will be used, which certifies nothing and certifies it green.
    """
    parsed = await _probe(
        host, ssh_user, ssh_key, _SUDO_PROBE_CMD, SUDO_PROBE_TIMEOUT
    )
    if parsed is None:
        return False
    return parsed.get("SUDO") == "1"


class LinuxMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    # 2: glances_port dropped from entry data (GH-470). See
    # __init__.async_migrate_entry.
    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            offline = user_input[CONF_OFFLINE_EXPECTED]
            ssh_user = user_input[CONF_SSH_USER]
            ssh_key = user_input[CONF_SSH_KEY]
            hostname = (user_input.get(CONF_HOSTNAME) or "").strip()

            if not hostname and not offline:
                hostname = await _probe_hostname(host, ssh_user, ssh_key)

            if not hostname:
                if offline:
                    errors[CONF_HOSTNAME] = "hostname_required_offline"
                else:
                    errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(hostname.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=hostname,
                    data={
                        CONF_HOST: host,
                        CONF_HOSTNAME: hostname,
                        CONF_SSH_USER: ssh_user,
                        CONF_SSH_KEY: ssh_key,
                    },
                    options={CONF_OFFLINE_EXPECTED: offline},
                )

        suggested = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=suggested.get(CONF_HOST, "")): str,
                vol.Optional(CONF_HOSTNAME, default=suggested.get(CONF_HOSTNAME, "")): str,
                vol.Required(
                    CONF_SSH_USER, default=suggested.get(CONF_SSH_USER, DEFAULT_SSH_USER)
                ): str,
                vol.Required(
                    CONF_SSH_KEY, default=suggested.get(CONF_SSH_KEY, DEFAULT_SSH_KEY)
                ): str,
                vol.Required(
                    CONF_OFFLINE_EXPECTED,
                    default=suggested.get(CONF_OFFLINE_EXPECTED, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # --- reauth -------------------------------------------------------------
    #
    # WHAT THIS IS FOR. ssh has no password to expire, so it is easy to assume
    # a key-based integration needs no reauth path at all. It needs one more
    # than a password integration does: a rotated key, a revoked
    # authorized_keys line and a deleted account all present as an unreachable
    # host, and before this the ONLY way to supply a new key was to delete the
    # entry and recreate it -- which drops this integration's .storage ledger
    # and with it every package's first-seen timestamp and the last applied
    # upgrade (#14). The recovery cost more than the fault.
    #
    # coordinator._check_auth starts this flow, on ssh's own permission-denied
    # and never on a timeout. See there for why it does not raise
    # ConfigEntryAuthFailed to do it.

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """PROBED BEFORE IT IS STORED, over the account, key and address the
        entry will actually poll with -- the same rule async_step_user follows
        and for the same reason. A reauth form that stores whatever it is
        handed is a config flow that certifies nothing, at the one moment the
        operator is most sure they have fixed it.

        THE HOST'S OWN NAME IS CHECKED TOO. A credential that works but
        answers to a different hostname is not this entry's host: the address
        was reassigned, or the key was put on the wrong machine. Storing it
        would leave the entry quietly monitoring something else under the old
        unique_id, which is worse than refusing.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            ssh_user = user_input[CONF_SSH_USER]
            ssh_key = user_input[CONF_SSH_KEY]
            reported = await _probe_hostname(
                entry.data[CONF_HOST], ssh_user, ssh_key
            )
            expected = str(entry.data.get(CONF_HOSTNAME) or "").strip()
            if reported is None:
                errors["base"] = "cannot_connect"
            elif expected and reported.strip().lower() != expected.lower():
                _LOGGER.warning(
                    "%s: reauth probe reached a host calling itself %s -- "
                    "refusing to repoint this entry",
                    expected, reported,
                )
                errors["base"] = "wrong_host"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_SSH_USER: ssh_user,
                        CONF_SSH_KEY: ssh_key,
                    },
                )

        suggested = user_input or entry.data
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SSH_USER,
                        default=suggested.get(CONF_SSH_USER, DEFAULT_SSH_USER),
                    ): str,
                    vol.Required(
                        CONF_SSH_KEY,
                        default=suggested.get(CONF_SSH_KEY, DEFAULT_SSH_KEY),
                    ): str,
                }
            ),
            description_placeholders={"host": entry.title},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return LinuxMonitorOptionsFlow()


class LinuxMonitorOptionsFlow(OptionsFlow):
    """ONE STEP, carrying BOTH options.

    Deliberately not split. async_create_entry(data=...) replaces
    entry.options wholesale, so a second step returning only its own keys
    would delete the first step's -- silently, with no edit to point at.
    While there is one step there is nothing to merge; if a second is ever
    added, both must merge over self.config_entry.options.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        data = self.config_entry.data

        if user_input is not None:
            wants_install = bool(user_input.get(CONF_ALLOW_INSTALL, False))
            had_install = bool(options.get(CONF_ALLOW_INSTALL, False))

            # PROBED ON THE TRANSITION TO TRUE, not on every save. Re-probing
            # an already-granted entry would let one unreachable host revoke a
            # grant that was proven when it mattered, turning a transient
            # network fault into a silent loss of the control. Turning it OFF
            # is never gated: withdrawing permission must always be possible,
            # including from a host that cannot be reached to confirm it.
            if wants_install and not had_install:
                ok = await _probe_sudo(
                    data[CONF_HOST],
                    data.get(CONF_SSH_USER, DEFAULT_SSH_USER),
                    data.get(CONF_SSH_KEY, DEFAULT_SSH_KEY),
                )
                if not ok:
                    errors[CONF_ALLOW_INSTALL] = "no_sudo"

            if not errors:
                return self.async_create_entry(data=user_input)

        suggested = user_input or options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OFFLINE_EXPECTED,
                        default=suggested.get(CONF_OFFLINE_EXPECTED, False),
                    ): bool,
                    vol.Required(
                        CONF_ALLOW_INSTALL,
                        default=suggested.get(CONF_ALLOW_INSTALL, False),
                    ): bool,
                }
            ),
            errors=errors,
        )
