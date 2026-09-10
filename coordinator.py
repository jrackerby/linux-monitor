"""Data coordinator for a single generic monitored host.

ONE TRANSPORT: ssh. See const.py for why the Glances daemon is gone (GH-470)
and why CPU percent is sampled twice inside one round trip rather than
delta'd across polls.

Everything the host reports arrives as strict KEY=value lines terminated by
an __END__ marker, so a partial failure loses one field rather than the whole
read and a truncated read never looks like a read that found nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALLOW_INSTALL,
    CONF_HOST,
    CONF_HOSTNAME,
    CONF_OFFLINE_EXPECTED,
    CONF_SSH_KEY,
    CONF_SSH_USER,
    CPU_HWMON_NAMES,
    CPU_SAMPLE_SECS,
    DEFAULT_KNOWN_HOSTS,
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_USER,
    EXEC_TIMEOUT,
    PENDING_STORE_KEY,
    PENDING_STORE_VERSION,
    SLOW_INTERVAL,
    SLOW_RETRY_INTERVAL,
    SSH_FAST_TIMEOUT,
    SSH_SLOW_TIMEOUT,
    STORE_SHAPE,
    STORE_SHAPE_KEY,
    TRANSPORT_FAIL_DWELL,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# /bin/sh is dash on every host here (verified 2026-09-01 on devhost01 and the
# Pis), so this block is POSIX sh -- no bashisms, no <<<, no `local`, and no
# `cmd | while read` when a value has to survive the loop, because dash runs
# the right-hand side of a pipe in a subshell.
_HWMON_CASE = "|".join(CPU_HWMON_NAMES)

FAST_CMD = rf"""
# CPU SAMPLE 0 FIRST, sample 1 last, with everything else riding inside the
# same window -- the percentage is a ratio of deltas, so the exact interval
# cancels and only its being non-trivial matters. Fields on the aggregate
# `cpu ` line are user nice system idle iowait irq softirq steal guest
# guest_nice. This is psutil's own arithmetic, which is what Glances
# reported, so the series keeps its meaning: total excludes guest and
# guest_nice (Linux already counts those inside user and nice, and they
# cancel out of both sums), busy excludes idle and iowait.
CPU0=$(awk '/^cpu /{{t=$2+$3+$4+$5+$6+$7+$8+$9; b=$2+$3+$4+$7+$8+$9; printf "%d %d", t, b; exit}}' /proc/stat 2>/dev/null)
echo "CPU_TOT_0=${{CPU0%% *}}"
echo "CPU_BUSY_0=${{CPU0##* }}"

# ASK THE HOST, NEVER THE TABLE -- same rule kiosk_pi's FAST_CMD states at
# length. HOSTNAME is read so a machine answering to a different name than
# the one typed at config time is visible rather than silently assumed.
echo "HOSTNAME=$(hostname)"
echo "UNAME=$(uname -r)"

# /proc/uptime is seconds-since-boot as a float. It replaces Glances' uptime,
# which was a PREFORMATTED STRING ('18:00:08' or '1 day, 2:03:04') that had
# to be regex-parsed back into a duration -- a parse that could fail, and a
# whole class of failure that does not exist here.
echo "UPTIME_SECS=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
echo "CORES=$(nproc 2>/dev/null)"

LAVG=$(cat /proc/loadavg 2>/dev/null)
echo "LOAD1=$(echo "$LAVG" | cut -d' ' -f1)"
echo "LOAD5=$(echo "$LAVG" | cut -d' ' -f2)"
echo "LOAD15=$(echo "$LAVG" | cut -d' ' -f3)"

# MemAvailable, not MemFree. Glances' mem.percent is psutil's
# (total - available) / total, and free/cached/buffers on a healthy Linux box
# makes MemFree read like an emergency permanently.
echo "MEM_TOTAL_KB=$(awk '/^MemTotal:/{{print $2; exit}}' /proc/meminfo 2>/dev/null)"
echo "MEM_AVAIL_KB=$(awk '/^MemAvailable:/{{print $2; exit}}' /proc/meminfo 2>/dev/null)"

