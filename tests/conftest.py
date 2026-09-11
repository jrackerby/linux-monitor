"""Fixtures, and the one assertion that makes a confusing layout fail loudly.

THIS REPOSITORY CANNOT BE TESTED IN PLACE, and that is a property of the
layout rather than a shortcoming of the suite. hacs.json declares
`content_in_root`, so this integration's package __init__.py sits at the
REPOSITORY ROOT -- which means the root is a Python package, and pytest turns
every directory holding an __init__.py into a Package collector whose setup()
imports that __init__.py (`_pytest/python.py`, pytest_collect_directory and
Package.setup). Collected from the repo root, pytest therefore imports this
integration's own __init__.py as a top-level module with no parent, and its
`from .const import ...` raises ImportError before a single test body runs.

Measured rather than reasoned about: a tripwire in the root __init__.py fires
at "ERROR at setup of test_trivial" from a bare clone, and does not fire from
the staged layout. It is invisible under `--collect-only`, because setup()
never runs there -- which is exactly how a first CI run reported 63 tests
collected and then failed the moment the suite actually ran.

So the suite runs against a STAGED layout -- the integration copied to
custom_components/<domain>/ under a workspace root that is not itself a
package -- which is also the layout every Home Assistant tool expects and the
one the hassfest job in this repo's workflow already builds for the same
reason. The `tests` job in .github/workflows/validate.yml is the definition.
"""

from __future__ import annotations

import pytest

try:
    import custom_components.linux_monitor  # noqa: F401
except ModuleNotFoundError as err:  # pragma: no cover - a layout fault
    # DISCRIMINATE, do not assume. A missing dependency raises through here
    # too, and reporting "your layout is wrong" over an absent homeassistant
    # would send the next person to fix the one thing that is not broken.
    # ModuleNotFoundError.name says which module was actually missing.
    if (err.name or "").split(".")[0] != "custom_components":
        raise
    raise RuntimeError(
        "custom_components.linux_monitor is not importable, so this suite is "
        "being run against the repository root rather than the staged layout. "
        "Running pytest from a bare clone of this repo does not work and "
        "cannot be made to -- see this module's docstring. Build the layout "
        "the `tests` job in .github/workflows/validate.yml builds, and run "
        "pytest from there."
    ) from err

# --- shared fixtures --------------------------------------------------------

from custom_components.linux_monitor.const import (
    CONF_ALLOW_INSTALL,
    CONF_HOST,
    CONF_HOSTNAME,
    CONF_OFFLINE_EXPECTED,
    CONF_SSH_KEY,
    CONF_SSH_USER,
)
from custom_components.linux_monitor.const import DOMAIN as LM_DOMAIN

HOST = "203.0.113.5"
HOSTNAME = "testhost"
SSH_USER = "monitor"
SSH_KEY = "/config/.ssh/test_key"

ENTRY_DATA = {
    CONF_HOST: HOST,
    CONF_HOSTNAME: HOSTNAME,
    CONF_SSH_USER: SSH_USER,
    CONF_SSH_KEY: SSH_KEY,
}


@pytest.fixture
def creds() -> dict[str, str]:
    """The credential an entry is built with.

    A FIXTURE RATHER THAN AN IMPORTABLE CONSTANT, and that is not style. With
    `from .conftest import HOST` in a test module, Python has to resolve
    `tests` as a package -- which makes pytest walk up looking for the package
    root, find the __init__.py that IS this integration (content_in_root), and
    try to import it as a top-level module. Measured: 114 collection errors,
    every one of them "attempted relative import with no known parent
    package", pointing at the repo's own __init__.py. tests/ carries no
    __init__.py for the same reason.
    """
    return {
        "host": HOST,
        "hostname": HOSTNAME,
        "ssh_user": SSH_USER,
        "ssh_key": SSH_KEY,
    }


@pytest.fixture
def user_input(creds) -> dict:
    """What the user step is handed, in the shape its schema declares."""
    return {
        CONF_HOST: creds["host"],
        CONF_HOSTNAME: "",
        CONF_SSH_USER: creds["ssh_user"],
        CONF_SSH_KEY: creds["ssh_key"],
        CONF_OFFLINE_EXPECTED: False,
    }


