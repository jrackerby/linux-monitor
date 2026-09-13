# Invention disclosure draft — linux_monitor

Siblings in this set: [`linux_monitor.md`](linux_monitor.md) — full
technical reference · [`linux_monitor_abstract.md`](linux_monitor_abstract.md) —
one-page plain-language summary · [`linux_monitor_process_flow.md`](linux_monitor_process_flow.md) —
control flow, execution order · [`linux_monitor_data_flow.md`](linux_monitor_data_flow.md) —
data lineage · [`linux_monitor_architecture.md`](linux_monitor_architecture.md) —
static module structure and system boundary.

> **This is an internal invention-disclosure draft, not a filed patent
> application and not legal advice.** It is written in the same
> patent-specification form as `household_alert_patent_disclosure.md`
> (numbered paragraphs, formal section headers) as a documentation
> exercise, per `jrackerby/HA` `docs/PROCESS.md`'s instruction that every integration
> in this set receives a disclosure regardless of outcome. No prior-art
> search, novelty opinion, or freedom-to-operate analysis has been
> performed.

## BACKGROUND

[0001] `custom_components/linux_monitor` (v0.3.0) polls a Debian host's
operating-system health — CPU, memory, disk, temperature, load, uptime,
kernel currency, and pending package updates — over a single read-only
SSH connection, and publishes the result as Home Assistant entities. It
is **agentless**: nothing is installed on a monitored host, no daemon
has to be running there, and the account used holds no sudo grant. Every
reading originates in a kernel-maintained, world-readable file (`/proc`,
`/sys`, `/etc/os-release`) or a stock Debian utility (`findmnt`,
`nproc`, `dpkg-query`, `apt`).

[0001a] Through v0.2.0 the component additionally required a Glances
REST daemon listening on each monitored host, as a second transport
alongside SSH. That dependency was removed in v0.3.0 (GH-470) after a
monitored host read fully unavailable: its Glances daemon had been
started in XML-RPC mode bound to loopback, and — separately — the SSH
account the component was configured to use had never been created on
that machine, a defect the then-current config flow could not detect
because it validated against Glances rather than against the transport
it would actually poll with. Both the agent dependency and the
unexercised-credential hole are gone; the disclosure below is retained
because the reapplication analysis it records is unchanged by which
transport supplies the numbers.

[0002] The component's own source repeatedly and explicitly identifies
its architecture as derived from a sibling integration in the same
repository, `custom_components/kiosk_pi`: `const.py`'s module docstring
states the design is "[m]odeled on custom_components/kiosk_pi's
coordinator/transport/dwell shape (... same 'never raise UpdateFailed'
honesty rule)";
`coordinator.py`'s module docstring states "[s]hape is kiosk_pi's
coordinator, trimmed to what a non-kiosk host needs"; and individual
design choices throughout the codebase (the strict
`KEY=value ... __END__` SSH output format so a truncated read cannot be
mistaken for an empty one; the
distinction between an immediate trip for a bad reading and a
dwell-gated trip for a missing one; the coordinator's refusal to ever
raise `UpdateFailed`) are each attributed in-line to the same reasoning
already present in `kiosk_pi`.

## SUMMARY

[0003] Every mechanism of potential technical interest in this
component — disposition-preserving handling of an unreadable source
(distinguishing "did not answer" from "answered zero"), an
availability-preserving coordinator that never allows a collection
failure to blank its own entities' attributes, and an asymmetric
trip-vs-dwell rule for a health indication (an out-of-range reading
trips immediately; a missing reading is held through a grace period
before it counts) — is a direct reapplication of a pattern this
codebase already implemented, in largely the same form, in `kiosk_pi`.
Where `household_alert_patent_disclosure.md` treats the
disposition-preserving read and the availability-preserving coordinator
as candidate claims (its First and Fifth aspects), those aspects were
themselves drawn from patterns already present in this repository's
device-monitoring integrations at the time that disclosure was written,
`kiosk_pi` foremost among them. `linux_monitor` supplies no mechanism
that is new relative to that existing pattern — it is a scoped-down
reimplementation of it for a different, non-kiosk device class, and its
own source says so directly rather than leaving that to be inferred.

[0004] The two design choices unique to `linux_monitor` relative to
`kiosk_pi` are both *removals* of capability (no remote patch/reboot,
no DHCP-based auto-discovery), argued in `const.py` on ordinary
risk-management and applicability grounds rather than as a technical
mechanism: a working host that administers the automation system itself
should not also expose a remote code-execution-adjacent control surface
back into itself, and a generic host has no naming convention to
pattern-match for discovery the way a fleet of identically-provisioned
Raspberry Pi kiosks does. Declining to build a feature, for a stated
operational reason, is not itself a patentable mechanism.

[0005] One implementation detail is a genuine, specific piece of
engineering judgment — the exclusion of Debian's `-unsigned` kernel
package variant from the "installed kernel" comparison, because it
sorts as version-greater than its own signed sibling under `sort -V`
and would otherwise produce a permanent false "reboot needed" reading
(`coordinator.py`'s `SLOW_CMD`, and verified live on `ecldev01`
2026-08-21 per that comment). This is a correctly-diagnosed,
well-documented bug fix for a specific vendor packaging quirk. It is
not, on its own, a novel *system or method* — it is one `grep -v`
filter applied to a `dpkg-query` result, narrow enough that it would not
support a claim independent of the surrounding, non-novel polling
architecture it sits inside.

## CLAIMS

No claims are asserted. The component's architecture is a direct,
in-scope reduction of an already-existing pattern within this same
codebase (`kiosk_pi`), acknowledged as such by the component's own
source comments rather than independently arrived at; its two points of
difference from that pattern are capability removals justified by
ordinary operational risk reasoning, not technical mechanisms; and its
one piece of specific engineering insight (the `-unsigned` kernel
exclusion) is a narrow, single-purpose bug fix for a documented vendor
packaging behavior, not a system or method of the scope patent claims
are drafted at.

## ABSTRACT

A read-only, agentless OS-health monitoring integration for Debian
hosts, polling a narrow SSH command set on a fixed interval — requiring
no software installed on, and no daemon running on, the monitored host —
and publishing CPU, memory, disk, temperature, uptime, kernel, and
pending-update readings as Home Assistant entities. The
integration's coordinator, transport, and health-disposition patterns
are a direct, acknowledged reapplication of an existing sibling
integration's architecture (`kiosk_pi`) to a non-kiosk device class,
with capability intentionally removed (no remote patch, install, reboot,
or auto-discovery) rather than added. No aspect of the design departs
from that existing, already-implemented pattern in a way that
constitutes a separate novel invention.

---

*Reference implementation: `custom_components/linux_monitor/`
(`const.py`, `coordinator.py`, `entity.py`, `sensor.py`,
`binary_sensor.py`), v0.3.0, as of 2026-09-01. See
`docs/integrations/linux_monitor.md` for the current operational
reference, `docs/integrations/household_alert_patent_disclosure.md` for
the disclosure covering the original device-monitoring pattern this
component reapplies, and `jrackerby/HA` `docs/PROCESS.md` for the documentation method
used across this repo's `docs/integrations/` tree.*