# Glances' system.linux_distro was NAME + ' ' + VERSION_ID: 'Debian GNU/Linux 13'.
echo "DISTRO_NAME=$(sed -n 's/^NAME=//p' /etc/os-release 2>/dev/null | head -1 | tr -d '"')"
echo "DISTRO_VERSION=$(sed -n 's/^VERSION_ID=//p' /etc/os-release 2>/dev/null | head -1 | tr -d '"')"

# CAPITAL Model first, then lowercase 'model name'. A Pi's /proc/cpuinfo
# carries `Model : Raspberry Pi 4 Model B Rev 1.5` -- the BOARD, which is what
# an operator wants on the device page -- and no 'model name' line at all. An
# x86 box carries 'model name : Intel(R) Core(TM) i5-9500T ...' and no
# capital-M Model line (verified on devhost01: the anchored grep returns
# nothing), so the two never collide and neither needs an arch test.
CPUM=$(sed -n 's/^Model[ 	]*:[ 	]*//p' /proc/cpuinfo 2>/dev/null | head -1)
[ -z "$CPUM" ] && CPUM=$(sed -n 's/^model name[ 	]*:[ 	]*//p' /proc/cpuinfo 2>/dev/null | head -1)
echo "CPU_MODEL=$CPUM"

# FILESYSTEMS AS INDEXED KEY GROUPS, NEVER A DELIMITED JOIN. A mount point
# may contain any byte but NUL and newline, so no join character is safe
# (LAW.md 4: a value joined into a delimited channel must not be able to
# contain the delimiter). findmnt -r escapes whitespace as \x20, which the
# Python side unescapes.
#
# --real drops pseudo filesystems AND the bind mounts that made Glances
# report / four times on devhost01 (/, /home, /root, /var/tmp, all the same
# device) while missing /boot entirely. -b gives bytes, matching the units
# Glances' psutil-derived size/used/free reported.
OLDIFS=$IFS
IFS='
'
I=0
for L in $(findmnt -rnb -o SOURCE,FSTYPE,TARGET,SIZE,USED,AVAIL --real 2>/dev/null); do
  IFS=' '
  set -- $L
  IFS='
'
  [ $# -ge 6 ] || continue
  echo "FS${{I}}_DEV=$1"
  echo "FS${{I}}_TYPE=$2"
  echo "FS${{I}}_MNT=$3"
  echo "FS${{I}}_SIZE=$4"
  echo "FS${{I}}_USED=$5"
  echo "FS${{I}}_FREE=$6"
  I=$((I+1))
done
IFS=$OLDIFS
echo "FS_N=$I"

# TEMPERATURE. hwmon by NAME first (const.py CPU_HWMON_NAMES says why a
# positional "first hwmon" is wrong), thermal_zone by type as the fallback.
# temp1_input explicitly before the glob: on coretemp it is the package
# sensor, and a bare temp*_input glob sorts temp10_input BEFORE temp1_input,
# which would silently read a core instead of the package on any box with
# ten or more sensors. Every read is guarded -- devhost01's iwlwifi_1 hwmon
# answers ENODATA when the radio is idle, and an unguarded cat there emits
# an error line into a KEY=value channel.
TEMP=""
TEMPSRC=""
for H in /sys/class/hwmon/hwmon*; do
  [ -r "$H/name" ] || continue
  N=$(cat "$H/name" 2>/dev/null) || continue
  case "$N" in
    {_HWMON_CASE}) ;;
    *) continue ;;
  esac
  for T in "$H/temp1_input" "$H"/temp*_input; do
    [ -r "$T" ] || continue
    V=$(cat "$T" 2>/dev/null) || continue
    case "$V" in ''|*[!0-9-]*) continue ;; esac
    TEMP="$V"; TEMPSRC="$N"; break
  done
  [ -n "$TEMP" ] && break
