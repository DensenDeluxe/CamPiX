"""Minimal ONVIF Profile S device service and a bounded PTZ client.

The device service is off until an administrator sets a username and password.
PasswordText and HTTP Basic are checked against an Argon2 hash. PasswordDigest
is rejected because the plaintext is not retained. The PTZ client talks only to
the host already stored on a network camera, with a one-second move budget.
"""

from __future__ import annotations

import re
import secrets
import socket
import xml.etree.ElementTree as ET
from datetime import UTC
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from pydantic import Field

from .models import StrictModel
from .notify import address_allowed
from .security import HASHER

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
PASSWORD_TEXT = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"
)
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_LOCAL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,40}$")


class OnvifSettings(StrictModel):
    enabled: bool = False
    username: str = Field("", max_length=64)

    def username_valid(self) -> None:
        if self.enabled and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}", self.username):
            raise ValueError("ONVIF username is not allowed")


class OnvifError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def public_state(settings: OnvifSettings, password_set: bool) -> dict:
    return {
        "enabled": settings.enabled,
        "username": settings.username,
        "password_set": password_set,
    }


def load_state(db) -> tuple[OnvifSettings, str | None]:
    saved = db.setting("onvif") or {}
    password_hash = saved.get("password_hash") if isinstance(saved, dict) else None
    payload = {key: saved.get(key) for key in ("enabled", "username")} if isinstance(saved, dict) else {}
    settings = OnvifSettings.model_validate({key: value for key, value in payload.items() if value is not None})
    if not isinstance(password_hash, str) or not password_hash:
        password_hash = None
    return settings, password_hash


def save_state(db, settings: OnvifSettings, password: str | None, previous_hash: str | None) -> str | None:
    settings.username_valid()
    password_hash = previous_hash
    if password:
        if len(password) < 12 or len(password) > 128 or any(c in password for c in "\r\n\x00"):
            raise ValueError("ONVIF password is not allowed")
        password_hash = HASHER.hash(password)
    if settings.enabled and not password_hash:
        raise ValueError("ONVIF password is required")
    db.set_setting(
        "onvif",
        {"enabled": settings.enabled, "username": settings.username, "password_hash": password_hash or ""},
    )
    return password_hash


def authenticate(body: bytes, authorization: str | None, settings: OnvifSettings, password_hash: str) -> bool:
    found = _basic(authorization)
    if found is None and body:
        found = _password_text(body)
    if not found or not settings.username:
        return _reject(password_hash)
    username, password = found
    if not secrets.compare_digest(username, settings.username):
        return _reject(password_hash)
    try:
        return bool(HASHER.verify(password_hash, password))
    except Exception:
        return _reject(password_hash)


def _reject(password_hash: str) -> bool:
    try:
        HASHER.verify(password_hash, secrets.token_urlsafe(16))
    except Exception:
        pass
    return False


def _basic(header: str | None):
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    import base64

    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def _password_text(body: bytes):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise OnvifError(400, "ONVIF request was rejected")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise OnvifError(400, "ONVIF request was rejected") from exc
    username = password = None
    digest = False
    for element in root.iter():
        name = _local(element.tag)
        if name == "Username" and element.text:
            username = element.text.strip()
        elif name == "Password" and element.text:
            password = element.text
            if element.attrib.get("Type", PASSWORD_TEXT) != PASSWORD_TEXT:
                digest = True
    if digest:
        raise OnvifError(401, "ONVIF password digest is not accepted")
    if username is None or password is None:
        return None
    return username, password


def action_name(body: bytes) -> str:
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise OnvifError(400, "ONVIF request was rejected")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise OnvifError(400, "ONVIF request was rejected") from exc
    body_element = next((child for child in root.iter() if _local(child.tag) == "Body"), None)
    if body_element is None:
        raise OnvifError(400, "ONVIF request was rejected")
    for child in list(body_element):
        name = _local(child.tag)
        if _LOCAL_NAME.fullmatch(name):
            return name
    raise OnvifError(400, "ONVIF request was rejected")


