"""Constants for the Linux Monitor integration.

WHAT THIS IS. Generic OS-health monitoring for a Debian host: CPU, memory,
disk, temperature, load, uptime, kernel currency, pending updates — read
entirely over a read-only SSH connection. AGENTLESS: nothing is installed on
a monitored host and no daemon has to be running on it. Modeled on
custom_components/kiosk_pi's coordinator/dwell shape (same "never raise
UpdateFailed" honesty rule, same __END__ marker discipline, same
log-once-at-the-crossing streak logic), deliberately without kiosk_pi's
kiosk-specific concepts: no Chromium, no display, no dashboard assignment
and no kiosk.sh config-drift assertion. Remote patch and reboot ARE here as
of 0.5.0, off by default and per entry -- see the reversal note below.

0.4.0: RENAMED FROM host_monitor TO linux_monitor. The name was the point:
after generic OS health was split away from the kiosk integration, this
component owns it for every Linux host, including hosts that are not kiosks. `host_monitor` described what it was.

THE DOMAIN IS THE ONLY THING THAT MOVED, AND IT MOVED OUTSIDE THIS CODE.
A config entry's domain is its dispatch key, so no in-component hook can
change it — `async_migrate_entry` never runs for an entry whose integration
no longer exists. The rename was therefore completed by an offline rewrite of
the three registries (`core.config_entries`, `core.device_registry`,
`core.entity_registry`) with HA stopped, carrying every existing row across
under the new name. unique_id is `<hostname>_<key>` and never contained the
domain, so it did not need to move; entity_id did not move either, which is
the whole point of doing it that way rather than letting HA re-register 78
entities beside the originals as `_2` duplicates. Consumers name entity_ids,
so nothing downstream was repointed: household_state's INTEGRITY axis keys on
config-entry SHAPE and names no domain, and the dashboard fleet reads ids.

0.3.0: THE GLANCES DAEMON DEPENDENCY IS GONE, and with it the whole
second transport. Every reading that used to come from a Glances REST daemon
on :61208 now comes from /proc, /sys, findmnt, nproc and /etc/os-release over
the SSH connection this integration already opened for the kernel readback —
one transport, no agent, nothing to keep running on the host.

WHY, measured 2026-09-01: a monitored host read fully unavailable. Its Glances
was running in XML-RPC mode bound to loopback, so the REST transport was dead —
and the `monitor` account did not exist on that host at all, so the SSH
transport had never worked either. The entry was Glances-only in practice
from the day it was created and one daemon misconfiguration took the whole
host dark. An agent that must be installed, configured and kept running is a
failure mode; a file in /proc is not.

ONE TRANSPORT, SAID OUT LOUD. There is no longer anything to combine, so the
glances_fails counter, the min(glances_fails, ssh_fails) floor in the health
sensor's _reasons(), and the glances_ok / glances_missed_polls attributes are
all removed rather than left reporting a constant. `online` and `ssh_ok` are
now the same fact and only ssh_ok is published. Same deliberate collapse
kiosk_pi 0.12.0 made for the same reason.

NO SUDO ON ANY POLLING PATH, AND THAT IS A CONSTRAINT ON THE SOURCES. Every
path read by FAST_CMD and SLOW_CMD is world-readable: /proc/stat,
/proc/meminfo, /proc/loadavg, /proc/uptime, /proc/cpuinfo, /etc/os-release,
/sys/class/hwmon, /sys/class/thermal, and findmnt/nproc/dpkg-query/apt-list,
all confirmed readable unprivileged on both the amd64 hosts and the Pis. That
is a design rule, not an accident of provisioning, and it does not move:
everything this integration collects on its own schedule stays unprivileged.

0.5.0, GH linux-monitor#3: REMOTE PATCH AND REBOOT SUPPORT LANDED. THIS
REVERSES THIS FILE'S OWN EARLIER POSITION, WHICH READ "NO REMOTE PATCH,
INSTALL OR REBOOT SUPPORT, DELIBERATELY, UNLIKE kiosk_pi's update.py …
read-only for every host here, no exceptions without arguing a specific one
on its own." Labelled as a reversal rather than edited away, because the
earlier text was right about the risk and the argument it demanded is the
thing that changed: the maintainer asked for update monitoring and remote
system updates here, and named kiosk_pi's implementation as the source to
take them from. That is the specific argument, and it is the owner's.

WHAT THE EARLIER POSITION WAS PROTECTING, AND HOW IT SURVIVES. A monitored
host may be the very machine an operator is working on, and a remote
reboot/upgrade path into it is a footgun. Three things keep that closed rather than reopened:

  1. The capability is OFF by default and opted in PER ENTRY
     (CONF_ALLOW_INSTALL). Held off, neither control exists at all.
  2. It needs passwordless sudo, which the monitor account does not have --
     measured live on the first host 2026-09-01, `sudo -n true` answered "a
     password is required". That measurement is now load-bearing rather than
     merely descriptive: it is why the option cannot be turned on there by
     accident.
  3. Turning it on PROBES for that sudo over the entry's own account, key and
     address, and refuses to save when the answer is no -- or when the host
     could not be asked at all. An unreachable host is not a yes.

So the read-only default is unchanged for every host; what moved is that a
host CAN now be argued into patchability one entry at a time, on the host's
own evidence, instead of the whole capability living in a kiosk-specific
integration because the kiosks needed it first.

0.2.0, maintainer ruling, standing: THIS INTEGRATION ALSO COVERS THE FOUR KIOSK PI HOSTS'
GENERIC OS HEALTH. kiosk_pi's own coordinator used to read cpu/mem/disk/temp/
load and expose kernel/apt facts alongside its kiosk-specific ones — the
un-deduplicated half of the split this integration's design already
described. kiosk_pi 0.12.0 removed all of that; each kiosk Pi gets its own
linux_monitor entry instead, using the SAME ssh_user/ssh_key kiosk_pi already
had working access with (CONF_SSH_USER/CONF_SSH_KEY are per-entry, not the
DEFAULT_* below — a fresh "monitor" credential was not provisioned onto
hardware already reachable). Forcing a non-kiosk host into kiosk_pi would
trip its KioskPiConfigDrift binary_sensor permanently (kiosk.sh does not
exist there) and clutter the device with entities that can never read
anything; the same reasoning is why this split runs the other direction
rather than merging everything into one entry.

DHCP DISCOVERY IS NOT OFFERED. kiosk_pi's dhcp hostname-glob discovery exists
to keep an operator from having to type in five Pis; a generic host has no
naming convention to glob on, so every entry here is added by hand, which
is also the safer default — nothing gets auto-enrolled as monitored.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "linux_monitor"

CONF_HOST = "host"
CONF_HOSTNAME = "hostname"
CONF_SSH_USER = "ssh_user"
CONF_SSH_KEY = "ssh_key"
CONF_OFFLINE_EXPECTED = "offline_expected"

# PER-HOST ALLOWLIST FOR PRIVILEGED ACTION. Default False, and it must stay
# False by default: turning it on adds UpdateEntityFeature.INSTALL, which
# lists this host in Home Assistant's global Updates panel with an Install
# button and an update-all path, and it creates the Reboot host button. One
# mis-click in a list of unrelated updates would then patch and reboot a
# machine nobody was thinking about.
#
# ONE OPTION FOR BOTH CONTROLS, NOT TWO. They are the same grant: patching
# chains a reboot on apt exit 0, and both need the same passwordless sudo on
# the monitored host. Splitting them would imply a host could be allowed to
# reboot but not patch when the underlying permission is identical, and a
# gate that does not match what it gates is a gate nobody can reason about.
#
# The default SSH account (DEFAULT_SSH_USER) is a read-only one with NO sudo,
# which is why config_flow probes `sudo -n true` over the entry's own
# credential before it will store this as True. A setup check that exercises a
# different channel than the one that will be used certifies nothing.
CONF_ALLOW_INSTALL = "allow_install"

# Entry-data key from VERSION 1, when a Glances REST daemon was a second
# transport. Named here ONLY so async_migrate_entry can strip it; nothing
# reads it and no code path honours a value found under it. Do not
# reintroduce it as a live option — see this file's header.
LEGACY_CONF_GLANCES_PORT = "glances_port"

DEFAULT_SSH_USER = "monitor"
# DELIBERATELY NOT RENAMED WITH THE DOMAIN. This is a path to a
# private key that exists on the Home Assistant host under this exact name,
# not a reference to the old domain. Renaming the string without moving the
# file breaks SSH auth for every entry already using it, and moving a
# credential to make a name tidy buys nothing. The filename is
# historical and stays that way.
DEFAULT_SSH_KEY = "/config/.ssh/host_monitor_key"
DEFAULT_KNOWN_HOSTS = "/config/.ssh/known_hosts"

UPDATE_INTERVAL = timedelta(seconds=60)
SLOW_INTERVAL = timedelta(hours=6)

# Same reasoning as kiosk_pi/const.py SLOW_RETRY_INTERVAL: a failed slow read
# must not park apt/kernel reporting at unknown until the next 6h window.
SLOW_RETRY_INTERVAL = timedelta(minutes=15)

SSH_FAST_TIMEOUT = 15
SSH_SLOW_TIMEOUT = 60

# CPU PERCENT IS THE ONE READING THAT NEEDS TWO SAMPLES. /proc/stat is
# cumulative jiffies since boot, so a percentage is a delta between two
# instants and a single read can only ever produce a since-boot average.
#
# TWO SAMPLES IN ONE SSH ROUND TRIP, not a delta carried across polls.
# Both were viable; this is why this one:
#   - It is honest on the very first poll. The carried-delta form has no
#     previous sample after any HA restart or entry reload and MUST report
#     None for a full minute — and a None that has to be remembered to be
#     produced is exactly the stale-zero shape this integration's comments
#     exist to prevent. One wrong `or 0` and a restart reads 0% CPU.
#   - It reports the same QUANTITY Glances did. A delta across the 60s poll
#     interval is a 60-second mean; Glances sampled over its own few-second
#     refresh. Swapping the transport must not silently change what the
#     series means, because ~5 weeks of recorder history is graphed against
#     it and nothing would mark the discontinuity.
#   - It carries no state, so there is no restart hole to reason about.
# The cost is this many seconds of a 60s budget, inside one connection that
# is already paid for, and well inside SSH_FAST_TIMEOUT.
CPU_SAMPLE_SECS = 1

# Consecutive missed polls before a TRANSPORT miss counts as a health problem.
# Same value and same reasoning as kiosk_pi: three minutes is longer than a
# routine reboot or one transient ssh timeout.
TRANSPORT_FAIL_DWELL = 3

# CONSECUTIVE AUTH-CLASSIFIED FAILURES BEFORE A REAUTH FLOW IS STARTED. Lower
# than TRANSPORT_FAIL_DWELL on purpose and for the opposite reason: that dwell
# exists because an absent reading might be a flap that heals itself, and an
# auth failure never does -- a key that is not in authorized_keys will not be
# there next minute either. Two rather than one only so a single malformed
# stderr cannot put a reauth card in front of the operator.
#
# It is deliberately SHORTER than the health dwell, so the card that names the
# real cause appears BEFORE the sensor that would otherwise send the operator
# to look at the network.
SSH_AUTH_FAIL_DWELL = 2

# Health ladder. CPU busy percent, never load.min1 (a multi-core host
# saturates load well past 1.0 under normal use; see kiosk_pi/const.py for
# the measured defect this avoids repeating).
CPU_PROBLEM_PCT = 90.0
DISK_PROBLEM_PCT = 90.0

# hwmon `name` values that are a CPU package/die temperature, worst-first by
# preference. Measured 2026-09-01: coretemp on amd64 hosts (hwmon2, with
# temp1_input labelled "Package id 0"), cpu_thermal on a Raspberry Pi
# (hwmon0). NOT a guess — an Intel box also exposes pch_cannonlake, hp,
# ucsi_source_psy_* and iwlwifi_1 as hwmon devices, and iwlwifi_1's
# temp1_input answers ENODATA when the radio is idle, so "first hwmon with a
# temp input" would read the wrong chip or fail outright.
CPU_HWMON_NAMES = (
    "coretemp",
    "k10temp",
    "zenpower",
    "cpu_thermal",
    "cpu-thermal",
    "soc_thermal",
)


# --- Privileged actions ----------------------------------------------------
#
# apt-get upgrade, NEVER full-upgrade. upgrade cannot remove a package and
# cannot install a new one; full-upgrade can do both. A monitored host may be
# headless, wall-mounted or in a rack nobody visits, and a package removal
# that takes out the graphics or network stack is not a fault that can be
# fixed from here. Packages held back by the safer form are REPORTED, never
# silently escalated — a hold-back is a finding.
#
# INSTALL_TIMEOUT is its own budget and is deliberately not SSH_SLOW_TIMEOUT.
# 60 s is sized for `apt list --upgradable`; unpacking ten packages on a Pi 3B
# under load runs well past it, and a timeout mid-dpkg leaves a host
# part-configured. This number is sized not to be hit.
INSTALL_TIMEOUT = 900

# The reboot tears down the transport it is issued over, so ssh cannot return
# cleanly. This budget waits for the command to LAND, not to answer.
REBOOT_TIMEOUT = 20

# Default budget for a one-off command that is neither of the above.
EXEC_TIMEOUT = 45

# Budget for the `sudo -n true` probe the options flow runs. It is a login
# plus one builtin; anything slower than this is a host that has no business
# being handed an unattended apt run.
SUDO_PROBE_TIMEOUT = 20

# --- Security-patch AGE clock ----------------------------------------------
#
# A count of pending security updates is not a severity. Two packages found an
# hour ago and two found forty days ago are the same number and different
# problems.
#
# WHY THIS IS PERSISTED AND NOT DERIVED. The obvious source is last_changed on
# the security-count sensor. It is wrong twice over: it re-arms whenever the
# count moves, so 1 -> 2 restarts the clock on the package that was already
# there, and HA resets it to restart time for every restored entity, so a host
# forty days behind reads zero days old after any restart. Both failures are
# permissive — they under-report age, which is the direction that hides the
# problem.
PENDING_STORE_VERSION = 1
PENDING_STORE_KEY = "linux_monitor_pending"

# THE STORE HOLDS ONE SHAPE TODAY AND MUST NOT GUESS IF THAT EVER CHANGES.
# The payload carries a package -> first-seen mapping alongside
# install_applied_at, discriminated by _v rather than by sniffing for a key
# that looks like a package name: a dpkg package name cannot begin with an
# underscore, so this key can never collide with one.
STORE_SHAPE_KEY = "_v"
STORE_SHAPE = 1