done
if [ -z "$TEMP" ]; then
  for Z in /sys/class/thermal/thermal_zone*; do
    [ -r "$Z/type" ] || continue
    ZT=$(cat "$Z/type" 2>/dev/null) || continue
    case "$ZT" in *cpu*|*CPU*|*x86_pkg_temp*|*soc*) ;; *) continue ;; esac
    V=$(cat "$Z/temp" 2>/dev/null) || continue
    case "$V" in ''|*[!0-9-]*) continue ;; esac
    TEMP="$V"; TEMPSRC="$ZT"; break
  done
fi
# Both keys absent rather than present-and-empty when nothing was readable,
# so the sensor lands on None instead of on a plausible-looking zero.
if [ -n "$TEMP" ]; then
  echo "CPU_TEMP_MC=$TEMP"
  echo "CPU_TEMP_SRC=$TEMPSRC"
fi

sleep {CPU_SAMPLE_SECS}
CPU1=$(awk '/^cpu /{{t=$2+$3+$4+$5+$6+$7+$8+$9; b=$2+$3+$4+$7+$8+$9; printf "%d %d", t, b; exit}}' /proc/stat 2>/dev/null)
echo "CPU_TOT_1=${{CPU1%% *}}"
echo "CPU_BUSY_1=${{CPU1##* }}"
echo "__END__"
"""

# Runs on SLOW_INTERVAL, not every poll -- apt list --upgradable costs real
# time and there is no reason to pay it every 60s. No sudo anywhere in THIS
# block, and none in any polling path: every reading this integration takes on
# its own schedule comes from a world-readable file or an unprivileged query.
# Sudo appears only in the privileged blocks below, which never run on a
# schedule -- see their header.
SLOW_CMD = r"""
U=$(uname -r)
SUF="+${U#*+}"
# EXCLUDE -unsigned. Debian's secure-boot flavour ships linux-image-X-unsigned
# alongside the signed linux-image-X for the SAME version -- the unsigned one
# is the raw kernel binary the signed wrapper carries, never something
# `uname -r` reports, but it sorts as "greater" than its own signed sibling
# under -V and was read as a permanent one-version-ahead false positive on
# devhost01 (measured 2026-08-21: both 6.12.101+deb13-amd64 and its -unsigned
# twin installed, running the former, NEWEST landing on the latter).
NEWEST=$(dpkg-query -W -f='${Package}\n' 'linux-image-*' 2>/dev/null \
  | grep -E '^linux-image-[0-9]' | grep -F -- "$SUF" | grep -v -- '-unsigned$' \
  | sed 's/^linux-image-//' | sort -V | tail -1)
echo "KERNEL_INSTALLED=$NEWEST"
UP=$(apt list --upgradable 2>/dev/null | tail -n +2)
if [ -z "$UP" ]; then
  echo "UPGRADABLE=0"
  echo "SECURITY=0"
  echo "SECURITY_PKGS="
else
  echo "UPGRADABLE=$(echo "$UP" | grep -c '^')"
  echo "SECURITY=$(echo "$UP" | grep -c '/[a-z]*-security')"
  echo "SECURITY_PKGS=$(echo "$UP" | grep '/[a-z]*-security' | cut -d/ -f1 | head -10 | tr '\n' ',' | sed 's/,$//')"