def profile_token(body: bytes) -> str:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise OnvifError(400, "ONVIF request was rejected") from exc
    for element in root.iter():
        if _local(element.tag) != "ProfileToken" or not element.text:
            continue
        token = element.text.strip()
        if _TOKEN.fullmatch(token):
            return token
        raise OnvifError(400, "ONVIF profile was rejected")
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def envelope(inner: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" xmlns:tds="{DEVICE_NS}" xmlns:trt="{MEDIA_NS}"'
        f' xmlns:tt="{SCHEMA_NS}" xmlns:tptz="{PTZ_NS}">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    ).encode()


def fault(reason: str) -> bytes:
    text = escape(reason)
    return envelope(f"<s:Fault><s:Reason><s:Text>{text}</s:Text></s:Reason></s:Fault>")


def device_information(version: str, serial: str) -> bytes:
    return envelope(
        "<tds:GetDeviceInformationResponse>"
        "<tds:Manufacturer>CamPiX</tds:Manufacturer>"
        "<tds:Model>CamPiX</tds:Model>"
        f"<tds:FirmwareVersion>{escape(version)}</tds:FirmwareVersion>"
        f"<tds:SerialNumber>{escape(serial)}</tds:SerialNumber>"
        "<tds:HardwareId>software</tds:HardwareId>"
        "</tds:GetDeviceInformationResponse>"
    )


def system_date() -> bytes:
    from datetime import datetime

    now = datetime.now(UTC)
    return envelope(
        "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
        "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
        "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
        "<tt:UTCDateTime><tt:Time>"
        f"<tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second>"
        "</tt:Time><tt:Date>"
        f"<tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day>"
        "</tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
    )


def capabilities(xaddr: str) -> bytes:
    safe = escape(xaddr)
    return envelope(
        "<tds:GetCapabilitiesResponse><tds:Capabilities>"
        f'<tt:Device><tt:XAddr>{safe}/onvif/device_service</tt:XAddr></tt:Device>'
        f'<tt:Media><tt:XAddr>{safe}/onvif/device_service</tt:XAddr></tt:Media>'
        "</tds:Capabilities></tds:GetCapabilitiesResponse>"
    )


def services(xaddr: str) -> bytes:
    safe = escape(xaddr)
    return envelope(
        "<tds:GetServicesResponse>"
        f'<tds:Service><tds:Namespace>{DEVICE_NS}</tds:Namespace><tds:XAddr>{safe}/onvif/device_service</tds:XAddr>'
        "<tds:Version><tt:Major>1</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>"
        f'<tds:Service><tds:Namespace>{MEDIA_NS}</tds:Namespace><tds:XAddr>{safe}/onvif/device_service</tds:XAddr>'
        "<tds:Version><tt:Major>1</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>"
        "</tds:GetServicesResponse>"
    )


def profiles(cameras: list[dict]) -> bytes:
    blocks = []
    for camera in cameras:
        token = escape(camera["id"])
        name = escape(camera["name"])[:80]
        width, height = int(camera["width"]), int(camera["height"])
        fps = int(camera["fps"])
        blocks.append(
            f'<trt:Profiles token="{token}" fixed="true"><tt:Name>{name}</tt:Name>'
            f'<tt:VideoSourceConfiguration token="{token}-src"><tt:Name>{name}</tt:Name>'
            f"<tt:UseCount>1</tt:UseCount><tt:SourceToken>{token}</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{width}" height="{height}"/></tt:VideoSourceConfiguration>'
            f'<tt:VideoEncoderConfiguration token="{token}-enc"><tt:Name>H264</tt:Name>'
            "<tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:Resolution>"
            f"<tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>{fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit>"
            "</tt:RateControl></tt:VideoEncoderConfiguration></trt:Profiles>"
        )
    return envelope("<trt:GetProfilesResponse>" + "".join(blocks) + "</trt:GetProfilesResponse>")


def stream_uri(uri: str) -> bytes:
    return envelope(
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        f"<tt:Uri>{escape(uri)}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


def snapshot_uri(uri: str) -> bytes:
    return envelope(
        "<trt:GetSnapshotUriResponse><trt:MediaUri>"
        f"<tt:Uri>{escape(uri)}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout>"
        "</trt:MediaUri></trt:GetSnapshotUriResponse>"
    )


def video_sources(cameras: list[dict]) -> bytes:
    blocks = []
    for camera in cameras:
        token = escape(camera["id"])
        blocks.append(
            f"<trt:VideoSources token=\"{token}\"><tt:Resolution>"
            f"<tt:Width>{int(camera['width'])}</tt:Width><tt:Height>{int(camera['height'])}</tt:Height>"
            "</tt:Resolution></trt:VideoSources>"
        )
    return envelope("<trt:GetVideoSourcesResponse>" + "".join(blocks) + "</trt:GetVideoSourcesResponse>")


def media_rtsp(base: str, camera_id: str) -> str:
    parts = urlsplit(base)
    if parts.scheme != "rtsp" or not parts.hostname or not _TOKEN.fullmatch(camera_id):
        raise OnvifError(409, "ONVIF stream is not configured")
    port = f":{parts.port}" if parts.port else ""
    return f"rtsp://{parts.hostname}{port}/{camera_id}"


def ptz_request(profile: str, action: str, pan: float, tilt: float, zoom: float) -> bytes:
    if not _TOKEN.fullmatch(profile):
        raise OnvifError(400, "PTZ profile was rejected")
    if action == "stop":
        body = (
            f'<tptz:Stop xmlns:tptz="{PTZ_NS}"><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>'
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
        )
    elif action == "move":
        body = (
            f'<tptz:ContinuousMove xmlns:tptz="{PTZ_NS}">'
            f"<tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken><tptz:Velocity>"
            f'<tt:PanTilt xmlns:tt="{SCHEMA_NS}" x="{pan:.3f}" y="{tilt:.3f}"/>'
            f'<tt:Zoom xmlns:tt="{SCHEMA_NS}" x="{zoom:.3f}"/></tptz:Velocity></tptz:ContinuousMove>'
        )
    else:
        raise OnvifError(400, "PTZ action was rejected")
    return envelope(body)


def allowed_target(hostname: str) -> list[str]:
    if not hostname or hostname.endswith(".") or any(c in hostname for c in "\r\n\x00 /\\"):
        raise OnvifError(422, "PTZ camera address is not allowed")
    try:
        literal = ipaddress_literal(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not address_allowed(literal):
            raise OnvifError(422, "PTZ camera address is not allowed")
        return [hostname]
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OnvifError(422, "PTZ camera address is not allowed") from exc
    addresses = []
    for info in infos:
        address = ipaddress_literal(info[4][0])
        if address is not None and address_allowed(address):
            addresses.append(info[4][0])
    if not addresses:
        raise OnvifError(422, "PTZ camera address is not allowed")
    return addresses[:4]


def ipaddress_literal(value: str):
    import ipaddress

    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def first_profile_token(payload: bytes) -> str:
    match = re.search(rb'token="([A-Za-z0-9][A-Za-z0-9_.:-]{0,63})"', payload)
    if not match:
        raise OnvifError(502, "PTZ camera did not provide a profile")
    return match.group(1).decode()


def soap_call(connection, path: str, payload: bytes, username: str, password: str) -> bytes:
    import base64

    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    connection.request(
        "POST",
        path,
        payload,
        {"Content-Type": "application/soap+xml; charset=utf-8", "Authorization": f"Basic {token}"},
    )
    response = connection.getresponse()
    body = response.read(65536)
    if response.status >= 400 or not body:
        raise OnvifError(502, "PTZ camera did not accept the command")
    return body


def move(
    connection_factory,
    host: str,
    port: int,
    https: bool,
    username: str,
    password: str,
    action: str,
    pan: float,
    tilt: float,
    zoom: float,
) -> None:
    """Send Stop, or ContinuousMove using the camera's first profile token."""
    addresses = allowed_target(host)
    last_error = None
    for address in addresses:
        try:
            connection = connection_factory(address, port, 3, https)
            try:
                listed = soap_call(
                    connection,
                    "/onvif/device_service",
                    envelope("<trt:GetProfiles/>"),
                    username,
                    password,
                )
                profile = first_profile_token(listed)
                soap_call(
                    connection,
                    "/onvif/device_service",
                    ptz_request(profile, action, pan, tilt, zoom),
                    username,
                    password,
                )
            finally:
                close = getattr(connection, "close", None)
                if close:
                    close()
            return
        except OnvifError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue
    if isinstance(last_error, OnvifError):
        raise last_error
    raise OnvifError(502, "PTZ camera did not accept the command")

