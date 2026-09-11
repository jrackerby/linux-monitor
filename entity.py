"""Shared entity base -- DeviceInfo is defined once, here."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LinuxMonitorCoordinator


class LinuxMonitorEntity(CoordinatorEntity[LinuxMonitorCoordinator]):
    """_attr_has_entity_name makes the entity_id <device_slug>_<key>, e.g.
    sensor.<host>_cpu.

    unique_id is <configured hostname, lowercased>_<key> and is FROZEN. It is
    built from entry.data's hostname and the platform's own key, neither of
    which the Glances removal touched, so every entity keeps the id it was
    registered under.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: LinuxMonitorCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.hostname.lower()}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """manufacturer, model and sw_version all come off the ssh read now.

        NO configuration_url. It used to point at http://<host>:61208/, the
        Glances web UI -- a link that dies with the daemon this integration
        no longer needs, and a generic monitored host has no other web
        surface to offer. A dead link on a device page is worse than none.

        model is populated for the first time here. It read Glances'
        quicklook.cpu_name, but `quicklook` was never in the fetched plugin
        list, so the key was always absent and the model was always None.
        """
        c = self.coordinator
        metrics = (c.data or {}).get("metrics") or {}

        return DeviceInfo(
            identifiers={(DOMAIN, c.hostname.lower())},
            name=c.hostname,
            manufacturer=metrics.get("distro") or "Linux",
            model=metrics.get("cpu_model"),
            sw_version=metrics.get("kernel"),
        )

    @property
    def available(self) -> bool:
        """Individual readings go unavailable when their source is missing.
        The health binary_sensor and the reboot counter override this -- see
        binary_sensor.py and sensor.py."""
        data = self.coordinator.data or {}
        if data.get("offline_expected"):
            return False
        return bool(data.get("online")) and super().available
