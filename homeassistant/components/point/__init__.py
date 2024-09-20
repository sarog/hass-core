"""Support for Minut Point."""

import asyncio
from dataclasses import dataclass
from http import HTTPStatus
import logging

from aiohttp import ClientError, ClientResponseError, web
from pypoint import PointSession
import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_WEBHOOK_ID,
    Platform,
)
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    aiohttp_client,
    config_entry_oauth2_flow,
    config_validation as cv,
    device_registry as dr,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.dt import as_local, parse_datetime, utc_from_timestamp

from . import api
from .const import (
    CONF_WEBHOOK_URL,
    DOMAIN,
    EVENT_RECEIVED,
    POINT_DISCOVERY_NEW,
    SCAN_INTERVAL,
    SIGNAL_UPDATE_ENTITY,
    SIGNAL_WEBHOOK,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

type PointConfigEntry = ConfigEntry[PointData]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_CLIENT_ID): cv.string,
                vol.Required(CONF_CLIENT_SECRET): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Minut Point component."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]

    async_create_issue(
        hass,
        HOMEASSISTANT_DOMAIN,
        f"deprecated_yaml_{DOMAIN}",
        breaks_in_ha_version="2025.4.0",
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
        translation_placeholders={
            "domain": DOMAIN,
            "integration_title": "Point",
        },
    )

    if not hass.config_entries.async_entries(DOMAIN):
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential(
                conf[CONF_CLIENT_ID],
                conf[CONF_CLIENT_SECRET],
            ),
        )

        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=conf
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: PointConfigEntry) -> bool:
    """Set up Minut Point from a config entry."""

    if "auth_implementation" not in entry.data:
        raise ConfigEntryAuthFailed("Authentication failed. Please re-authenticate.")

    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    auth = api.AsyncConfigEntryAuth(
        aiohttp_client.async_get_clientsession(hass), session
    )

    try:
        await auth.async_get_access_token()
    except ClientResponseError as err:
        if err.status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise ConfigEntryAuthFailed from err
        raise ConfigEntryNotReady from err
    except ClientError as err:
        raise ConfigEntryNotReady from err

    point_session = PointSession(auth)

    client = MinutPointClient(hass, entry, point_session)
    hass.async_create_task(client.update())
    entry.runtime_data = PointData(client)

    await async_setup_webhook(hass, entry, point_session)
    # Entries are added in the client.update() function.

    return True


async def async_setup_webhook(
    hass: HomeAssistant, entry: PointConfigEntry, session: PointSession
) -> None:
    """Set up a webhook to handle binary sensor events."""
    if CONF_WEBHOOK_ID not in entry.data:
        webhook_id = webhook.async_generate_id()
        webhook_url = webhook.async_generate_url(hass, webhook_id)
        _LOGGER.debug("Registering new webhook at: %s", webhook_url)

        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_WEBHOOK_ID: webhook_id,
                CONF_WEBHOOK_URL: webhook_url,
            },
        )

    await session.update_webhook(
        webhook.async_generate_url(hass, entry.data[CONF_WEBHOOK_ID]),
        entry.data[CONF_WEBHOOK_ID],
        ["*"],
    )
    webhook.async_register(
        hass, DOMAIN, "Point", entry.data[CONF_WEBHOOK_ID], handle_webhook
    )


async def async_unload_entry(hass: HomeAssistant, entry: PointConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, [*PLATFORMS, Platform.ALARM_CONTROL_PANEL]
    ):
        session: PointSession = entry.runtime_data.client
        if CONF_WEBHOOK_ID in entry.data:
            webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
            await session.remove_webhook()
    return unload_ok


async def handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> None:
    """Handle webhook callback."""
    try:
        data = await request.json()
        _LOGGER.debug("Webhook %s: %s", webhook_id, data)
    except ValueError:
        return

    if isinstance(data, dict):
        data["webhook_id"] = webhook_id
        async_dispatcher_send(hass, SIGNAL_WEBHOOK, data, data.get("hook_id"))
    hass.bus.async_fire(EVENT_RECEIVED, data)


