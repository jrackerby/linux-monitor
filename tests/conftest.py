"""Make an integration that lives at the REPOSITORY ROOT importable as
`custom_components.linux_monitor`, which is the only name Home Assistant will
ever look for it under.

WHY THIS IS NEEDED AT ALL. hacs.json declares `content_in_root: true`: the
integration's modules sit at the top level of this repo rather than under
`custom_components/<domain>/`, because that is the layout HACS copies into
place. Every other repo's test suite gets this for free from its own directory
structure.

WHY IT IS A sys.path ENTRY AND NOT A CONFIG DIRECTORY. Home Assistant does not
discover custom integrations by reading its config directory -- loader.py's
_get_custom_components does `import custom_components` and then walks that
package's __path__. So the requirement is an importable `custom_components`
package on sys.path with this integration inside it, and pointing the test
config directory anywhere would not have helped. Measured against
homeassistant 2026.9.1's own loader.py rather than assumed.

The staging directory is built OUTSIDE the repository, on purpose: a
`custom_components/` created inside it would contain a symlink back to its own
parent, and anything walking the tree would recurse for ever.

This runs at import time rather than in a fixture because collection imports
the test modules -- and therefore the integration -- before any fixture runs.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

DOMAIN = "linux_monitor"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_staging = pathlib.Path(tempfile.mkdtemp(prefix="linux_monitor_tests_"))
_package_root = _staging / "custom_components"
# No __init__.py: `custom_components` is a namespace package in a real Home
# Assistant config directory too, and adding one here would be a difference
# between the test layout and the shipped one.
_package_root.mkdir()
(_package_root / DOMAIN).symlink_to(REPO_ROOT, target_is_directory=True)
sys.path.insert(0, str(_staging))


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """The staging tree is a symlink and a directory; remove the directory."""
    shutil.rmtree(_staging, ignore_errors=True)


# --- shared fixtures --------------------------------------------------------

import pytest  # noqa: E402

from custom_components.linux_monitor.const import (  # noqa: E402
    CONF_ALLOW_INSTALL,
    CONF_HOST,
    CONF_HOSTNAME,
    CONF_OFFLINE_EXPECTED,
    CONF_SSH_KEY,
    CONF_SSH_USER,
)
from custom_components.linux_monitor.const import DOMAIN as LM_DOMAIN  # noqa: E402

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
