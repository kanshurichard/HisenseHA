"""DLNA/UPnP protocol layer for the Hisense TV (HiSmart) integration.

Reverse-engineered from the Hisense Hia app (com.hisense.ms.fly2tv v7.0.8.12).

Discovery:
  The TV advertises itself via UPnP SSDP:
    NOTIFY * -> 239.255.255.250:1900
      NT: urn:schemas-upnp-org:device:MediaRenderer:1
      USN: uuid:<UDN>
      Location: http://<tv_ip>:<port>/MediaServer/rendererdevicedesc.xml
    Period: 1800s (30 min)

  We can also trigger discovery by sending M-SEARCH on the same group.
  Additionally we can do a UDP subnet sweep as a fallback.

Control:
  Standard DLNA MediaRenderer SOAP services on the same base_url:
    AVTransport       -> /upnp/control/mingusavtr
    RenderingControl  -> /upnp/control/mingusrcr
    ConnectionManager -> /upnp/control/minguscmr

  Verified actions:
    AVTransport:   GetTransportInfo, SetAVTransportURI, Play, Pause, Stop
    RenderingControl: SetMute, GetMute, ListPresets, SetPresetName
    ConnectionManager: GetProtocolInfo

  Volume adjust (SetTargetVolume) returns UPnP 401 Invalid Action on
  HiSmart TVs — only mute + presets are usable for audio control.

Key remote:
  A separate local HTTP service (not DLNA) accepts key presses:
    POST http://<tv_ip>:<port>/<KEY>..controller   (body ignored)
  Port 7778/7779, Server: WebServer/1.1, no auth. Keys use Android-style
  names (KEY_POWER, KEY_UP, KEY_OK, KEY_MENU, KEY_RETURNS, ...).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST_TYPES = [
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
]
SSDP_NOTIFY_NT_TYPES = [
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "upnp:rootdevice",
]

# Default DLNA base URL pattern (Hisense HiSmart TVs use :38400)
DEFAULT_DLNA_PORT = 38400

# Local key-remote HTTP service. The Hia app and the official web remote
# (`weixintv/web/remote-ctrl.html`) send
#     POST http://<tv_ip>:<port>/<KEY>..controller   body: {}
# with no authentication. The TV answers `Server: WebServer/1.1`.
# Ports observed listening on a real HiSmart TV: 7778 and 7779.
RC_PORT_CANDIDATES = (7778, 7779)
DEFAULT_RC_PORT = 7778

# Control URL paths (confirmed from device description XML)
CTRL_CM = "/upnp/control/minguscmr"
CTRL_AVT = "/upnp/control/mingusavtr"
CTRL_RCR = "/upnp/control/mingusrcr"

AVT_NS = "urn:schemas-upnp-org:service:AVTransport:1"
AVT_PFX = "AVTransport"
RCR_NS = "urn:schemas-upnp-org:service:RenderingControl:1"
RCR_PFX = "RenderingControl"
CM_NS = "urn:schemas-upnp-org:service:ConnectionManager:1"
CM_PFX = "ConnectionManager"


def _soap_envelope(prefix: str, ns: str, action: str, body: str) -> str:
    """Build a UPnP SOAP envelope with a short, namespace-declared prefix.

    The TV's DLNA stack rejects action elements whose tag uses the full
    namespace URI as the XML name (colons are illegal in XML names), so we
    must use a short prefix declared via an inline xmlns.
    """
    return (
        '<?xml version="1.0"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><{prefix}:{action} xmlns:{prefix}="{ns}">'
        f"{body}</{prefix}:{action}></s:Body></s:Envelope>"
    )


# ─── Data classes ────────────────────────────────────────────────────────────
@dataclass
class DlnaTv:
    """A discovered Hisense TV device."""
    udn: str
    name: str
    model: str
    base_url: str          # e.g. "http://192.168.31.128:38400"
    location: str          # full device desc URL
    host: str
    port: int
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def device_id(self) -> str:
        return self.udn.strip("urn:uuid:").split("::")[0]


# ─── SSDP Discovery ──────────────────────────────────────────────────────────
class SsdpDiscovery:
    """Discovers DLNA/UPnP devices on the LAN via SSDP.

    The Hisense HiSmart TV re-announces itself with `NOTIFY ssdp:alive`
    (every 30 min, Cache-Control: max-age=1800) and may also answer
    M-SEARCH with a 200 response. We capture both.
    """

    def __init__(self, iface: str | None = None, timeout: float = 5.0):
        self.iface = iface or self._auto_iface()
        self.timeout = timeout

    @staticmethod
    def _auto_iface() -> str:
        """Pick the LAN IPv4 interface that owns the default route."""
        try:
            import socket as _sock

            s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "0.0.0.0"

    async def discover(self) -> list[DlnaTv]:
        """Run SSDP discovery and return a list of DlnaTv devices."""
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, self._sync_discover)
        _LOGGER.info(
            "ssdp discovery found %d device(s) on iface %s",
            len(found),
            self.iface,
        )
        return found

    def _sync_discover(self) -> list[DlnaTv]:
        results: dict[str, DlnaTv] = {}
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass

        try:
            s.bind(("", 1900))
        except OSError:
            # Port 1900 may be in use by HA core SSDP; bind to ephemeral port for M-SEARCH
            s.bind(("", 0))

        # Bind multicast to the chosen interface if bound to 1900
        try:
            mreq = socket.inet_aton("239.255.255.250")
            mreq += socket.inet_aton(self.iface or "0.0.0.0")
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass

        # Send M-SEARCH for all ST types
        for st in SSDP_ST_TYPES:
            query = (
                f"M-SEARCH * HTTP/1.1\r\n"
                f"HOST: 239.255.255.250:1900\r\n"
                f"MAN: ssdp:discover\r\n"
                f"MX: 2\r\n"
                f"ST: {st}\r\n\r\n"
            ).encode()
            try:
                s.sendto(query, SSDP_ADDR)
            except OSError:
                pass

        # Collect responses (NOTIFY alive + M-SEARCH 200 replies)
        s.settimeout(self.timeout)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._parse_packet(data, addr, results)
        s.close()
        return list(results.values())

    def _parse_packet(self, data: bytes, addr: tuple[str, int], results: dict[str, DlnaTv]):
        text = data.decode("utf-8", "replace")
        low = text.lower()
        # Skip our own M-SEARCH echoes and unrelated packets
        if "m-search" in low and "200" not in low:
            return

        # Extract headers (works for both 200 responses and NOTIFY)
        headers: dict[str, str] = {}
        for line in text.splitlines()[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        loc = headers.get("location", "")
        usn = headers.get("usn", "")
        nt = headers.get("nt", "")
        if not loc:
            return

        # Fast pre-filter: ignore gateway / router UPnP services
        nt_usn = f"{nt} {usn}".lower()
        if any(bad in nt_usn for bad in ("internetgateway", "wandevice", "wanconnection", "lanhost")):
            return

        key = usn or loc
        if key in results:
            return

        # The real control host/port come from the Location URL, not the
        # ephemeral UDP source port of the NOTIFY
        loc_parsed = urlparse(loc)
        tv = DlnaTv(
            udn=usn,
            name="",
            model="",
            base_url="",
            location=loc,
            host=loc_parsed.hostname or addr[0],
            port=loc_parsed.port or addr[1],
        )

        # Fetch device description and verify it is truly a Hisense TV
        try:
            devinfo = self._fetch_desc(loc)
            if not devinfo:
                return

            name = devinfo.get("name", "")
            model = devinfo.get("model", "")
            mfr = devinfo.get("manufacturer", "")
            dtype = devinfo.get("device_type", "")

            # Exclude non-TV devices (e.g. OpenWrt router, gateway, NAS)
            low_type = dtype.lower()
            if any(k in low_type for k in ("internetgatewaydevice", "wandevice", "wanconnectiondevice", "lanhostconfig")):
                return
            low_mfr = mfr.lower()
            if any(k in low_mfr for k in ("openwrt", "asus", "netgear", "tp-link", "huawei", "cisco", "miniupnp")):
                return

            # Match Hisense TV signature
            full_sig = f"{name} {model} {mfr} {loc}".lower()
            is_hisense = "hisense" in full_sig or "hismart" in full_sig or "vidaa" in full_sig
            is_media = "mediarenderer" in low_type or "tv" in full_sig or "电视" in full_sig or "rendererdevicedesc" in loc.lower()

            if not (is_hisense and is_media):
                return

            tv.name = name or "Hisense TV"
            tv.model = model or "HiSmart"
            tv.base_url = devinfo.get("base_url") or tv.base_url
            tv.udn = devinfo.get("udn") or tv.udn

        except Exception as e:
            _LOGGER.debug("Error inspecting UPnP device at %s: %s", loc, e)
            return

        # Deduplicate: several USN NOTIFYs announce the same device
        dev_udn = (tv.udn or usn).split("::")[0]
        if dev_udn and any(
            (e.udn or "").split("::")[0] == dev_udn for e in results.values()
        ):
            return
        results[dev_udn or key] = tv

    def _fetch_desc(self, url: str, timeout: float = 2.0) -> dict[str, str] | None:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "HomeAssistant/Hisense"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    parsed = urlparse(url)
                    return self._parse_device_xml(body, parsed.hostname or "", parsed.port or 80)
        except Exception as e:
            _LOGGER.debug("Failed to fetch UPnP desc from %s: %s", url, e)
        return None

    def _parse_device_xml(self, xml_text: str, host: str, port: int) -> dict[str, str] | None:
        try:
            root = ET.fromstring(xml_text.encode())
        except ET.ParseError:
            return None

        # Search elements recursively ignoring namespace and case
        tags: dict[str, str] = {}
        for el in root.iter():
            tag_name = el.tag.split("}")[-1].lower()
            if tag_name not in tags and el.text and el.text.strip():
                tags[tag_name] = el.text.strip()

        name = tags.get("friendlyname", "")
        model = tags.get("modelname", "")
        manufacturer = tags.get("manufacturer", "")
        device_type = tags.get("devicetype", "")
        udn = tags.get("udn", "")
        base_url = tags.get("urlbase") or f"http://{host}:{port}"

        return {
            "name": name,
            "model": model,
            "manufacturer": manufacturer,
            "device_type": device_type,
            "udn": udn,
            "base_url": base_url,
        }


# ─── DLNA Control Client ─────────────────────────────────────────────────────
class DlnaClient:
    """Async DLNA/UPnP control client for the Hisense TV."""

    def __init__(self, host: str, port: int = DEFAULT_DLNA_PORT, session: ClientSession | None = None):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.session: ClientSession | None = session
        self._own_session: bool = session is None
        self._ctrl_urls: dict[str, str] = {}
        self._info: dict[str, str] = {}
        self._rc_port = DEFAULT_RC_PORT

    async def test_connect(self, timeout: float = 2.0) -> bool:
        """Quickly check if the TV is reachable via DLNA or key-remote port."""
        loop = asyncio.get_running_loop()
        for port in (self._rc_port, DEFAULT_DLNA_PORT, 7779, 7778):
            def _check():
                try:
                    with socket.create_connection((self.host, port), timeout=timeout):
                        return True
                except (OSError, socket.timeout):
                    return False
            if await loop.run_in_executor(None, _check):
                return True
        return False

    async def connect(self) -> dict[str, str]:
        """Fetch device description, map control URLs, return device info."""
        if self.session is None or self.session.closed:
            await self._open_session()
        return self._info

    async def _open_session(self) -> None:
        """Open the HTTP session and load the DLNA device description."""
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=5))
            self._own_session = True
        try:
            # Fetch device description (absolute URL, no base_url join ambiguity)
            resp = await self.session.get(
                self.base_url + "/MediaServer/rendererdevicedesc.xml"
            )
            if resp.status != 200:
                resp = await self.session.get(self.base_url + "/description.xml")
            if resp.status != 200:
                raise ConnectionError(
                    f"Cannot fetch device description from {self.base_url} "
                    f"(HTTP {resp.status})"
                )
            text = await resp.text()
        except Exception:
            if self._own_session and self.session:
                await self.session.close()
                self.session = None
            raise

        self._info = self._parse_desc(text)
        self._ctrl_urls = self._extract_ctrl_urls(text, self.base_url)
        _LOGGER.info(
            "dlna connected: %s (%s) ctrl=%s",
            self._info.get("name"), self.host, list(self._ctrl_urls.keys()),
        )

    async def close(self) -> None:
        if self.session and self._own_session and not self.session.closed:
            await self.session.close()
            self.session = None

    def _extract_ctrl_urls(self, xml_text: str, base: str) -> dict[str, str]:
        """Extract control URLs from serviceList in device XML."""
        ctrl: dict[str, str] = {}
        for m in re.finditer(
            r"<service>\s*<serviceType>([^<]+)</serviceType>.*?"
            r"<controlURL>([^<]+)</controlURL>.*?</service>",
            xml_text,
            re.S,
        ):
            st = m.group(1).strip()
            url = m.group(2).strip()
            if not url.startswith("http"):
                url = base + url
            # Map by service type
            if "AVTransport" in st:
                ctrl["avt"] = url
            elif "RenderingControl" in st:
                ctrl["rcr"] = url
            elif "ConnectionManager" in st:
                ctrl["cm"] = url
        return ctrl

    @staticmethod
    def _parse_desc(text: str) -> dict[str, str]:
        info: dict[str, str] = {}
        for tag, key in [
            ("friendlyName", "name"),
            ("modelName", "model"),
            ("UDN", "udn"),
            ("URLBase", "base_url"),
        ]:
            m = re.search(f"<{tag}>(.*?)</{tag}>", text, re.S)
            if m:
                info[key] = m.group(1).strip()
        return info

    # ── SOAP helpers ──────────────────────────────────────────────────────
    async def _soap_call(
        self,
        ctrl_key: str,
        prefix: str,
        ns: str,
        action: str,
        body: str,
    ) -> ET.Element:
        """Send a SOAP call and return the response element."""
        if self.session is None:
            # TV was off/standby when the entry was set up — reconnect lazily.
            await self._open_session()
        ctrl_url = self._ctrl_urls.get(ctrl_key)
        if not ctrl_url:
            # Fallback: construct from base_url
            fallback = {
                "avt": self.base_url + CTRL_AVT,
                "rcr": self.base_url + CTRL_RCR,
                "cm": self.base_url + CTRL_CM,
            }
            ctrl_url = fallback.get(ctrl_key, "")
        if not ctrl_url:
            raise RuntimeError(f"No control URL for {ctrl_key}")

        envelope = _soap_envelope(prefix, ns, action, body)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{ns}#{action}"',
        }
        resp = await self.session.post(
            ctrl_url,
            data=envelope.encode("utf-8"),
            headers=headers,
        )
        text = await resp.text()
        if resp.status != 200:
            # Check for UPnP fault
            m = re.search(
                r"<errorCode>(\d+)</errorCode>\s*<errorDescription>(.*?)</errorDescription>",
                text,
            )
            if m:
                raise RuntimeError(
                    f"UPnP error {m.group(1)}: {m.group(2)}"
                )
            raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
        # Parse response body
        body_xml = text.partition("<s:Body>")[2].partition("</s:Body>")[0]
        if not body_xml:
            body_xml = text.partition("\r\n\r\n")[2]
        root = ET.fromstring(body_xml.encode("utf-8"))
        return root

    # ── AVTransport ───────────────────────────────────────────────────────
    async def get_transport_info(self) -> dict[str, str]:
        root = await self._soap_call("avt", AVT_PFX, AVT_NS, "GetTransportInfo",
                                     "<InstanceID>0</InstanceID>")
        result: dict[str, str] = {}
        for child in root:
            tag = child.tag.split("}")[-1]
            result[tag] = child.text or ""
        return result

    async def play(self, media_url: str) -> None:
        await self._soap_call("avt", AVT_PFX, AVT_NS, "SetAVTransportURI",
                               f"<InstanceID>0</InstanceID><CurrentURI>{media_url}</CurrentURI>")
        await self._soap_call("avt", AVT_PFX, AVT_NS, "Play",
                               "<InstanceID>0</InstanceID><Speed>1</Speed>")

    async def pause(self) -> None:
        await self._soap_call("avt", AVT_PFX, AVT_NS, "Pause",
                               "<InstanceID>0</InstanceID>")

    async def stop(self) -> None:
        await self._soap_call("avt", AVT_PFX, AVT_NS, "Stop",
                               "<InstanceID>0</InstanceID>")

    # ── Key remote (LAN HTTP) ─────────────────────────────────────────────
    async def send_key(self, key: str) -> None:
        """Send a remote key such as KEY_MENU to the TV.

        The TV exposes an unauthenticated HTTP service that ignores the
        request body and always answers 200, so only transport-level errors
        are detectable here.
        """
        if not key.startswith("KEY_"):
            raise ValueError(f"not a remote key: {key}")
        if self.session is None or self.session.closed:
            # The key service lives on another port and can be reachable
            # while DLNA is not, so do not require the device description.
            self.session = ClientSession(timeout=ClientTimeout(total=5))
            self._own_session = True

        ports = [self._rc_port] + [
            p for p in RC_PORT_CANDIDATES if p != self._rc_port
        ]
        last_err: Exception | None = None
        for port in ports:
            url = f"http://{self.host}:{port}/{key}..controller"
            try:
                resp = await self.session.post(
                    url, json={}, timeout=ClientTimeout(total=3)
                )
                if resp.status != 200:
                    last_err = RuntimeError(f"HTTP {resp.status} from {url}")
                    continue
                self._rc_port = port
                return
            except (ClientError, asyncio.TimeoutError) as err:
                last_err = err
        raise RuntimeError(f"sending {key} failed: {last_err}")

    # ── RenderingControl ─────────────────────────────────────────────────
    async def set_mute(self, mute: bool) -> None:
        val = "1" if mute else "0"
        await self._soap_call("rcr", RCR_PFX, RCR_NS, "SetMute",
                               f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>{val}</DesiredMute>")

    async def get_mute(self) -> bool:
        root = await self._soap_call("rcr", RCR_PFX, RCR_NS, "GetMute",
                                     "<InstanceID>0</InstanceID><Channel>Master</Channel>")
        for child in root:
            if "Mute" in child.tag:
                return (child.text or "0") != "0"
        return False

    async def list_presets(self) -> list[str]:
        root = await self._soap_call("rcr", RCR_PFX, RCR_NS, "ListPresets",
                                     "<InstanceID>0</InstanceID>")
        presets_str = ""
        for child in root:
            if "PresetNameList" in child.tag:
                presets_str = child.text or ""
                break
        return [p.strip() for p in presets_str.split(",") if p.strip()]

    async def set_preset(self, name: str) -> None:
        await self._soap_call("rcr", RCR_PFX, RCR_NS, "SetPresetName",
                               f"<InstanceID>0</InstanceID><PresetName>{name}</PresetName>")

    # ── ConnectionManager ───────────────────────────────────────────────
    async def get_protocol_info(self) -> dict[str, str]:
        root = await self._soap_call("cm", CM_PFX, CM_NS, "GetProtocolInfo",
                                      "<SourceProtocolInfo></SourceProtocolInfo>"
                                      "<SinkProtocolInfo></SinkProtocolInfo>")
        result: dict[str, str] = {}
        for child in root:
            tag = child.tag.split("}")[-1]
            result[tag] = child.text or ""
        return result
