r"""The shell the host actually runs, run.

THESE TESTS EXECUTE THE SHIPPED TEXT, NOT A COPY OF IT. Every other module
here stops at the Python boundary and feeds the parsers output a person typed,
which is exactly how #20 shipped: the defect lived in the seam between what
dpkg writes and what the KEY=value contract assumed, and no hand-written
fixture has ever had a carriage return in it. The TAIL pipeline is pure shell
over a file, so it costs nothing to point it at a real dpkg-shaped log and
read the answer back -- LAW.md 9's channel rule, applied to the one channel
this suite could otherwise only describe.

The command line under test is LOCATED IN THE CONSTANT rather than restated,
so an edit to the shipped block reaches these tests and a test that has
quietly stopped covering it fails loudly instead. The value is then read back
through _parse_kv, because the shell and the parser are two halves of one
contract and #20 was in neither half alone.
"""

from __future__ import annotations

import subprocess

from custom_components.linux_monitor.coordinator import APT_UPGRADE_CMD, _parse_kv

# What dpkg and apt actually write into the upgrade log. Both progress
# displays use carriage returns, so each is ONE newline-delimited line that a
# terminal renders as its final fragment.
DPKG_PROGRESS = (
    "(Reading database ... 5%\r(Reading database ... 45%\r"
    "(Reading database ... 41234 files and directories currently installed.)"
)
APT_PROGRESS = "Progress: [  0%]\rProgress: [ 20%]\rProgress: [100%]"
DPKG_LOG = (
    "Reading package lists...\n"
    "Building dependency tree...\n"
    "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    f"{DPKG_PROGRESS}\n"
    "Preparing to unpack .../libssl3_3.5.2_amd64.deb ...\n"
    "Unpacking libssl3:amd64 (3.5.2) ...\n"
    f"{APT_PROGRESS}\n"
    "Setting up libssl3:amd64 (3.5.2) ...\n"
    "Processing triggers for libc-bin (2.41-12) ...\n"
)

# The pipeline as it shipped before #20, kept as a CONTROL. Without it these
# tests prove only that some pipeline produces some string; with it they prove
# this harness can tell the fixed one from the broken one on this fixture.
SHIPPED_BEFORE_20 = (
    """echo "TAIL=$(tail -5 "$LOG" | tr '\\n' ' ' | tr -s ' ' | cut -c1-400)\""""
)


def _tail_line() -> str:
    """The one line of APT_UPGRADE_CMD that builds TAIL.

    Anchored on the full statement prefix at its own indentation (column 0 --
    the block is not indented) and asserted unique, so a second TAIL echo or a
    renamed key fails here rather than silently testing the wrong line.
    """
    matches = [
        line for line in APT_UPGRADE_CMD.split("\n") if line.startswith('echo "TAIL=')
    ]
    assert len(matches) == 1, f"expected exactly one TAIL line, found {len(matches)}"
    return matches[0]


def _tail_value(line: str, log: str, tmp_path) -> str:
    """Run one line of the block against a real file and read TAIL back the
    way the integration does -- through _parse_kv, end marker and all."""
    path = tmp_path / "apt-upgrade.log"
    path.write_text(log, encoding="utf-8", newline="")
    # BYTES, AND DECODED HERE. subprocess's text mode is universal-newlines
    # mode: it rewrites every \r to \n on the way in, which would hand this
    # harness a clean payload no matter what the pipeline did and report the
    # broken control as fixed. Measured -- the control passed until this line
    # stopped asking Python to normalise the one character under test.
    out = subprocess.run(
        ["/bin/sh", "-c", f'LOG="$1"\n{line}', "sh", str(path)],
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout.decode("utf-8")
    parsed = _parse_kv(f"{out}__END__\n")
    assert parsed is not None and "TAIL" in parsed, out
    return parsed["TAIL"]


def test_the_tail_pipeline_returns_apt_s_last_five_rendered_lines(tmp_path) -> None:
    """The point of the attribute: the ordinary case needs nothing read on the
    host. Collapsing each line to what a terminal would leave showing is what
    makes the five lines five MESSAGES."""
    tail = _tail_value(_tail_line(), DPKG_LOG, tmp_path)

    assert "\r" not in tail
    assert tail == (
        "Preparing to unpack .../libssl3_3.5.2_amd64.deb ... "
        "Unpacking libssl3:amd64 (3.5.2) ... "
        "Progress: [100%] "
        "Setting up libssl3:amd64 (3.5.2) ... "
        "Processing triggers for libc-bin (2.41-12) ..."
    )
    # The apt progress line is IN the window and reads as its final state.
    # Every fragment it overwrote is gone rather than mashed in beside it.
    assert "20%" not in tail


def test_the_pipeline_this_replaced_fails_on_the_same_fixture(tmp_path) -> None:
    """THE SELF-TEST. A green assertion set that cannot go red proves nothing,
    so the control is run too: the shipped-before-#20 pipeline hands the
    overwritten progress fragments back with their carriage returns intact,
    which is the payload that then truncated at the parser."""
    tail = _tail_value(SHIPPED_BEFORE_20, DPKG_LOG, tmp_path)

    assert "\r" in tail
    assert "Progress: [ 0%]\rProgress: [ 20%]\rProgress: [100%]" in tail


def test_the_tail_pipeline_is_bounded_at_400_characters(tmp_path) -> None:
    """An entity attribute is not a log viewer. `last_install_log` names the
    file for the case that needs more than this."""
    tail = _tail_value(_tail_line(), ("x" * 200 + "\n") * 10, tmp_path)
    assert len(tail) == 400


def test_the_tail_pipeline_is_empty_rather_than_noisy_on_an_empty_log(
    tmp_path,
) -> None:
    """An upgrade that wrote nothing reports nothing. `last_install_tail` is
    read with `or None`, so an empty string lands on None at the entity."""
    assert _tail_value(_tail_line(), "", tmp_path) == ""
