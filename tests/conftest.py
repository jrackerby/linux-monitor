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
