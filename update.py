"""Update entity -- one per monitored host.

WHAT "CURRENT" MEANS HERE -- both halves, deliberately:

  1. Nothing pending from apt.
  2. The RUNNING kernel is the newest INSTALLED kernel of the running flavour.

The second half exists because uname is RAM and dpkg is disk. After a kernel
update they diverge until reboot: remediated on disk, still exploitable in
memory. Nothing closes that gap by itself, so an entity that ignored it would
call a host current while it ran an exploitable kernel.

/var/run/reboot-required is not used and must not be: update-notifier-common
is not installed everywhere, so it is absent on a stale host exactly as on a
current one -- a signal that reads identically in both states is not a signal.

REBOOT_REQUIRED IS NOT KERNEL-ONLY. A non-kernel upgrade can supersede
libraries a long-running process already has mapped -- a graphics stack under
a browser is the worked example -- so the old code keeps executing with the
card reading green. That is the same lie installed_version already refuses to
tell about uname vs dpkg, and it is refused the same way: by comparing the
host's boot time against the moment the upgrade landed.

UNKNOWN IS NOT UP TO DATE. When the slow block has not produced a reading,
latest_version is None and the entity reads unknown. A stale zero is not a
zero, it is an unanswered question.

AN INTENTIONALLY-OFFLINE HOST IS NOT UNKNOWN, AND IT IS NOT UNAVAILABLE. An
entity that vanishes with its host cannot describe the host being deliberately
off, and attributes vanish along with an unavailable entity -- so anything
downstream reading one would score it as nothing-to-see at exactly the moment
there was something. Both halves are needed: an available entity with None
versions still reads unknown, so both versions report OFFLINE_VERSION and the
pair compares equal. THE CLAIM IS NOT THAT THE HOST IS PATCHED. It is that it
was never asked, and release_summary plus the disposition attribute say so in
those words. The failure mode to watch is a host left flagged offline_expected
after it comes back, silently excluded from the patch count; disposition
exists so that is visible rather than inferred.

INSTALL IS OPT-IN PER ENTRY (const.py CONF_ALLOW_INSTALL) and the option is
not accepted until config_flow has proven sudo over this entry's own SSH
credential. A host that has not opted in does not ADVERTISE install at all --
the control is absent from the Updates panel rather than present and refusing.

NAMED "System updates", NOT "System". kiosk_pi registers update.<host>_system
against a device carrying the same name on the four kiosk hosts, and Home
Assistant mints an entity_id from device name plus entity name: a colliding id
is taken as _2 and never reclaimed, even after the other entity goes away
(TOOLS.md). The two integrations therefore coexist under distinct ids while
the kiosk-side entity is retired.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import LinuxMonitorConfigEntry
from .const import INSTALL_TIMEOUT, REBOOT_TIMEOUT
from .coordinator import APT_UPGRADE_CMD, REBOOT_CMD, _parse_kv
from .entity import LinuxMonitorEntity

_LOGGER = logging.getLogger(__name__)

# Reported for BOTH versions when a host is flagged offline_expected, so the
# pair is equal and the entity scores as nothing-pending.
OFFLINE_VERSION = "offline_expected"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LinuxMonitorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([LinuxMonitorUpdate(entry.runtime_data)])


class LinuxMonitorUpdate(LinuxMonitorEntity, UpdateEntity):
    _attr_name = "System updates"
    _attr_title = "Linux system packages"
    # Raised per instance from the entry option -- see the module docstring on
    # why a host that has not opted in must not advertise the feature.
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "system_update")
        if self.coordinator.allow_install:
            self._attr_supported_features = UpdateEntityFeature.INSTALL

    @property
    def available(self) -> bool:
        """Deliberately overrides the base. See the module docstring."""
        return True

    # --- readings -----------------------------------------------------------

    def _offline_expected(self) -> bool:
        return bool((self.coordinator.data or {}).get("offline_expected"))

    def _counts(self) -> tuple[int | None, int | None, str | None]:
        """(pending, security, newest installed kernel). Any of them None when
        the host did not answer -- never a plausible-looking zero."""
        slow = (self.coordinator.data or {}).get("slow") or {}

        def as_int(raw: Any) -> int | None:
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                return None

        return (as_int(slow.get("UPGRADABLE")), as_int(slow.get("SECURITY")),
                slow.get("KERNEL_INSTALLED") or None)

    def _unattended(self) -> bool | None:
        """None, never False, when the host did not answer."""
        raw = ((self.coordinator.data or {}).get("slow") or {}).get("UNATTENDED")
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip()) == 1
        except (TypeError, ValueError):
            return None

    def _boot_time(self):
        """When the host last booted, from the fast block's /proc/uptime read.

        Computed from the RAW seconds rather than from metrics['boot_time'],
        which sensor.py rounds to the minute so a TIMESTAMP entity does not
        rewrite itself every poll. That rounding always moves the instant
        EARLIER, which here would mean an upgrade and a reboot inside the same
        minute reads permanently as a reboot still owed.
        """
        secs = ((self.coordinator.data or {}).get("metrics") or {}).get(
            "uptime_secs"
        )
        if secs is None:
            return None
        return dt_util.utcnow() - timedelta(seconds=float(secs))

    def _reboot_owed(self) -> bool | None:
        """True when the running system is not what is installed on disk.

        Two independent reasons, either sufficient: the newest installed
        kernel is not the one executing, or an upgrade landed and the host has
        not booted since.
        """
        newest = self._counts()[2]
        running = self.installed_version
        kernel_stale = (
            None if (newest is None or running is None) else newest != running
        )
        applied = self.coordinator.install_applied_at
        if applied is not None:
            boot = self._boot_time()
            if boot is None:
                # Cannot prove it rebooted. Do not assume it did.
                return True
            if boot < applied:
                return True
        return kernel_stale

    @property
    def installed_version(self) -> str | None:
        """The kernel actually executing -- not the newest one on disk."""
        if self._offline_expected():
            return OFFLINE_VERSION
        return ((self.coordinator.data or {}).get("ssh") or {}).get("UNAME") or None

    @property
    def latest_version(self) -> str | None:
        if self._offline_expected():
            # Equal to installed_version on purpose: nothing pending, because
            # nothing was asked. Not a claim that the host is patched.
            return OFFLINE_VERSION
        running = self.installed_version
        pending, security, newest = self._counts()
        if running is None or pending is None or newest is None:
            # Unreadable. None -> unknown, never a confident match on installed.
            return None
        if pending == 0 and newest == running:
            return running
        target = newest
        if pending:
            target = f"{newest} +{pending} pkg"
            if security:
                target += f" ({security} security)"
        return target

    @property
    def release_summary(self) -> str | None:
        if self._offline_expected():
            return (
                "Host is intentionally offline and was not polled. This is a "
                "disposition, not a patch state — it does not mean current."
            )
        running = self.installed_version
        pending, security, newest = self._counts()
        if running is None or pending is None or newest is None:
            return "Patch state unknown — the host did not answer."
        parts: list[str] = []
        if pending:
            parts.append(
                f"{pending} package(s) upgradable"
                + (f", {security} from a security archive" if security else "")
            )
        if newest != running:
            parts.append(
                f"running {running} but {newest} is installed on disk — "
                "reboot required for the kernel to take effect"
            )
        if not parts:
            return "Up to date, and running the newest installed kernel."
        return ". ".join(parts) + "."

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        if self._offline_expected():
            return {
                "disposition": "offline_expected",
                "updates_pending": None,
                "security_pending": None,
                "kernel_installed": None,
                "kernel_running": None,
                "reboot_required": None,
                "install_allowed": self.coordinator.allow_install,
                "install_applied_at": None,
                "apt_lists_mtime": None,
                "unattended_upgrades": None,
                "security_packages": None,
                "pending_since": None,
                "security_age_days": None,
                "last_install_log": None,
                "last_install_tail": None,
            }
        pending, security, newest = self._counts()
        slow = (self.coordinator.data or {}).get("slow") or {}
        since = self.coordinator.pending_since
        age = (
            round((dt_util.utcnow() - since).total_seconds() / 86400, 1)
            if since else None
        )
        return {
            "disposition": "polled",
            "updates_pending": pending,
            "security_pending": security,
            "kernel_installed": newest,
            "kernel_running": self.installed_version,
            # NOT kernel-only. See the module docstring.
            "reboot_required": self._reboot_owed(),
            "install_allowed": self.coordinator.allow_install,
            "install_applied_at": (
                self.coordinator.install_applied_at.isoformat()
                if self.coordinator.install_applied_at else None
            ),
            "apt_lists_mtime": slow.get("APT_LISTS_MTIME"),
            # apt-daily-upgrade only INSTALLS when unattended-upgrades is
            # present. Without it a host refreshes its lists for ever and
            # never self-patches, which looks identical from the outside.
            "unattended_upgrades": self._unattended(),
            # The names behind the count: a bare 6 does not tell you whether
            # it is libnss3 or a font.
            "security_packages": self.coordinator.security_pkgs(slow),
            # The AGE of a pending security package, not its count. Persisted
            # per package, so this survives a restart; last_changed does not
            # and under-reports, which is the direction that hides it.
            "pending_since": since.isoformat() if since else None,
            "security_age_days": age,
            # Where the last remote upgrade's log actually landed on the host,
            # and its last lines. Reported rather than assumed -- the host
            # chooses between ~/.cache and /var/tmp, and it is the host that
            # says which (#8). Both None until this HA session has run one.
            "last_install_log": self.coordinator.last_install_log,
            "last_install_tail": self.coordinator.last_install_tail,
        }

    # --- install ------------------------------------------------------------

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Run apt-get upgrade, then reboot ON EXIT 0 ONLY.

        Every refusal below RAISES rather than returning quietly. A patch
        control that declines silently is worse than one that is absent: the
        operator is left believing the host was patched.
        """
        host = self.coordinator.hostname

        if self._offline_expected():
            raise HomeAssistantError(
                f"{host} is marked offline_expected and is not polled. Its "
                "patch state is unknown, not current — clear the flag and let "
                "it report before patching it."
            )
        if not self.coordinator.allow_install:
            raise HomeAssistantError(
                f"{host} has not opted in to remote patching. Enable "
                "'Allow remote patching and reboot' in this entry's options "
                "first; it is only accepted once sudo answers over this "
                "entry's own SSH credential."
            )

        pending, _security, newest = self._counts()
        if pending is None or newest is None:
            raise HomeAssistantError(
                f"{host} has not reported a patch state. Refusing to upgrade a "
                "host whose current state is unknown."
            )
        if pending == 0 and not self._reboot_owed():
            raise HomeAssistantError(f"{host} has nothing pending.")

        _LOGGER.warning(
            "%s: remote apt upgrade starting — %s package(s) pending",
            host, pending,
        )
        self._attr_in_progress = True
        self.async_write_ha_state()
        # Cleared before the run, not after it. A tail left over from the
        # previous upgrade would otherwise be read as this one's -- and it
        # would read as this one's most convincingly in exactly the case the
        # attribute exists for, a run that returned nothing.
        self.coordinator.last_install_tail = None
        self.coordinator.last_install_log = None
        try:
            ok, out = await self.coordinator.async_exec(
                APT_UPGRADE_CMD, INSTALL_TIMEOUT
            )
            parsed = _parse_kv(out) if ok else None
            if parsed is None:
                # No end marker means the read was truncated or the transport
                # died mid-dpkg. That is NOT a clean failure — the host may be
                # part-configured, so say so rather than implying nothing ran.
                raise HomeAssistantError(
                    f"{host}: apt upgrade did not return a complete result. The "
                    "host may be part-configured — read the upgrade log on the "
                    "host before retrying: ~/.cache/linux_monitor/apt-upgrade.log "
                    "for this entry's SSH account, or "
                    "/var/tmp/linux_monitor-<uid>/apt-upgrade.log if its home "
                    "was not writable. This is the one path that cannot name "
                    "the file itself — LOG_PATH comes back with the end marker "
                    "that was missing here."
                )
            # BEFORE the return-code check, so a FAILED upgrade records its
            # tail too. That is the run an operator most needs the words from.
            self.coordinator.last_install_log = parsed.get("LOG_PATH") or None
            self.coordinator.last_install_tail = parsed.get("TAIL") or None
            rc = parsed.get("APT_RC")
            if rc != "0":
                raise HomeAssistantError(
                    f"{host}: apt-get upgrade exited {rc}. NOT rebooting. "
                    f"Tail: {parsed.get('TAIL', '')}"
                )

            if parsed.get("REMOVED") not in (None, "0"):
                # upgrade is not supposed to be able to do this. If it ever
                # does, that is a finding and it gets said out loud.
                _LOGGER.error(
                    "%s: apt-get upgrade REMOVED %s package(s) — that should be "
                    "impossible with the safer form. Investigate before the "
                    "next host.",
                    host, parsed.get("REMOVED"),
                )
            if parsed.get("KEPT_BACK") not in (None, "0"):
                _LOGGER.warning(
                    "%s: packages were kept back and are NOT installed. They "
                    "need dependency changes apt-get upgrade will not make. "
                    "Not escalating to full-upgrade — that can remove packages "
                    "on a host with no console.",
                    host,
                )
            _LOGGER.warning(
                "%s: apt upgrade OK, %s still upgradable. Rebooting.",
                host, parsed.get("REMAINING"),
            )

            await self.coordinator.async_set_install_applied(dt_util.utcnow())
            # The cached apt numbers now describe the host before the upgrade.
            self.coordinator.invalidate_slow()

            # Chained on rc 0 ONLY. ssh cannot return cleanly from this -- the
            # reboot tears down the transport carrying it -- so the result is
            # logged, not checked. Proof of the reboot is uptime resetting on a
            # later poll, which is what _reboot_owed reads.
            await self.coordinator.async_exec(REBOOT_CMD, REBOOT_TIMEOUT)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

        await self.coordinator.async_request_refresh()
