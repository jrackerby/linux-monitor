# Linux Monitor

Health and reachability for the estate's Linux hosts, **entirely over SSH**.

An earlier version polled a Glances daemon; that is gone. Every reading now
comes over the SSH transport, so there is nothing to install on a monitored
host beyond an authorised key — which matters for hosts whose filesystems are
rebuilt on update. `LEGACY_CONF_GLANCES_PORT` survives only to migrate config entries
created before that change.

## What it creates

Platforms: `binary_sensor`, `sensor`. One device per configured host.

## Configuration

Config flow, one entry per host. Required: host address, an
`offline_expected` flag, SSH user and SSH key. Optional: a display hostname.

`offline_expected` is load-bearing rather than cosmetic — a host declared
expected-offline does not raise, so declaring one carelessly *masks* its
disappearance rather than tolerating it.

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