@pytest.fixture
def entry_factory():
    """A config entry in this integration's CURRENT shape (version 2)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    def _make(*, options=None, data=None, **kwargs):
        return MockConfigEntry(
            domain=LM_DOMAIN,
            title=HOSTNAME,
            unique_id=HOSTNAME.lower(),
            version=2,
            data={**ENTRY_DATA, **(data or {})},
            options={
                CONF_OFFLINE_EXPECTED: False,
                CONF_ALLOW_INSTALL: False,
                **(options or {}),
            },
            **kwargs,
        )

    return _make


@pytest.fixture
def coordinator_factory(hass, entry_factory):
    """A REAL coordinator, with only the ssh subprocess replaced.

    Built directly rather than through async_setup_entry: everything under
    test here is the coordinator's own logic, and a full entry setup would
    drag in four platforms and an entity registry to prove nothing extra.
    """
    from custom_components.linux_monitor.coordinator import LinuxMonitorCoordinator

    def _make(*, options=None, data=None):
        entry = entry_factory(options=options, data=data)
        entry.add_to_hass(hass)
        return LinuxMonitorCoordinator(hass, entry)

    return _make


# --- a host that answers ----------------------------------------------------
#
# Realistic output for both collector blocks, so a mounted platform produces
# the values a real one would rather than a wall of None. The numbers are
# chosen to make the arithmetic checkable by eye: cpu 50%, memory 25%, disk
# 40%, temperature 45.1C.

FAST_OUT = """CPU_TOT_0=1000
CPU_BUSY_0=100
HOSTNAME=testhost
UNAME=6.12.101+deb13-amd64
UPTIME_SECS=9000.0
CORES=4
LOAD1=0.15
LOAD5=0.20
LOAD15=0.25
MEM_TOTAL_KB=8000000
MEM_AVAIL_KB=6000000
DISTRO_NAME=Debian GNU/Linux
DISTRO_VERSION=13
CPU_MODEL=Intel(R) Core(TM) i5-9500T
FS0_DEV=/dev/sda1
FS0_TYPE=ext4
FS0_MNT=/
FS0_SIZE=100000
FS0_USED=40000
FS0_FREE=60000
FS_N=1
CPU_TEMP_MC=45123
CPU_TEMP_SRC=coretemp
CPU_TOT_1=2000
CPU_BUSY_1=600
__END__
"""

SLOW_OUT = """KERNEL_INSTALLED=6.12.101+deb13-amd64
UPGRADABLE=4
SECURITY=2
SECURITY_PKGS=libssl3,curl
UNATTENDED=1
APT_LISTS_MTIME=1757000000
__END__
"""


def ssh_answers(fast: str = FAST_OUT, slow: str = SLOW_OUT, rc: int = 0):
    """A stand-in for _ssh_raw that answers the two blocks differently.

    Dispatches on a token only the slow block carries. Returning one canned
    payload for both would let the slow block's keys leak into the fast read
    and vice versa, which is precisely the confusion the strict KEY=value
    contract exists to prevent -- a test that does it is not exercising the
    split it claims to.
    """

    async def _raw(script, timeout):  # noqa: ARG001
        if "KERNEL_INSTALLED" in script:
            return rc, slow, ""
        return rc, fast, ""

    return _raw


@pytest.fixture
def register_integration(hass):
    """Put this integration into the loader's cache DIRECTLY, rather than
    leaving Home Assistant to go and find it.

    phcc's `enable_custom_integrations` works by popping the cache key so the
    loader rescans, and that rescan runs `import custom_components` in an
    EXECUTOR THREAD while pytest is importing modules on the main one. Under
    that fixture the mounted tests failed with "Integration not found" -- but
    not all of them, and not the same ones: one passed while both its
    neighbours failed, which is the signature of a race rather than a missing
    file. (Measured separately: `custom_components` resolves to a proper
    namespace package with this integration inside it, so the scan had
    everything it needed to succeed.)

    resolve_from_root is the same classmethod that scan would have called, so
    this seeds exactly what it would have produced and nothing else -- it
    removes the timing, not a check. It is deliberately NOT autouse: it
    depends on `hass`, and making every test build one to register something
    it never loads would be slower for no reason.
    """
    import custom_components
    from homeassistant import loader

    from custom_components.linux_monitor.const import DOMAIN

    integration = loader.Integration.resolve_from_root(
        hass, custom_components, DOMAIN
    )
    assert integration is not None, (
        "the integration could not be resolved out of the custom_components "
        "namespace package -- the staged layout is wrong, not the test"
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {DOMAIN: integration}
    return integration


@pytest.fixture
def mounted(hass, entry_factory, register_integration):
    """An entry set up for real, through async_setup_entry, with all four
    platforms mounted and only the ssh subprocess replaced.

    This is the only fixture that exercises the wiring rather than a class in
    isolation: the platform forwards, the entity registry, DeviceInfo, and the
    availability overrides LAW.md §11 contracts for.
    """
    from unittest.mock import patch

    from custom_components.linux_monitor.coordinator import LinuxMonitorCoordinator

    async def _make(*, options=None, data=None, raw=None):
        entry = entry_factory(options=options, data=data)
        entry.add_to_hass(hass)
        with patch.object(
            LinuxMonitorCoordinator, "_ssh_raw", raw or ssh_answers()
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return entry

    return _make