fi
# apt-daily-upgrade only INSTALLS when unattended-upgrades is present, so
# without this a host refreshes its lists for ever and self-patches never.
# Reported rather than assumed: "nothing is auto-patching here" is a claim
# about the host, and the host is the only thing that can answer it.
echo "UNATTENDED=$(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null | grep -c 'ok installed')"
echo "APT_LISTS_MTIME=$(stat -c %Y /var/lib/apt/lists 2>/dev/null)"
echo "__END__"
"""


# --- Privileged blocks ------------------------------------------------------
#
# EVERYTHING ABOVE THIS LINE RUNS UNPRIVILEGED, AND THAT IS THE NORMAL CASE.
# The three blocks below are the only place this integration asks for sudo,
# they run only on an explicit press, and only on an entry whose
# CONF_ALLOW_INSTALL was accepted by the probe in config_flow.py.

# What the options flow runs before it will store allow_install=True. It
# proves the ACCOUNT, KEY, HOST and SUDO GRANT that the apt and reboot blocks
# will actually use, at the moment the operator opts in -- rather than
# discovering at the first press that the read-only monitoring account was
# never granted anything. LAW 9: a check on a different channel certifies
# nothing, and it certifies nothing green.
SUDO_PROBE_CMD = r"""
sudo -n true 2>/dev/null && echo "SUDO=1" || echo "SUDO=0"
echo "__END__"
"""

REBOOT_CMD = "sudo -n /sbin/reboot"

# APT-GET UPGRADE, NEVER FULL-UPGRADE. See INSTALL_TIMEOUT in const.py.
#
# DEBIAN_FRONTEND=noninteractive plus --force-confold/--force-confdef is not
# belt-and-braces. Without it a conffile prompt blocks for ever on a host with
# no tty, the call dies at its timeout, and the failure presents as a network
# fault rather than as the question it actually is.
#
# sudo -n sh -c '...' rather than sudo -n VAR=val cmd: setting an environment
# variable through sudo needs setenv in sudoers, so the latter would become
# silently interactive-unsafe if a NOPASSWD: ALL drop-in is ever tightened.
#
# Output is the same strict KEY=value shape as the collectors and ends in the
# marker, so a truncated read is distinguishable from a clean one. APT_RC is
# captured on the line IMMEDIATELY after the command -- anything between them
# overwrites $?.
APT_UPGRADE_CMD = r"""
LOG=/tmp/linux_monitor_apt_upgrade.log
sudo -n sh -c 'DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef upgrade' > "$LOG" 2>&1
echo "APT_RC=$?"
echo "REMOVED=$(grep -cE '^Remv ' "$LOG")"
echo "KEPT_BACK=$(grep -c 'kept back' "$LOG")"
echo "REMAINING=$(apt list --upgradable 2>/dev/null | tail -n +2 | grep -c '^')"
echo "TAIL=$(tail -2 "$LOG" | tr '\n' ' ' | cut -c1-200)"
echo "__END__"
"""


def _parse_kv(text: str) -> dict[str, str] | None:
    """None if the end marker is missing -- a truncated read must not read as
    a read that found nothing."""
    if "__END__" not in text:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line == "__END__":
            break
        key, sep, val = line.partition("=")
        if sep:
            out[key.strip()] = val.strip()
    return out


# --- typed readers ----------------------------------------------------------
#
# Every one of these lands on None when the field is absent, empty or
# unparseable. None of them can return a plausible value from a failed read --
# that is the whole contract, and it is why they are functions rather than
# inline float() calls at eleven call sites.


def _f(raw: Any) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _i(raw: Any) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _unescape(raw: str) -> str:
    r"""findmnt -r escapes whitespace in SOURCE/TARGET as \x20 etc."""
    out = raw
    for esc, ch in (("\\x20", " "), ("\\x09", "\t"), ("\\x0a", "\n"), ("\\x5c", "\\")):
        out = out.replace(esc, ch)
    return out


def _cpu_percent(kv: dict[str, str]) -> float | None:
    """Busy fraction between the two /proc/stat samples FAST_CMD took.

    A non-positive total delta means the two samples were identical or went
    backwards -- no elapsed time to divide by, so there is no percentage to
    report and the answer is None, never 0.0.
    """
    t0, b0 = _i(kv.get("CPU_TOT_0")), _i(kv.get("CPU_BUSY_0"))
    t1, b1 = _i(kv.get("CPU_TOT_1")), _i(kv.get("CPU_BUSY_1"))
    if t0 is None or b0 is None or t1 is None or b1 is None:
        return None
    dt, db = t1 - t0, b1 - b0
    if dt <= 0 or db < 0:
        return None
    return round(min(100.0, db * 100.0 / dt), 1)


def _mem_percent(kv: dict[str, str]) -> float | None:
    total, avail = _i(kv.get("MEM_TOTAL_KB")), _i(kv.get("MEM_AVAIL_KB"))
    if total is None or avail is None or total <= 0:
        return None
    return round((total - avail) * 100.0 / total, 1)


def _filesystems(kv: dict[str, str]) -> list[dict[str, Any]]:
    """Same row shape Glances' fs plugin produced, so the disk sensor's
    attributes and the health sensor's per-mount loop are unchanged."""
    count = _i(kv.get("FS_N"))
    if count is None or count <= 0:
        return []
    rows: list[dict[str, Any]] = []
    for idx in range(count):
        size = _i(kv.get(f"FS{idx}_SIZE"))
        used = _i(kv.get(f"FS{idx}_USED"))
        free = _i(kv.get(f"FS{idx}_FREE"))
        mnt = kv.get(f"FS{idx}_MNT")
        if mnt is None or size is None or used is None or free is None:
            continue
        # psutil's own definition, which is what Glances reported: used over
        # used-plus-AVAILABLE, not over total. The gap is the root-reserved
        # blocks, and using total instead would under-report every ext4
        # filesystem by about 5%.
        denom = used + free
        percent = round(used * 100.0 / denom, 1) if denom > 0 else None
        rows.append(
            {
                "device_name": _unescape(kv.get(f"FS{idx}_DEV", "")),
                "fs_type": kv.get(f"FS{idx}_TYPE"),
                "mnt_point": _unescape(mnt),
                "size": size,
                "used": used,
                "free": free,
                "percent": percent,
            }
        )
    return rows


