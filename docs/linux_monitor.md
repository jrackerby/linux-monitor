# linux_monitor

> **Front-end consumers below are HISTORICAL.** Every `www/*.js` card and
> every `dashboards/*.yaml` board this document names was deleted — the card
> fleet and its 54 `/local/` resource registrations by GH-564, the 41 dead
> board files and the card-era tooling by GH-575. Verified live: the Lovelace
> resource registry holds no `/local/` entry and `lovelace/dashboards/list`
> returns `[]`.
>
> What that does and does not invalidate: **the entity, platform and
> resolution content is unaffected** and is still the reference for this
> integration. Only the consumer claims are stale, and they are stale in one
> direction — a card named here as a consumer no longer exists, so "who reads
> this entity" reads as *nobody in this repo* until a dashboard app claims it.
> Those apps live in their own repos (CLAUDE.md §5) and are not greppable from
> here, so absence of a consumer in this document is not evidence there is
> none. A "no consumer found" conclusion reached by grepping `www/` or
> `dashboards/` still holds; the directories it names simply no longer exist.

Siblings in this set: [`linux_monitor_abstract.md`](linux_monitor_abstract.md) —
one-page plain-language summary · [`linux_monitor_process_flow.md`](linux_monitor_process_flow.md) —
control flow, execution order · [`linux_monitor_data_flow.md`](linux_monitor_data_flow.md) —
data lineage · [`linux_monitor_architecture.md`](linux_monitor_architecture.md) —
static module structure and system boundary · [`linux_monitor_patent_disclosure.md`](linux_monitor_patent_disclosure.md) —
novelty assessment.

`custom_components/linux_monitor` — version 0.3.0. Generic OS-health
monitoring for a Debian host that is not a kiosk: CPU, memory, disk,
temperature, load, uptime, kernel currency, pending updates — read
**entirely over a read-only SSH connection**. One config entry per host,
one `DataUpdateCoordinator` per entry.

**Agentless.** Nothing is installed on a monitored host and no daemon has
to be running on it. Every value comes from a world-readable file
(`/proc`, `/sys`, `/etc/os-release`) or a stock utility (`findmnt`,
`nproc`, `dpkg-query`, `apt`), read by an account with **no sudo**.

Source of truth for anything below is `custom_components/linux_monitor/`
itself — `const.py`'s module docstring carries the scoping rationale.
This file is a reader's map onto that code, not a replacement for it —
if the two disagree, the code is right and this file is stale.

## 1. What it's for

Modeled on `custom_components/kiosk_pi`'s coordinator/dwell shape (same
"never raise `UpdateFailed`" honesty rule, same `__END__` marker
discipline, same log-once-at-the-crossing streak logic), deliberately
**without** kiosk_pi's kiosk-specific concepts: no Chromium, no display,
no dashboard assignment, no `kiosk.sh` config-drift assertion, no remote
patch or reboot. `const.py`'s docstring is explicit that forcing a
non-kiosk host into `kiosk_pi` would trip its `KioskPiConfigDrift`
binary_sensor permanently (`kiosk.sh` does not exist on a generic host)
and clutter the device with entities that can never read anything.

Since 0.2.0 this integration also covers **the four kiosk Pis' generic OS
health**. `kiosk_pi` 0.12.0 (GH-467) removed its own duplicate cpu/mem/
disk/temp/load collection; each kiosk Pi now carries its own
`linux_monitor` entry using the same `ssh_user`/`ssh_key` `kiosk_pi`
already had working access with. `kiosk_pi` keeps every kiosk-specific
reading and its own remote-patch surface; nothing about display,
DevTools, config drift or install moved here.

Three deliberate omissions, stated directly in `const.py`:

- **No agent, and no second transport.** See §2 and GH-470.
- **No remote patch, install or reboot support**, for any host. The
  first host monitored, `ecldev01`, is a Claude Code working host; a
  remote reboot/upgrade path into the machine driving the session
  administering it is named as a footgun this integration does not
  carry. This stays true fleet-wide including for the kiosk hosts —
  `kiosk_pi`'s own `update.py` is the only remote-patch surface for
  those, scoped to the four hosts that need it and gated per-entry
  behind `allow_install`.
- **No DHCP discovery.** `kiosk_pi`'s DHCP hostname-glob discovery exists
  because Pi hostnames follow a naming convention worth globbing; a
  generic host has none, so every host here is added by hand — also
  named as the safer default, since nothing gets auto-enrolled.

## 2. Requirements to run

