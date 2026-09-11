"""The readers whose defects are silent by construction.

Every function here has the same contract and it is the reason they are
functions rather than inline float() calls: a failed read lands on None, never
on a plausible-looking value. A regression in any of them does not raise and
does not log -- it publishes a confident number that is wrong, which is the
class of defect this integration was written to refuse.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.linux_monitor.coordinator import (
    _boot_time,
    _cpu_percent,
    _distro,
    _f,
    _filesystems,
    _i,
    _mem_percent,
    _parse_kv,
    _temp_c,
    _unescape,
)


# --- _parse_kv --------------------------------------------------------------


def test_parse_kv_without_end_marker_is_none_not_empty() -> None:
    """THE CONTRACT. A truncated read must not present as a read that found
    nothing: None makes every caller refuse, {} would make them all report
    zeroes they never measured."""
    assert _parse_kv("A=1\nB=2\n") is None


def test_parse_kv_stops_at_the_marker() -> None:
    parsed = _parse_kv("A=1\n__END__\nB=2\n")
    assert parsed == {"A": "1"}


def test_parse_kv_keeps_equals_signs_inside_a_value() -> None:
    """TAIL carries apt's own words, which contain '=' routinely."""
    parsed = _parse_kv("TAIL=Setting up x (1=2) ...\n__END__\n")
    assert parsed == {"TAIL": "Setting up x (1=2) ..."}


def test_parse_kv_strips_the_trailing_space_the_tail_pipeline_leaves() -> None:
    """`tr '\\n' ' '` leaves one trailing space on every TAIL."""
    assert _parse_kv("TAIL=a b \n__END__\n") == {"TAIL": "a b"}


def test_parse_kv_ignores_a_line_with_no_separator() -> None:
    assert _parse_kv("noise\nA=1\n__END__\n") == {"A": "1"}


def test_parse_kv_keeps_an_empty_value_distinct_from_an_absent_key() -> None:
    """SECURITY_PKGS= means the host answered and had none; a missing key
    means nobody asked. security_pkgs() depends on telling them apart."""
    parsed = _parse_kv("SECURITY_PKGS=\n__END__\n")
    assert parsed is not None
    assert "SECURITY_PKGS" in parsed
    assert parsed["SECURITY_PKGS"] == ""


# --- typed readers ----------------------------------------------------------


def test_f_and_i_land_on_none_rather_than_a_plausible_zero() -> None:
    for bad in (None, "", "   ", "n/a", "unknown", [], {}):
        assert _f(bad) is None, bad
        assert _i(bad) is None, bad
    assert _f(" 1.5 ") == 1.5
    assert _i(" 7 ") == 7
    # A float string is not an int; refusing beats silently truncating.
    assert _i("7.9") is None


# --- cpu --------------------------------------------------------------------


def test_cpu_percent_over_a_real_delta() -> None:
    kv = {
        "CPU_TOT_0": "1000", "CPU_BUSY_0": "100",
        "CPU_TOT_1": "2000", "CPU_BUSY_1": "600",
    }
    assert _cpu_percent(kv) == 50.0


def test_cpu_percent_is_none_when_no_time_passed() -> None:
    """Identical samples mean there is no interval to divide by. 0.0 would be
    a claim that the host was idle, which was never measured."""
    kv = {
        "CPU_TOT_0": "1000", "CPU_BUSY_0": "100",
        "CPU_TOT_1": "1000", "CPU_BUSY_1": "100",
    }
    assert _cpu_percent(kv) is None


def test_cpu_percent_is_none_when_the_counters_went_backwards() -> None:
    """A host that rebooted between the two samples inside one round trip."""
    kv = {
        "CPU_TOT_0": "2000", "CPU_BUSY_0": "600",
        "CPU_TOT_1": "1000", "CPU_BUSY_1": "100",
    }
    assert _cpu_percent(kv) is None


def test_cpu_percent_is_none_on_a_partial_read() -> None:
    assert _cpu_percent({"CPU_TOT_0": "1000", "CPU_BUSY_0": "100"}) is None


def test_cpu_percent_is_capped_at_one_hundred() -> None:
    kv = {
        "CPU_TOT_0": "0", "CPU_BUSY_0": "0",
        "CPU_TOT_1": "100", "CPU_BUSY_1": "500",
    }
    assert _cpu_percent(kv) == 100.0


# --- memory -----------------------------------------------------------------


