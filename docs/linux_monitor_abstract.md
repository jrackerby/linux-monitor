# linux_monitor — abstract

Siblings in this set: technical doc — full reference · process flow —
what runs in what order · data flow — where each number comes from ·
architecture — how the pieces fit together · patent disclosure —
whether anything here is a novel invention.

## What this is

A small watchman for ordinary computers — not smart-home gadgets, just
regular Debian machines that happen to sit on the same network as the
house's automation system. It keeps an eye on the basics any computer
owner would want to know: is it too hot, is it running out of memory or
disk space, is it behind on security patches, and — most basically — is
it even answering when asked.

## Why it exists

The house already has a well-developed way of watching over its fleet of
wall-mounted display tablets. But some machines on the network aren't
tablets — one is a working computer used to make changes to the house's
automation itself, another is the box that serves the household's
dashboards. Those deserve the same watchful eye, but they don't have a
screen to keep clean, a browser to restart, or a wall-mount to check the
placement of. Bolting them onto the tablet-watching system would have
meant either stretching that system's assumptions past their breaking
point, or quietly ignoring the alarms it would raise for features these
machines don't have. So this is its own, smaller watchman, built for
exactly what a plain computer needs and nothing more.

It has since grown one step further: the display tablets' *own* basic
computer health — temperature, memory, disk — is now measured by this
watchman too, so there is one answer to "how is that machine doing"
rather than two subtly different ones. The tablet-watching system kept
everything genuinely about being a tablet, and handed over the rest.

## How it works, in plain terms

Once a minute, the watchman logs into the monitored computer through a
narrow, carefully limited remote login and reads a handful of files the
operating system already keeps about itself. It installs nothing on the
machine, and nothing has to be left running there for this to work.
The account it logs in as holds no administrative privileges whatsoever
— every number it collects comes from a file any user on that machine
could already read.

**That last point is the whole story of the most recent change.** Until
now the watchman also depended on a separate reporting program having
been installed and left running on each monitored machine. That program
was a liability rather than a help: one of the monitored computers went
completely dark, and the cause turned out to be that its copy of that
program had been started in the wrong mode and was only listening to
itself. A file the operating system maintains cannot be misconfigured,
cannot crash, cannot be forgotten during a rebuild, and needs nobody to
keep it running. So the extra program was removed, and everything the
watchman reports now comes down the one connection it already had open.

Removing it also revealed a second, quieter fault. The machine that went
dark had *never* had a working remote login either — the account the
watchman was meant to use had simply never been created there. Nobody
noticed, because the setup step that was supposed to check the login had
only ever checked the reporting program instead. Now it checks the thing
it will actually use.

It never gains the ability to reboot these machines, install anything on
them, or change how they run; several times over, its own design notes
point out that a "watch only, never touch" boundary is deliberate,
because one of these computers is also the one used to make changes to
the house's setup, and handing a watchman the power to reach back in and
restart the very hand that built it would be an unnecessary risk with no
present need.

A slower question — about how many software updates are waiting and
whether the currently-running system software is the newest one
installed — only gets asked once every six hours, because it is a
heavier question to ask and doesn't change from minute to minute.

## The one genuinely awkward measurement

"How busy is the processor right now" is the only reading that cannot be
answered by looking once. The operating system doesn't record a
percentage; it records a running total of time spent working since the
machine was switched on. A percentage is the *difference* between two
such readings, so it takes two looks, a moment apart.

The watchman takes both looks during the same visit, a second apart,
rather than comparing this minute's look against last minute's. The
tempting alternative — remember the previous reading and subtract — is
free, but it has no previous reading to work from immediately after any
restart, and it would then have to admit it doesn't know for a full
minute. Experience in this system is that a value which has to
*remember* to say "I don't know" eventually says "zero" instead, and a
falsely calm zero is far more dangerous than an honest gap.

## When things go wrong

If the connection stops answering for a few minutes in a row, the
watchman doesn't panic on the first missed reply — a machine can miss
one check for an ordinary, harmless reason. But once the silence
stretches past a short, deliberate grace period, it plainly reports
"not reachable," attaching a reason and a count of how long it's been
quiet, rather than just quietly failing to say anything at all. If the
machine answers but the answer itself is bad news — dangerously low on
disk space, for instance — it reports that immediately, with no grace
period, because a bad answer already contains everything needed to act
on it; there is nothing to wait for.

Throughout, an unreadable value is reported as *unknown*, never as a
plausible-looking number. This is stated here because it is the single
most expensive mistake this kind of program can make: a monitor that
reports a comfortable zero when it has actually failed to look is worse
than no monitor at all.

The one status the watchman produces never simply disappears, even when
the machine it's watching goes silent — a watchman that vanishes exactly
when its subject goes quiet is the failure this whole design is built to
avoid.

## Where it fits

The watchman now covers six machines: two general-purpose computers and
the four that drive the wall displays. Its findings appear as rows on a
wall-mounted status display elsewhere in the house, alongside other
technology-health information, so a glance at that display is enough to
know whether any of them needs attention. This is a separate,
unrelated fact from whether those same computers are currently connected
to the home network — a different watchman already answers that
question, for a different reason.