- HA core only — `manifest.json` declares `dependencies: []`,
  `requirements: []`. No PyPI package, no third-party service, and since
  0.3.0 **no daemon on the monitored host**.
- `integration_type: device`, `iot_class: local_polling`. `config_flow: true`,
  no `single_config_entry` restriction — one entry per monitored host,
  added via **Settings → Devices & Services → Add Integration → Host
  Monitor**, once per host.
- Config flow fields (`config_flow.py`): `host` (IP, required),
  `hostname` (optional — left blank it is probed live **over SSH, using
  the credential the entry will actually poll with**; required if
  `offline_expected` is set, since an intentionally-offline host cannot
  be probed), `ssh_user` (default `monitor`), `ssh_key` (default
  `/config/.ssh/host_monitor_key`), `offline_expected` (bool). The unique
  id is the lower-cased hostname, so re-adding the same host with a
  different-case name still collides.
- **The probe exercises the transport, deliberately.** Before 0.3.0 it
  asked a Glances daemon for the hostname and never touched SSH, so an
  entry could be created against an SSH account that does not exist on
  the target and still validate green — which is exactly what happened to
  `eclprod01` (see §5's GH-470 row). A config flow that does not exercise
  the transport certifies nothing.
- Options flow exposes one field: `offline_expected`. Toggling it
  triggers a full entry reload (`__init__.py`'s `async_reload_entry`) so
  the coordinator picks up the new value immediately rather than waiting
  for the next poll.
- **Upstream data source per host: one SSH connection, two blocks.**
  - `FAST_CMD`, **every poll** (60s, `UPDATE_INTERVAL`): two `/proc/stat`
    samples one second apart (see §3), `/proc/meminfo`, `/proc/loadavg`,
    `/proc/uptime`, `nproc`, `/proc/cpuinfo`, `/etc/os-release`,
    `findmnt -rnb --real`, and a CPU temperature from `/sys/class/hwmon`
    (falling back to `/sys/class/thermal`). `BatchMode=yes` (key-only
    auth, no interactive fallback), 8s `ConnectTimeout`, 15s overall
    (`SSH_FAST_TIMEOUT`).
  - `SLOW_CMD`, **6-hourly** (`SLOW_INTERVAL`): installed-kernel version
    (via `dpkg-query`, explicitly excluding Debian's `-unsigned`
    kernel-image twin — see §5), `apt list --upgradable` counts (total
    and security-tagged), and the mtime of `/var/lib/apt/lists`. Retried
    every 15 minutes (`SLOW_RETRY_INTERVAL`) instead of waiting a full 6h
    window after a failed read.
  - Both blocks emit strict `KEY=value` lines terminated by an `__END__`
    marker, so a partial failure loses one field rather than the whole
    read and a truncated read never reads as a read that found nothing.
  - **The monitored account needs no sudo.** Verified live 2026-09-01 on
    both host classes: `sudo -n true` on `ecldev01`'s `monitor` answers
    "a password is required" and every source above still reads.
- **The target host's shell is `/bin/sh` (dash).** Both blocks are POSIX
  sh — no bashisms, and no `cmd | while read` where a value must survive
  the loop, since dash runs the right side of a pipe in a subshell.
- **No `.storage` persistence.** Unlike `household_state` and
  `household_state`, this coordinator keeps its transport-failure streak
  counter (`_ssh_fails`) and its cached slow-block result in memory only.
  A restart resets the streak to zero and clears the cached slow block
  until the next successful 6-hourly read —
  `updates_pending`/`kernel_installed`/etc. read `unknown` for up to 6h
  after a restart, not because the host changed but because the cache
  did. (`sensor.<host>_reboot_count` is the exception: it is a
  `RestoreEntity` and is re-seeded from its own last state.)
- No restart is required to pick up a config change to `offline_expected`
  (options-flow reload handles it). A restart **is** required after
  editing `custom_components/` itself, per the same HA-wide rule named
  in `household_state.md`.

## 3. Process / dataflow summary

Every 60 seconds (`UPDATE_INTERVAL`) the coordinator runs one SSH round
trip (`FAST_CMD`), tracks a consecutive-miss streak, turns the returned
`KEY=value` text into typed readings via `_metrics()`, and — on a
separate 6-hour timer, retried at 15 minutes on failure — layers in the
slow SSH block. See
[`linux_monitor_process_flow.md`](linux_monitor_process_flow.md) for the
full trigger-to-entity control flow (including the `offline_expected`
short-circuit) and
[`linux_monitor_data_flow.md`](linux_monitor_data_flow.md) for exactly
which file feeds which entity attribute.

**CPU percent is the one reading that needs two samples.** `/proc/stat`
is cumulative jiffies since boot, so a percentage is a delta between two
instants and a single read can only produce a since-boot average. The
two samples are taken **inside one SSH round trip**, one second apart
(`CPU_SAMPLE_SECS`), rather than delta'd across polls. `const.py` argues
the choice at length; in short, the carried-delta alternative has no
previous sample after any restart and must report `None` for a full
minute, which is precisely the stale-zero shape this integration's
comments exist to prevent, and it would also silently change the
quantity from a few-second sample to a 60-second mean partway through an
existing recorder series.

The arithmetic is psutil's own — total excludes `guest`/`guest_nice`
(Linux already counts those inside `user`/`nice`, and they cancel out of
both sums), busy excludes `idle` and `iowait` — so the series keeps the
meaning it had when Glances computed it.

## 4. Entities produced

One device per configured host, named for the host's own hostname
(`entity.py`'s `DeviceInfo`). `_attr_has_entity_name = True`, so ids are
`sensor.<hostname>_<key>` / `binary_sensor.<hostname>_health` — e.g.
`sensor.ecldev01_cpu`.

**No entity_id or unique_id changed in 0.3.0.** `unique_id` is
`<configured hostname, lowercased>_<key>`; the transport swap moved where
each value comes *from* and touched neither half of that.

| Entity | Domain | Key attributes | Source |
|---|---|---|---|
| `sensor.<host>_cpu` | sensor | `%`, `MEASUREMENT` | two `/proc/stat` samples, one second apart in a single round trip |
| `sensor.<host>_memory` | sensor | `%`, `MEASUREMENT` | `/proc/meminfo` — `(MemTotal − MemAvailable) / MemTotal` |
| `sensor.<host>_temperature` | sensor | `°C`, device_class `temperature`; attr `source` | `/sys/class/hwmon/*/temp*_input` matched **by `name`** against `CPU_HWMON_NAMES`, else `/sys/class/thermal/thermal_zone*` by `type` |
| `sensor.<host>_disk` | sensor | `%`; attrs `device_name`, `fs_type`, `mnt_point`, `size`, `used`, `free` | `findmnt -rnb --real`, `/` mount (falls back to first entry if `/` absent) |
| `sensor.<host>_load_1m` | sensor, diagnostic | unitless, no threshold; attrs `min5`, `min15`, `cores` | `/proc/loadavg` fields 1–3; `cores` from `nproc` |
| `sensor.<host>_uptime` | sensor, diagnostic | device_class `timestamp` | `/proc/uptime`, converted to a boot instant rounded to the minute |
| `sensor.<host>_kernel_running` | sensor, diagnostic | — | SSH fast block `UNAME` (`uname -r`) |
| `sensor.<host>_kernel_installed` | sensor, diagnostic | — | SSH slow block `KERNEL_INSTALLED` (dpkg-query, `-unsigned` excluded) |
| `sensor.<host>_updates_pending` | sensor, diagnostic | `MEASUREMENT` | SSH slow block `UPGRADABLE` count |
| `sensor.<host>_security_updates_pending` | sensor, diagnostic | `MEASUREMENT`; attr `packages` (first 10 names) | SSH slow block `SECURITY` count |
| `sensor.<host>_apt_lists_age` | sensor, diagnostic | hours, `MEASUREMENT` | computed from SSH slow block `APT_LISTS_MTIME` |
| `sensor.<host>_reboot_count` | sensor, diagnostic | `TOTAL_INCREASING`; always available; `RestoreEntity` | count of observed `/proc/uptime` decreases (GH-402) |
| `binary_sensor.<host>_health` | binary_sensor | device_class `problem`; attrs `disposition`, `reasons`, `offline_expected`, `ssh_ok`, `ssh_missed_polls`, `transport_fail_dwell`, `host` | derived — see §5 |

`DeviceInfo` carries `manufacturer` (from `/etc/os-release`
`NAME`+`VERSION_ID`), `model` (from `/proc/cpuinfo` — the board name on
a Pi, the CPU string on x86) and `sw_version` (`uname -r`). There is
**no `configuration_url`**: it used to point at the Glances web UI, a
link that dies with the daemon, and a generic host has no other web
surface to offer. `model` is populated for the first time in 0.3.0 —
it previously read Glances' `quicklook.cpu_name`, but `quicklook` was
never in the fetched plugin list, so the value was always `None`.

**Every entity's `available` follows one rule** (`entity.py`): `False`
when `offline_expected` is set, otherwise `online and super().available`
— except `binary_sensor.<host>_health` and `sensor.<host>_reboot_count`,
which hard-override `available` to always `True`. This is the same "a
monitor must not disappear with its subject" principle
`household_state.md` documents; the readings
(cpu/memory/disk/etc.) are allowed to go unavailable when the host is
unreachable, since an unreadable CPU percentage has no honest
non-`unavailable` value to report.

## 5. Failure modes — by design, not by omission

| Situation | What happens | Why |
|---|---|---|
| `offline_expected` is set | Coordinator returns a static dict (`online: False`, `ssh: {}`, `metrics: {}`, streak reset to 0) **without attempting any network I/O**, and health scores clean rather than red | An intentionally-powered-down host is not a fault — no wasted poll, no accumulating fail streak |
| SSH exits non-zero, times out, or fails to spawn | `_ssh()` returns `None`; every reading goes unavailable, health dwells | `_ssh_raw` catches `OSError` on spawn and `asyncio.TimeoutError` on the wait, both mapped to the same `None` |
| SSH output is truncated (missing `__END__` marker) | `_parse_kv` returns `None`, not a partial dict | A truncated read must not look like a read that found nothing |
| One command inside the block fails | That key is absent or empty; its reader returns `None`, every other reading is unaffected | The `KEY=value` channel is per-field, and every typed reader in `coordinator.py` lands on `None` rather than a plausible value |
| Two identical `/proc/stat` samples, or a counter that went backwards | `_cpu_percent` returns `None`, **never `0.0`** | No elapsed time means there is no percentage. A zero here is the stale-zero shape the whole file exists to prevent, and it is asserted explicitly in `tools/test_linux_monitor_agentless.py` |
| A `hwmon` device answers `ENODATA` (e.g. `ecldev01`'s idle `iwlwifi_1` radio) | Guarded read, skipped; the next candidate is tried | An unguarded `cat` there emits an error line into the `KEY=value` channel. This is also why hwmon is matched **by name**, never positionally — an Intel box exposes `pch_cannonlake`, `hp`, `ucsi_source_psy_*` and `iwlwifi_1` alongside `coretemp` |
| SSH misses ≥3 consecutive polls (`TRANSPORT_FAIL_DWELL`) | `binary_sensor.<host>_health` reports a problem, `reasons` = `unreachable:<n>_polls`; a `_track()` warning is logged **once, at the crossing** | Three minutes is longer than a routine reboot or one transient ssh timeout; a warning repeating every 60s is as unreadable as no warning |
| SSH misses fewer than 3 polls | Health stays clean, `disposition` reads `transport_flap` | A flap is visible without being a fault |
| CPU or disk crosses threshold (90% each, `CPU_PROBLEM_PCT`/`DISK_PROBLEM_PCT`) | Health trips **immediately**, no dwell | "The host answered and the answer was wrong, nothing to wait for." Every real filesystem is checked, not just `/` |
| A filesystem's percent is `None` (unreadable row) | Not counted as over-threshold | `isinstance(pct, (int, float))` gates the comparison — an unreadable filesystem is not a full one |
| Coordinator's own read logic throws | Never raises `UpdateFailed` — every branch of `_async_update_data` returns a dict | Same RULE 1 as `household_state`: raising takes every entity unavailable, and attributes on an unavailable entity vanish, which is how a broken collector reads green |
| Slow SSH block fails | Cached `self._slow` is cleared to `{}`, `_slow_failed` is set, retried every 15 minutes instead of the full 6h window; a warning logs once at the failure and once at recovery | Same once-per-transition logging discipline as the fast transport |
| Debian `-unsigned` kernel-image twin installed alongside the signed one | Explicitly `grep -v -- '-unsigned$'`ed out of the `dpkg-query` candidate list before `sort -V` | Measured defect: the unsigned twin sorts as version-greater than its signed sibling under `-V`, producing a permanent false "kernel one version ahead" reading — verified live on `ecldev01` 2026-08-21 |
| A mount point contains whitespace | `findmnt -r` escapes it as `\x20`; filesystems are emitted as **indexed key groups** (`FS0_MNT=…`), never a delimited join, and unescaped in Python | LAW.md §4 — a value joined into a delimited channel must not be able to contain the delimiter, and a mount point can contain any byte but NUL and newline |
| `hostname` left blank in config flow, host unreachable over SSH, `offline_expected` unset | Config flow shows `cannot_connect`, entry is **not** created | Cannot assign a `unique_id` without a hostname, and an unreachable SSH credential is itself a reason not to create the entry — see the GH-470 row below |
| `hostname` left blank, `offline_expected` set | Config flow shows `hostname_required_offline` | An intentionally-offline host cannot be probed to fill the gap |
| Restart of Home Assistant | In-memory fail-streak resets to 0; cached slow-block data is lost until the next successful 6-hourly read. `reboot_count` survives (`RestoreEntity`) | No `.storage` persistence — see §2 |
| **GH-470, the defect that produced 0.3.0** | `eclprod01` read fully unavailable: its Glances was running in XML-RPC mode bound to loopback, **and** the `monitor` account did not exist on that host at all, so the SSH transport had never worked either | The entry was Glances-only in practice from creation, because the old config flow validated against Glances and never exercised SSH. One daemon misconfiguration took the whole host dark. Both halves are fixed: the daemon is gone, and the probe now uses the SSH credential the entry will poll with |

## 6. Current consumers (verified 2026-09-01)

- `www/kiosk-fleet-model.js` (`SOURCES` array) — **live**. Declares
  `linux_monitor` as a third platform alongside `systemmonitor` (the HA
  server itself) and `kiosk_pi`, discovered by the shared
  `discoverHosts()`/`assess()` pair. `kind` is `'host'` and gates behind
  `opts.includeHost` (default `false`), matching the `includeServer`
  gate. Its alias map reads only entity id **suffixes**
  (`binary_sensor.health`, `sensor.cpu`, `sensor.memory`, `sensor.disk`,
  `sensor.temperature`, `sensor.uptime`,
  `sensor.security_updates_pending`, `sensor.updates_pending`,
  `sensor.kernel_running`, `sensor.kernel_installed`) — every one of
  which 0.3.0 preserves — plus the health sensor's `disposition`
  attribute, also preserved.
  - Its `TRANSPORT` disposition table still lists a
    `glances_unreachable` key. That was never a `disposition` this
    integration emitted (only ever a `reasons` string), so it matched
    nothing before 0.3.0 and matches nothing now. Left alone
    deliberately rather than bundled into this change.
- `www/linux-compute-card.js` — **live**, and the only caller passing
  `includeHost: true`. Renders the non-kiosk Linux compute fleet as an
  exception list on `monitoring-card-base`, filtered to `kind === 'host'`
  so a kiosk row can never leak in.
- `www/claude-usage-card.js` — **not a consumer.** GH-421 moved
  `linux_monitor` rows off it to `linux-compute-card.js`, since that
  card's logo asserts every row is about Claude Code usage.
- `monitor-kiosk-pi-card` / `kiosk-pi-card` — **not consumers.** Neither
  references `includeHost` or `linux_monitor`, and the model defaults
  `includeHost` to `false`.
- No automation, script, or `packages/*.yaml` file reads any
  `linux_monitor` entity.
- **Not the same signal as `sensor.critical_networking_device_health`.**
  `packages/network_client_monitoring.yaml` tracks `ecldev01` under the
  UniFi-client-presence label `infra_adjacent_device` — a
  **network-presence** check (is the device on the LAN). `linux_monitor`
  is an **OS-health** check on the same physical box via a wholly
  different transport. The two never share code, an entity, or a
  computation.

## 7. Monitored hosts

Six entries as of 2026-09-01, all verified reporting over the SSH
transport: `ecldev01` (x86-64, Intel `coretemp`, `/dev/sda2` + EFI,
account `monitor`), `eclprod01` (**aarch64 — a Raspberry Pi 4 booting
from an SSD**, `cpu_thermal`, account `monitor`), and the four kiosk Pis
`PI3BKIOSK1`, `PI3BKIOSK2`, `PI4KIOSK03`, `PI4KIOSK04` (`cpu_thermal`,
`mmcblk` + `/boot/firmware`, account `kiosk`).

The two hardware classes disagree on exactly the two fields most likely
to break — which `hwmon` chip answers, and where the CPU model string
lives in `/proc/cpuinfo` — which is why
`tools/test_linux_monitor_agentless.py` carries a real captured fixture
from each rather than one.

## 8. Open tickets touching this integration

Query live before trusting this list — issues move. At time of writing:
**GH-470** (drop the Glances daemon dependency) is the change this
version *is*. **GH-402** (ecldev01 crash-loop invisible to the health
binary_sensor) is the reason `sensor.<host>_reboot_count` exists and
remains open as a dashboard-surfacing question. **GH-388** (monitor
ecldev01 OS health on the Network Monitor board) is closed.