def test_mem_percent_uses_available_not_free() -> None:
    """psutil's definition, which is what the series has always meant. Using
    MemFree would make a healthy Linux box read as a permanent emergency."""
    assert _mem_percent({"MEM_TOTAL_KB": "1000", "MEM_AVAIL_KB": "250"}) == 75.0


def test_mem_percent_is_none_on_a_zero_total() -> None:
    assert _mem_percent({"MEM_TOTAL_KB": "0", "MEM_AVAIL_KB": "0"}) is None
    assert _mem_percent({}) is None


# --- filesystems ------------------------------------------------------------


def test_filesystems_percent_is_used_over_used_plus_available() -> None:
    """Not used over total. The gap is the root-reserved blocks, and using
    total instead under-reports every ext4 filesystem by about 5%."""
    kv = {
        "FS_N": "1",
        "FS0_DEV": "/dev/sda1", "FS0_TYPE": "ext4", "FS0_MNT": "/",
        "FS0_SIZE": "1000", "FS0_USED": "500", "FS0_FREE": "500",
    }
    rows = _filesystems(kv)
    assert len(rows) == 1
    assert rows[0]["percent"] == 50.0
    assert rows[0]["mnt_point"] == "/"


def test_filesystems_unescapes_a_mount_point_containing_a_space() -> None:
    """findmnt -r escapes whitespace; a mount point may contain any byte but
    NUL and newline, which is why these arrive as indexed key groups and never
    as a delimited join."""
    kv = {
        "FS_N": "1",
        "FS0_DEV": "/dev/sdb1", "FS0_TYPE": "ext4",
        "FS0_MNT": r"/mnt/my\x20disk",
        "FS0_SIZE": "10", "FS0_USED": "5", "FS0_FREE": "5",
    }
    assert _filesystems(kv)[0]["mnt_point"] == "/mnt/my disk"


def test_filesystems_skips_an_incomplete_row_and_keeps_the_rest() -> None:
    kv = {
        "FS_N": "2",
        "FS0_DEV": "/dev/sda1", "FS0_TYPE": "ext4", "FS0_MNT": "/",
        "FS0_SIZE": "100", "FS0_USED": "50", "FS0_FREE": "50",
        "FS1_DEV": "/dev/sda2", "FS1_TYPE": "ext4", "FS1_MNT": "/boot",
        # no SIZE/USED/FREE
    }
    rows = _filesystems(kv)
    assert [r["mnt_point"] for r in rows] == ["/"]


def test_filesystems_is_empty_when_nothing_was_read() -> None:
    assert _filesystems({}) == []
    assert _filesystems({"FS_N": "0"}) == []


def test_filesystems_percent_is_none_rather_than_zero_on_an_empty_device() -> None:
    kv = {
        "FS_N": "1",
        "FS0_DEV": "none", "FS0_TYPE": "tmpfs", "FS0_MNT": "/x",
        "FS0_SIZE": "0", "FS0_USED": "0", "FS0_FREE": "0",
    }
    assert _filesystems(kv)[0]["percent"] is None


# --- misc -------------------------------------------------------------------


def test_unescape_handles_every_escape_findmnt_emits() -> None:
    assert _unescape(r"a\x20b\x09c\x5cd") == "a b\tc\\d"


def test_temp_c_converts_millidegrees() -> None:
    assert _temp_c({"CPU_TEMP_MC": "45123"}) == 45.1
    assert _temp_c({}) is None


def test_distro_joins_name_and_version_and_is_none_when_empty() -> None:
    assert _distro({"DISTRO_NAME": "Debian GNU/Linux", "DISTRO_VERSION": "13"}) == (
        "Debian GNU/Linux 13"
    )
    assert _distro({"DISTRO_NAME": "", "DISTRO_VERSION": ""}) is None


def test_boot_time_is_rounded_to_the_minute() -> None:
    """Unrounded, /proc/uptime's sub-second precision would rewrite a
    TIMESTAMP entity's state once a minute for ever."""
    boot = _boot_time(3600.4)
    assert isinstance(boot, datetime)
    assert boot.second == 0 and boot.microsecond == 0


def test_boot_time_is_none_without_an_uptime() -> None:
    assert _boot_time(None) is None


def test_boot_time_moves_with_uptime() -> None:
    earlier = _boot_time(7200.0)
    later = _boot_time(60.0)
    assert earlier is not None and later is not None
    assert later - earlier >= timedelta(minutes=55)
