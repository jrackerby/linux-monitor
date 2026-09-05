"""Config flow for Host Monitor.

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
"""

from __future__ import annotations

import asyncio
import logging
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
)

_LOGGER = logging.getLogger(__name__)

# Same strict KEY=value plus __END__ contract as the coordinator's blocks, for
# the same reason: a truncated read must not read as a read that found nothing.
_PROBE_CMD = 'echo "HOSTNAME=$(hostname)"\necho "__END__"\n'


async def _probe_hostname(
    host: str, ssh_user: str, ssh_key: str
) -> str | None:
    """Ask the host its own name over the ssh credential this entry will use.

    None on any failure -- unreachable, wrong key, no such account, refused
    login -- which the caller surfaces as cannot_connect. Every one of those
    is a reason not to create the entry.
    """
    argv = [
        "ssh", "-i", ssh_key,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        f"{ssh_user}@{host}",
        _PROBE_CMD,
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
        async with asyncio.timeout(SSH_FAST_TIMEOUT):
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
    for line in out.splitlines():
        key, sep, val = line.partition("=")
        if sep and key.strip() == "HOSTNAME":
            name = val.strip()
            return name or None
    return None


class HostMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
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

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return HostMonitorOptionsFlow()


class HostMonitorOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(CONF_OFFLINE_EXPECTED, False)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_OFFLINE_EXPECTED, default=current): bool}
            ),
        )
