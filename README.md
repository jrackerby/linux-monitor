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

## Install

**Via HACS.** HACS → ⋮ → *Custom repositories* → `https://github.com/jrackerby/linux-monitor`,
category **Integration**. Install, restart Home Assistant, then add it under
*Settings → Devices & Services → Add Integration → "Linux Monitor"*.

The integration lives at the repository **root**, not under
`custom_components/`. `hacs.json` declares `content_in_root: true`, so HACS
copies the root into `/config/custom_components/linux_monitor/`.

## Development

Issues and feature requests: **[jrackerby/linux-monitor/issues](https://github.com/jrackerby/linux-monitor/issues)**.

CI runs [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest)
and HACS validation on every push. hassfest scans `custom_components/*` and
takes no path argument, so `.github/workflows/validate.yml` stages this repo
into that layout before invoking it; the repo itself stays root-layout because
`hacs.json` declares `content_in_root: true`.

Pushing a `manifest.json` whose `version` has changed tags and publishes a
release automatically — that is the only supported way to cut one.