def _temp_c(kv: dict[str, str]) -> float | None:
    """hwmon and thermal_zone both report milli-degrees Celsius."""
    milli = _i(kv.get("CPU_TEMP_MC"))
    if milli is None:
        return None
    return round(milli / 1000.0, 1)


def _distro(kv: dict[str, str]) -> str | None:
    name = (kv.get("DISTRO_NAME") or "").strip()
    version = (kv.get("DISTRO_VERSION") or "").strip()
    joined = f"{name} {version}".strip()
    return joined or None


def _boot_time(uptime_secs: float | None) -> datetime | None:
    """Rounded to the minute, deliberately. /proc/uptime has sub-second
    precision, so an unrounded boot instant would differ by a few
    milliseconds on every single poll and rewrite a TIMESTAMP entity's state
    once a minute forever. Glances' string uptime was second-resolution and
    this rounding was already here for the same reason."""
    if uptime_secs is None:
        return None
    boot = dt_util.utcnow() - timedelta(seconds=uptime_secs)
    return boot.replace(second=0, microsecond=0)


def _metrics(kv: dict[str, str] | None) -> dict[str, Any]:
    """Raw KEY=value lines to typed readings. Called once per poll; every
    entity reads from the result rather than re-parsing."""
    if not kv:
        return {}
    uptime_secs = _f(kv.get("UPTIME_SECS"))
    return {
        "cpu_percent": _cpu_percent(kv),
        "mem_percent": _mem_percent(kv),
        "load1": _f(kv.get("LOAD1")),
        "load5": _f(kv.get("LOAD5")),
        "load15": _f(kv.get("LOAD15")),
        "cores": _i(kv.get("CORES")),
        "fs": _filesystems(kv),
        "temp_c": _temp_c(kv),
        "temp_source": kv.get("CPU_TEMP_SRC") or None,
        "uptime_secs": uptime_secs,
        "boot_time": _boot_time(uptime_secs),
        "distro": _distro(kv),
        "cpu_model": kv.get("CPU_MODEL") or None,
        "kernel": kv.get("UNAME") or None,
        "hostname_reported": kv.get("HOSTNAME") or None,
    }


class LinuxMonitorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER,
            name=f"linux_monitor:{entry.data[CONF_HOSTNAME]}",
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.hostname: str = entry.data[CONF_HOSTNAME]
        self.ssh_user: str = entry.data.get(CONF_SSH_USER, DEFAULT_SSH_USER)
        self.ssh_key: str = entry.data.get(CONF_SSH_KEY, DEFAULT_SSH_KEY)

        self._ssh_fails = 0
        self._slow: dict[str, str] = {}
        self._slow_at: datetime | None = None
        self._slow_failed = False

        # Per-package first-seen for pending SECURITY packages, and the moment
        # a remote upgrade last landed. Both persist to .storage; const.py's
        # PENDING_STORE_VERSION block argues why neither can be derived from
        # entity last_changed. Keyed on the configured hostname so two entries
        # never share a file.
        self._pending_since: dict[str, str] = {}
        self.install_applied_at: datetime | None = None
        self._store: Store = Store(
            hass,
            PENDING_STORE_VERSION,
            f"{PENDING_STORE_KEY}_{self.hostname.lower()}",
        )

        # A crash-loop that self-heals inside TRANSPORT_FAIL_DWELL is invisible
        # to the health binary_sensor (its streak resets on the next good poll)
        # -- measured on devhost01, GH-402: ~20 hard reboots in 24h tripped it
        # once. This counter is the persistent record; it only ever grows.
        self.reboot_count: int = 0
        self._last_uptime_secs: float | None = None

    def restore_reboot_count(self, count: int) -> None:
        """Called once by the reboot-count sensor's async_added_to_hass,
        before this session has observed any reboot of its own -- an HA
        restart must not read as the crash-loop having stopped."""
        self.reboot_count = count

    @property
    def offline_expected(self) -> bool:
        return bool(self.entry.options.get(CONF_OFFLINE_EXPECTED, False))

    @property
    def allow_install(self) -> bool:
        """Per-entry opt-in for the two privileged controls. Option first,
        entry data second, False last -- an entry created before this option
        existed has neither and must land on False, never on a default that
        quietly grants something nobody asked for."""
        return bool(
            self.entry.options.get(
                CONF_ALLOW_INSTALL,
                self.entry.data.get(CONF_ALLOW_INSTALL, False),
            )
        )

    # --- persisted patch state ----------------------------------------------

    async def async_load_pending(self) -> None:
        """Restore the first-seen clock and install_applied_at from disk.

        MUST run before the first refresh, or that poll writes a fresh
        timestamp for every pending package and every age resets to zero on
        every Home Assistant restart -- which under-reports, the direction
        that hides the problem.

        The shape is read from the _v discriminator, never guessed from a key
        that looks like a package name (const.py STORE_SHAPE_KEY). An
        unrecognised shape is left alone rather than reinterpreted: losing the
        clock is recoverable, misreading someone else's payload as package
        names is not.
        """
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        if stored.get(STORE_SHAPE_KEY) != STORE_SHAPE:
            _LOGGER.warning(
                "%s: patch-age store has shape %s, expected %s -- ignoring it "
                "rather than guessing at its contents",
                self.hostname, stored.get(STORE_SHAPE_KEY), STORE_SHAPE,
            )
            return
        raw = stored.get("pending")
        if isinstance(raw, dict):
            self._pending_since = {str(k): str(v) for k, v in raw.items()}
        self.install_applied_at = dt_util.parse_datetime(
            str(stored.get("install_applied_at") or "")
        )

    async def _save(self) -> None:
        """One writer for the whole payload. Both halves go out together on
        purpose: two async_save calls against the same Store race, and the
        loser silently drops the other half."""
        await self._store.async_save({
            STORE_SHAPE_KEY: STORE_SHAPE,
            "pending": self._pending_since,
            "install_applied_at": (
                self.install_applied_at.isoformat()
                if self.install_applied_at else None
            ),
        })

    async def async_set_install_applied(self, when: datetime | None) -> None:
        """Record when a remote upgrade landed, and PERSIST it. update.py goes
        through this rather than assigning the attribute: an assignment cannot
        save, and this value only earns its keep by surviving the restart that
        would otherwise erase it between the upgrade and the host's reboot."""
        self.install_applied_at = when
        await self._save()

    def security_pkgs(self, slow: dict[str, str]) -> list[str] | None:
        """None when nobody asked, [] when the host answered and had none.

        Collapsing those two is the stale-zero shape this integration exists
        to avoid. One parser, called from both the sensor and the update
        entity -- two would eventually disagree about what counts as a package
        and the clock and the tile would drift.
        """
        if "SECURITY_PKGS" not in slow:
            return None
        raw = str(slow.get("SECURITY_PKGS") or "").strip()
        if not raw:
            return []
        return [p for p in (x.strip() for x in raw.split(",")) if p]

    async def _track_pending(self, slow: dict[str, str]) -> None:
        """First-seen clock, keyed on package NAME and never on the count.

        Name-keyed, each package carries its own first-seen: a second package
        landing does not restart the clock on the first, and clearing one does
        not restart the other. Count-keyed, both of those reset.

        A host that did not answer returns None and changes NOTHING. An unread
        poll is not an empty set, and treating it as one would clear the clock
        on exactly the hosts that stopped reporting.
        """
        pkgs = self.security_pkgs(slow)
        if pkgs is None:
            return
        now = dt_util.utcnow().isoformat()
        before = dict(self._pending_since)
        self._pending_since = {p: before.get(p, now) for p in pkgs}
        if self._pending_since != before:
            await self._save()

    @property
    def pending_since(self) -> datetime | None:
        """Oldest first-seen across the packages still pending."""
        stamps = [
            t for t in (
                dt_util.parse_datetime(v) for v in self._pending_since.values()
            ) if t
        ]
        return min(stamps) if stamps else None

    def invalidate_slow(self) -> None:
        """Force the next poll to re-run the slow block.

        After a patch run the cached apt numbers describe the host as it was
        BEFORE the upgrade. Serving those for up to six more hours is the
        stale-zero shape in its most misleading form -- it would show a pending
        count the operator had just cleared.
        """
        self._slow = {}
        self._slow_at = None
        self._slow_failed = False

    # --- transport ----------------------------------------------------------

    async def _ssh(self, script: str, timeout: int) -> dict[str, str] | None:
        rc, out, _err = await self._ssh_raw(script, timeout)
        if rc != 0:
            return None
        return _parse_kv(out)

    async def _ssh_raw(self, script: str, timeout: int) -> tuple[int | None, str, str]:
        argv = [
            "ssh", "-i", self.ssh_key,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={DEFAULT_KNOWN_HOSTS}",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            f"{self.ssh_user}@{self.host}",
            script,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:
            _LOGGER.debug("%s: ssh spawn failed: %s", self.hostname, err)
            return None, "", str(err)
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await proc.communicate()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _LOGGER.warning("%s: ssh timed out after %ss", self.hostname, timeout)
            return None, "", "timeout"
        return (proc.returncode,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"))

    async def async_exec(
        self, script: str, timeout: int = EXEC_TIMEOUT
    ) -> tuple[bool, str]:
        """Run a one-off command and return (ok, stdout). Used by the button
        and the update entity, never by a polling path.

        `ok` says only that ssh itself succeeded. Callers verify the EFFECT by
        reading the host back -- a zero exit is not proof of a state change.
        """
        if self.offline_expected:
            _LOGGER.warning(
                "%s: refusing to act on a host marked offline_expected",
                self.hostname,
            )
            return False, ""
        rc, out, err = await self._ssh_raw(script, timeout)
        if rc != 0:
            _LOGGER.error("%s: command failed rc=%s: %s", self.hostname, rc,
                          err[:300] or out[:300])
            return False, out
        return True, out

    # --- update ---------------------------------------------------------

    def _track(self, name: str, ok: bool, streak: int) -> int:
        """Logged once per outage, at the crossing -- a warning repeating
        every 60s is as unreadable as no warning."""
        if ok:
            if streak >= TRANSPORT_FAIL_DWELL:
                _LOGGER.warning("%s: %s recovered after %s missed polls",
                                self.hostname, name, streak)
            return 0
        streak += 1
        if streak == TRANSPORT_FAIL_DWELL:
            _LOGGER.warning(
                "%s: %s unreachable for %s consecutive polls -- health is now "
                "reporting a problem for this host",
                self.hostname, name, streak,
            )
        return streak

    async def _async_update_data(self) -> dict[str, Any]:
        """Always returns a dict. Never raises UpdateFailed -- see
        kiosk_pi/coordinator.py for why: raising takes every entity
        unavailable, and attributes on an unavailable entity vanish, which
        is how a broken collector reads green."""
        if self.offline_expected:
            # Short-circuits with NO network i/o at all, and scores clean
            # rather than red -- a host that is meant to be off is not a fault.
            self._ssh_fails = 0
            return {
                "offline_expected": True,
                "online": False,
                "ssh_ok": False,
                "ssh": {},
                "metrics": {},
                "slow": {},
                "hostname_configured": self.hostname,
                "ssh_fails": 0,
            }

        fast = await self._ssh(FAST_CMD, SSH_FAST_TIMEOUT)
        self._ssh_fails = self._track("ssh", fast is not None, self._ssh_fails)
        metrics = _metrics(fast)

        # Uptime only ever increases between two polls of the same boot; any
        # decrease is proof the host restarted, whether or not a miss was
        # ever observed here. Detected on the poll AFTER recovery, which is
        # the whole point -- it survives a self-heal the streak trackers do not.
        uptime_secs = metrics.get("uptime_secs")
        if uptime_secs is not None:
            if (self._last_uptime_secs is not None
                    and uptime_secs < self._last_uptime_secs):
                self.reboot_count += 1
                _LOGGER.warning(
                    "%s: uptime reset (was %ss, now %ss) -- reboot #%s detected",
                    self.hostname, int(self._last_uptime_secs), int(uptime_secs),
                    self.reboot_count,
                )
            self._last_uptime_secs = uptime_secs

        now = dt_util.utcnow()
        interval = SLOW_RETRY_INTERVAL if self._slow_failed else SLOW_INTERVAL
        due = self._slow_at is None or (now - self._slow_at) >= interval
        if due and fast is not None:
            slow = await self._ssh(SLOW_CMD, SSH_SLOW_TIMEOUT)
            if slow is not None:
                if self._slow_failed:
                    _LOGGER.warning("%s: slow block readable again after a "
                                    "failed read", self.hostname)
                self._slow = slow
                self._slow_at = now
                self._slow_failed = False
                await self._track_pending(slow)
            else:
                self._slow = {}
                self._slow_at = now
                if not self._slow_failed:
                    _LOGGER.warning(
                        "%s: slow block unreadable -- apt and kernel "
                        "reporting are unknown for this host", self.hostname,
                    )
                self._slow_failed = True

        # `online` and `ssh_ok` are now the same fact -- there is one transport.
        # Both keys are kept because they answer different questions to a
        # reader (is this host answering / did THIS transport answer), and
        # collapsing them would be a rename with no benefit.
        return {
            "offline_expected": False,
            "online": fast is not None,
            "ssh_ok": fast is not None,
            "ssh": fast or {},
            "metrics": metrics,
            "slow": dict(self._slow),
            "slow_at": self._slow_at,
            "hostname_configured": self.hostname,
            "ssh_fails": self._ssh_fails,
        }
