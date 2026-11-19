import voluptuous as vol
from homeassistant import config_entries, core
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from .protocol import SsdpDiscovery
from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ENABLE_WASHER_POLL,
    CONF_WASHER_POLL_INTERVAL,
    DEFAULT_ENABLE_WASHER_POLL,
    DEFAULT_WASHER_POLL_INTERVAL,
    MIN_WASHER_POLL_INTERVAL,
    CONF_ENABLE_PERIODIC_POLL,
    DEFAULT_ENABLE_PERIODIC_POLL,
    CONF_TV_CONTROL_MODE,
    DEFAULT_TV_CONTROL_MODE,
    CONTROL_MODE_LOCAL,
    CONTROL_MODE_CLOUD,
)
from .pyhisenseapi import HiSenseLogin

class HisenseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @core.callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return HisenseOptionsFlowHandler(config_entry)

    def __init__(self):
        self._home_options = None
        self._access_token = None
        self._refresh_token = None
        self._customer_id = None
        self._device_info = None
        self._username = None
        self._password = None
        self._reauth_username = ""

    async def async_step_user(self, user_input=None):
        """Step 1: Choose mode."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["lan_tv", "login"],
        )

    async def async_step_lan_tv(self, user_input=None):
        """Step 2: Add Local TV without cloud account."""
        errors = {}

        if user_input is not None:
            host = (user_input.get("tv_ip") or user_input.get("discovered_tv") or "").strip()
            if not host:
                errors["base"] = "invalid_ip"
            else:
                import ipaddress
                try:
                    ipaddress.IPv4Address(host)
                except ValueError:
                    errors["base"] = "invalid_ip"

            if not errors:
                from .protocol import DlnaClient
                client = DlnaClient(host, session=async_get_clientsession(self.hass))
                connected = await client.test_connect(timeout=2.5)
                if not connected:
                    errors["base"] = "cannot_connect"
                else:
                    udn = f"hisense_tv_{host}"
                    name = f"海信电视 ({host})"
                    try:
                        info = await client.connect()
                        if info.get("udn"):
                            udn = info["udn"]
                        if info.get("name"):
                            name = info["name"]
                    except Exception:
                        pass

                    await self.async_set_unique_id(udn)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=name,
                        data={
                            "entry_type": "lan_tv",
                            "tv_ip": host,
                            "host": host,
                            "port": 38400,
                            "udn": udn,
                            "device_type": "电视",
                        },
                    )

        # 扫描局域网电视
        discovery = SsdpDiscovery(timeout=3.0)
        found = await discovery.discover()
        self._discovered_tvs = {
            tv.host: f"{tv.name or '海信电视'} ({tv.host})"
            for tv in found
        }

        if self._discovered_tvs:
            schema = vol.Schema({
                vol.Optional("discovered_tv", default=list(self._discovered_tvs.keys())[0]): vol.In(self._discovered_tvs),
                vol.Optional("tv_ip"): str,
            })
        else:
            schema = vol.Schema({
                vol.Required("tv_ip"): str,
            })

        return self.async_show_form(
            step_id="lan_tv",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_login(self, user_input=None):
        """Step 2: Login via Cloud."""
        errors = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            hisense_login = HiSenseLogin(session=session)

            try:
                token_pair = await hisense_login.login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except Exception:
                errors["base"] = "invalid_auth"
            else:
                if not token_pair:
                    errors["base"] = "invalid_auth"
                else:
                    access_token, refresh_token, customer_id = token_pair
                    await self.async_set_unique_id(str(customer_id))
                    self._abort_if_unique_id_configured()
                    try:
                        self._home_options = await hisense_login.get_home_select_options(
                            access_token, customer_id
                        )
                    except Exception:
                        errors["base"] = "cannot_connect"
                    else:
                        if self._home_options is None:
                            errors["base"] = "cannot_connect"
                        elif not self._home_options:
                            errors["base"] = "no_homes"
                        else:
                            self._access_token = access_token
                            self._refresh_token = refresh_token
                            self._customer_id = customer_id
                            self._username = user_input[CONF_USERNAME]
                            self._password = user_input[CONF_PASSWORD]
                            return await self.async_step_home()

        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={
                "username_hint": "使用海信爱家 App 账号登录",
                "password_hint": "对应密码",
            },
            errors=errors,
        )

    async def async_step_home(self, user_input=None):
        errors = {}

        if user_input is not None:
            home_id = user_input["home_id"]
            session = async_get_clientsession(self.hass)
            hisense_login = HiSenseLogin(session=session)
            try:
                self._device_info = await hisense_login.get_all_devices(
                    self._access_token,
                    home_id,
                    self._refresh_token,
                    self._customer_id,
                )
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if self._device_info is None:
                    errors["base"] = "cannot_connect"
                elif not self._device_info:
                    errors["base"] = "no_devices"
                else:
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="home",
            data_schema=vol.Schema(
                {vol.Required("home_id"): vol.In(self._home_options)}
            ),
            errors=errors,
        )

    async def async_step_device(self, user_input=None):
        errors = {}

        if user_input is not None:
            device_ids = user_input["device_ids"]
            if not device_ids:
                errors["base"] = "no_devices"
            else:
                devices = [
                    self._device_info[device_id]
                    for device_id in device_ids
                ]
                return self.async_create_entry(
                    title="Hisense Smart Control",
                    data={
                        "devices": devices,
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                    },
                )

        device_id_to_label = {}
        for device_id, info in self._device_info.items():
            label = info.get("label", device_id)
            device_type = info.get("device_type", "")
            device_id_to_label[device_id] = f"{label} ({device_type})"

        data_schema = vol.Schema(
            {
                vol.Required("device_ids"): cv.multi_select(device_id_to_label),
            }
        )
        return self.async_show_form(
            step_id="device",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        """Collect credentials for an existing token-only entry."""
        self._reauth_username = entry_data.get(CONF_USERNAME, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Verify the account still contains the configured devices."""
        errors = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            login = HiSenseLogin(session)
            try:
                tokens = await login.login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if not tokens:
                    errors["base"] = "invalid_auth"
                else:
                    access_token, refresh_token, customer_id = tokens
                    entry = self._get_reauth_entry()
                    expected = {device["device_id"] for device in entry.data["devices"]}
                    home_ids = {
                        device.get("home_id")
                        for device in entry.data["devices"]
                        if device.get("home_id")
                    }
                    try:
                        visible = set()
                        for home_id in home_ids:
                            devices = await login.get_all_devices(
                                access_token, home_id, refresh_token, customer_id
                            )
                            if devices is None:
                                raise ValueError("AIHome device list failed")
                            visible.update(devices)
                    except Exception:
                        errors["base"] = "cannot_connect"
                    else:
                        if not home_ids or not expected.issubset(visible):
                            errors["base"] = "wrong_account"
                        else:
                            updated_devices = [
                                {
                                    **device,
                                    "access_token": access_token,
                                    "refresh_token": refresh_token,
                                    "customer_id": customer_id,
                                }
                                for device in entry.data["devices"]
                            ]
                            return self.async_update_reload_and_abort(
                                entry,
                                data_updates={
                                    "devices": updated_devices,
                                    CONF_USERNAME: user_input[CONF_USERNAME],
                                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                                },
                            )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=self._reauth_username): str,
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )


class HisenseOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Hisense integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    @property
    def _entry(self) -> config_entries.ConfigEntry | None:
        if getattr(self, "_config_entry", None) is not None:
            return self._config_entry
        try:
            return self.config_entry
        except Exception:
            return None

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}
        entry = self._entry
        is_lan_tv = (entry.data.get("entry_type") == "lan_tv" or "tv_ip" in entry.data) if entry else False

        if user_input is not None:
            if not is_lan_tv:
                interval = user_input.get(CONF_WASHER_POLL_INTERVAL, DEFAULT_WASHER_POLL_INTERVAL)
                if interval < MIN_WASHER_POLL_INTERVAL:
                    errors[CONF_WASHER_POLL_INTERVAL] = "interval_too_low"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        options = entry.options if entry else {}

        if is_lan_tv:
            schema = vol.Schema({
                vol.Required(
                    CONF_TV_CONTROL_MODE,
                    default=options.get(CONF_TV_CONTROL_MODE, DEFAULT_TV_CONTROL_MODE),
                ): vol.In({CONTROL_MODE_LOCAL: "本地优先 (Local First)", CONTROL_MODE_CLOUD: "仅云端 (Cloud Only)"}),
            })
        else:
            has_tv = any(d.get("device_type") == "电视" for d in entry.data.get("devices", [])) if entry else False
            fields = {
                vol.Required(
                    CONF_ENABLE_WASHER_POLL,
                    default=options.get(CONF_ENABLE_WASHER_POLL, DEFAULT_ENABLE_WASHER_POLL),
                ): bool,
                vol.Required(
                    CONF_WASHER_POLL_INTERVAL,
                    default=options.get(CONF_WASHER_POLL_INTERVAL, DEFAULT_WASHER_POLL_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_WASHER_POLL_INTERVAL)),
                vol.Required(
                    CONF_ENABLE_PERIODIC_POLL,
                    default=options.get(CONF_ENABLE_PERIODIC_POLL, DEFAULT_ENABLE_PERIODIC_POLL),
                ): bool,
            }
            if has_tv:
                fields[vol.Required(
                    CONF_TV_CONTROL_MODE,
                    default=options.get(CONF_TV_CONTROL_MODE, DEFAULT_TV_CONTROL_MODE),
                )] = vol.In({CONTROL_MODE_LOCAL: "本地优先 (Local First)", CONTROL_MODE_CLOUD: "仅云端 (Cloud Only)"})
            schema = vol.Schema(fields)

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

