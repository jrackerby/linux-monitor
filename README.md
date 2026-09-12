# Linux Monitor

Health and reachability for your Linux hosts, **entirely over SSH**.

An earlier version polled a Glances daemon; that is gone. Every reading now
comes over the SSH transport, so there is nothing to install on a monitored
host beyond an authorised key — which matters for hosts whose filesystems are
rebuilt on update. `LEGACY_CONF_GLANCES_PORT` survives only to migrate config entries
created before that change.

## What it creates

Platforms: `binary_sensor`, `button`, `sensor`, `update`. One device per
configured host.

The `update` entity puts the host in Home Assistant's own Updates panel. It
reads *current* as both halves of the question: nothing pending from apt, **and**
the running kernel is the newest installed kernel of its flavour. Those diverge
after a kernel upgrade — remediated on disk, still exploitable in memory — so an
entity that only counted packages would call such a host current. `reboot_required`
is not kernel-only either: a plain library upgrade leaves the superseded code
mapped in every process already running, so it also compares the host's boot time
against the moment an upgrade landed.

A host that has not reported reads **unknown**, never *up to date*. A host flagged
`offline_expected` reads as nothing-pending with `disposition: offline_expected` —
that is a statement that it was never asked, not a claim that it is patched.

## Configuration

Config flow, one entry per host. Required: host address, an
`offline_expected` flag, SSH user and SSH key. Optional: a display hostname.

`offline_expected` is load-bearing rather than cosmetic — a host declared
expected-offline does not raise, so declaring one carelessly *masks* its
disappearance rather than tolerating it.

### Remote patching and reboot

Off by default, per entry, under *Configure*. Everything above works over a
read-only SSH account with no sudo at all; this one option is the exception.

Turning it on adds an **Install** button for the host in the Updates panel — and
with it the panel's update-all path — and creates a **Reboot host** button. Install
runs `apt-get upgrade`, never `full-upgrade`, so it cannot remove a package; it
reboots only if apt exits 0, and packages held back are reported rather than
escalated. Held off, neither control exists at all rather than existing and
refusing.

Both need passwordless sudo for that entry's SSH account, so enabling the option
runs `sudo -n true` over that exact account, key and address first and refuses to
save if it does not answer. An unreachable host is a refusal too: the question is
whether the grant may be stored, and a host that could not be asked is not a yes.
Turning the option back off is never gated.

**Where the upgrade log goes.** `apt`'s output is written to
`~/.cache/linux_monitor/apt-upgrade.log` for the entry's SSH account — or to
`/var/tmp/linux_monitor-<uid>/apt-upgrade.log` if that account's home is not
writable. Not `/tmp`: a successful install reboots the host, and on a host with
`/tmp` on tmpfs that reboot erased the log of the only runs that reached it. You
should not normally need to read it. The update entity carries `last_install_log`
(the path the host actually chose) and `last_install_tail` (apt's closing lines)
as attributes after a run in the current Home Assistant session; a failed install
also puts the tail in the error it raises.

## Install

**Via HACS.** HACS → ⋮ → *Custom repositories* → `https://github.com/jrackerby/linux-monitor`,
category **Integration**. Install, restart Home Assistant, then add it under
*Settings → Devices & Services → Add Integration → "Linux Monitor"*.

The integration lives at the repository **root**, not under
`custom_components/`. `hacs.json` declares `content_in_root: true`, so HACS
copies the root into `/config/custom_components/linux_monitor/`.

## Removal

*Settings → Devices & Services → Linux Monitor → the host's entry → ⋮ →
**Delete***. That removes the entry, its device and all of its entities. To
remove the integration itself afterwards, uninstall it in HACS and restart.

**Deleting an entry destroys the only copy of that host's patch history.**
This integration keeps, per host, the moment each pending security package was
*first seen* and the moment a remote upgrade *last landed*, in its own store at
`/config/.storage/linux_monitor_pending_<hostname>`. That store belongs to the
config entry and goes with it.

Neither figure is recoverable afterwards. The recorder keeps the *published*
surface — what `security_age_days` read at each point — and not the rows behind
it, so a history query reconstructs a summary of the old answer rather than the
timestamps that produced it. Re-adding the host starts both clocks from zero,
which makes every pending package look as though it arrived today: it
under-reports patch age, the direction that hides the problem.

So if those ages matter, **copy that file somewhere before deleting the
entry**. Removing the subject is how you erase the subject.

Two things deliberately survive a delete, because they are yours and not this
integration's: the SSH key it was pointed at, and the `known_hosts` file. Note
also that re-adding a host mints **new entity IDs** — Home Assistant never
reclaims a released ID, so anything referring to the old ones needs updating.

## Branding

`brand/` carries this integration's own icon and logo — the Debian swirl for
the icon, the swirl-and-wordmark lockup for the logo, each at 1x and 2x, plus a
dark-optimised logo whose wordmark is white. Home Assistant 2026.3 and later
serve them from `/api/brands/integration/linux_monitor/<image>` and prefer them
over the brands CDN, so the integration, its config entries and its devices
carry the mark with nothing published to home-assistant/brands. Older cores
ignore the directory and fall back to the CDN's placeholder, which is what they
showed before.

There is no `dark_icon`: the swirl is a single crimson on transparency and
reads on either theme, and Home Assistant's fallback chain serves `icon.png`
when a dark variant is missing. Only the wordmark needed one.

The swirl and the wordmark are the Debian Project's marks, used here to name
what this integration reads — a Debian host, through `apt` and `dpkg-query`.
This is not a Debian project and carries no endorsement from it.

## Development

Issues and feature requests: **[jrackerby/linux-monitor/issues](https://github.com/jrackerby/linux-monitor/issues)**.

CI runs [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest)
and HACS validation on every push. hassfest scans `custom_components/*` and
takes no path argument, so `.github/workflows/validate.yml` stages this repo
into that layout before invoking it; the repo itself stays root-layout because
`hacs.json` declares `content_in_root: true`.

Pushing a `manifest.json` whose `version` has changed tags and publishes a
release automatically — that is the only supported way to cut one.