class MinutPointClient:
    """Get the latest data and update the states."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, session: PointSession
    ) -> None:
        """Initialize the Minut data object."""
        self._known_devices: set[str] = set()
        self._known_homes: set[str] = set()
        self._hass = hass
        self._config_entry = config_entry
        self._is_available = True
        self._client = session

        async_track_time_interval(self._hass, self.update, SCAN_INTERVAL)

    async def update(self, *args):
        """Periodically poll the cloud for current state."""
        await self._sync()

    async def _sync(self):
        """Update local list of devices."""
        if not await self._client.update():
            self._is_available = False
            _LOGGER.warning("Device is unavailable")
            async_dispatcher_send(self._hass, SIGNAL_UPDATE_ENTITY)
            return

        async def new_device(device_id, platform):
            """Load new device."""
            async_dispatcher_send(
                self._hass, POINT_DISCOVERY_NEW.format(platform, DOMAIN), device_id
            )

        self._is_available = True
        for home_id in self._client.homes:
            if home_id not in self._known_homes:
                await self._hass.config_entries.async_forward_entry_setups(
                    self._config_entry, [Platform.ALARM_CONTROL_PANEL]
                )
                await new_device(home_id, "alarm_control_panel")
                self._known_homes.add(home_id)
        for device in self._client.devices:
            if device.device_id not in self._known_devices:
                await self._hass.config_entries.async_forward_entry_setups(
                    self._config_entry, PLATFORMS
                )
                for platform in PLATFORMS:
                    await new_device(device.device_id, platform)
                self._known_devices.add(device.device_id)
        async_dispatcher_send(self._hass, SIGNAL_UPDATE_ENTITY)

    def device(self, device_id):
        """Return device representation."""
        return self._client.device(device_id)

    def is_available(self, device_id):
        """Return device availability."""
        if not self._is_available:
            return False
        return device_id in self._client.device_ids

    async def remove_webhook(self):
        """Remove the session webhook."""
        return await self._client.remove_webhook()

    @property
    def homes(self):
        """Return known homes."""
        return self._client.homes

    async def async_alarm_disarm(self, home_id):
        """Send alarm disarm command."""
        return await self._client.alarm_disarm(home_id)

    async def async_alarm_arm(self, home_id):
        """Send alarm arm command."""
        return await self._client.alarm_arm(home_id)


class MinutPointEntity(Entity):  # pylint: disable=hass-enforce-class-module # see PR 118243
    """Base Entity used by the sensors."""

    _attr_should_poll = False

    def __init__(self, point_client, device_id, device_class) -> None:
        """Initialize the entity."""
        self._async_unsub_dispatcher_connect = None
        self._client = point_client
        self._id = device_id
        self._name = self.device.name
        self._attr_device_class = device_class
        self._updated = utc_from_timestamp(0)
        self._attr_unique_id = f"point.{device_id}-{device_class}"
        device = self.device.device
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, device["device_mac"])},
            identifiers={(DOMAIN, device["device_id"])},
            manufacturer="Minut",
            model=f"Point v{device['hardware_version']}",
            name=device["description"],
            sw_version=device["firmware"]["installed"],
            via_device=(DOMAIN, device["home"]),
        )
        if device_class:
            self._attr_name = f"{self._name} {device_class.capitalize()}"

    def __str__(self) -> str:
        """Return string representation of device."""
        return f"MinutPoint {self.name}"

    async def async_added_to_hass(self):
        """Call when entity is added to hass."""
        _LOGGER.debug("Created device %s", self)
        self._async_unsub_dispatcher_connect = async_dispatcher_connect(
            self.hass, SIGNAL_UPDATE_ENTITY, self._update_callback
        )
        await self._update_callback()

    async def async_will_remove_from_hass(self):
        """Disconnect dispatcher listener when removed."""
        if self._async_unsub_dispatcher_connect:
            self._async_unsub_dispatcher_connect()

    async def _update_callback(self):
        """Update the value of the sensor."""

    @property
    def available(self):
        """Return true if device is not offline."""
        return self._client.is_available(self.device_id)

    @property
    def device(self):
        """Return the representation of the device."""
        return self._client.device(self.device_id)

    @property
    def device_id(self):
        """Return the id of the device."""
        return self._id

    @property
    def extra_state_attributes(self):
        """Return status of device."""
        attrs = self.device.device_status
        attrs["last_heard_from"] = as_local(self.last_update).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return attrs

    @property
    def is_updated(self):
        """Return true if sensor have been updated."""
        return self.last_update > self._updated

    @property
    def last_update(self):
        """Return the last_update time for the device."""
        return parse_datetime(self.device.last_update)


@dataclass
class PointData:
    """Point Data."""

    client: MinutPointClient
    entry_lock: asyncio.Lock = asyncio.Lock()
