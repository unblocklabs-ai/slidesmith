"""Constrained network access for authored image metadata."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import SplitResult, urljoin, urlsplit

import certifi
from PIL import Image

_FETCH_TIMEOUT_SECONDS = 10.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_READ_CHUNK_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000


@dataclass(frozen=True)
class _ResolvedUrl:
    parsed: SplitResult
    port: int
    addresses: tuple[str, ...]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose peer is an already-validated address."""

    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port, timeout=_FETCH_TIMEOUT_SECONDS)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP while verifying the URL hostname."""

    def __init__(self, host: str, port: int, address: str) -> None:
        context = ssl.create_default_context(cafile=certifi.where())
        super().__init__(host, port, timeout=_FETCH_TIMEOUT_SECONDS, context=context)
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def validate_public_image_url(url: str, *, resolve_host: bool) -> _ResolvedUrl:
    """Validate an HTTP(S) image URL and optionally resolve all peer addresses."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("expected an http(s) URL") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not host:
        raise ValueError("expected an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("image URLs must not contain credentials")

    port = port or (443 if scheme == "https" else 80)
    addresses: list[str] = []
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None

    if literal is not None:
        _require_public_address(literal, url)
        addresses.append(str(literal))
    elif resolve_host:
        try:
            info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"could not resolve image host {host!r}: {exc}") from exc
        for entry in info:
            address = entry[4][0].split("%", 1)[0]
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ValueError(f"could not resolve image host {host!r}")
        for address in addresses:
            _require_public_address(ipaddress.ip_address(address), url)

    return _ResolvedUrl(parsed=parsed, port=port, addresses=tuple(addresses))


def fetch_image_dimensions(url: str) -> tuple[int, int]:
    """Fetch an image through validated, IP-pinned redirect hops."""
    current_url = url
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            resolved = validate_public_image_url(current_url, resolve_host=True)
            connection, response = _open_response(resolved)
            try:
                if response.status in _REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError(
                            f"redirect from {current_url!r} omitted Location"
                        )
                    if redirect_count == _MAX_REDIRECTS:
                        raise ValueError("too many image redirects")
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise ValueError(
                        f"image request returned HTTP {response.status}"
                    )
                payload = _read_bounded(response)
            finally:
                response.close()
                connection.close()

            expected_size = _preflight_dimensions(payload)
            _require_bounded_pixels(expected_size)
            with Image.open(BytesIO(payload)) as image:
                if image.size != expected_size:
                    raise ValueError("image dimensions changed during validation")
                return image.size
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
        raise ValueError(
            f"Could not fetch image dimensions from {url!r} for fit='contain': {exc}"
        ) from exc

    raise AssertionError("unreachable")


def _require_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, url: str) -> None:
    if not address.is_global:
        raise ValueError(
            f"image URL {url!r} targets non-public address {address}"
        )


def _read_bounded(response: http.client.HTTPResponse) -> bytes:
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise ValueError("image response has an invalid Content-Length") from exc
        if declared_size < 0:
            raise ValueError("image response has a negative Content-Length")
        if declared_size > MAX_IMAGE_BYTES:
            raise ValueError("image download exceeds the 25 MB limit")

    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ValueError("image download exceeds the 25 MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _preflight_dimensions(payload: bytes) -> tuple[int, int]:
    """Read dimensions from bounded headers before Pillow inspects the image."""
    dimensions: tuple[int, int] | None = None
    if payload.startswith(b"\x89PNG\r\n\x1a\n") and payload[12:16] == b"IHDR":
        if len(payload) >= 24:
            dimensions = (
                int.from_bytes(payload[16:20], "big"),
                int.from_bytes(payload[20:24], "big"),
            )
    elif payload[:6] in {b"GIF87a", b"GIF89a"} and len(payload) >= 10:
        dimensions = (
            int.from_bytes(payload[6:8], "little"),
            int.from_bytes(payload[8:10], "little"),
        )
    elif payload.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(payload)
    elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        dimensions = _webp_dimensions(payload)
    elif payload.startswith(b"BM") and len(payload) >= 26:
        dimensions = (
            abs(int.from_bytes(payload[18:22], "little", signed=True)),
            abs(int.from_bytes(payload[22:26], "little", signed=True)),
        )

    if dimensions is None:
        raise ValueError("unsupported or truncated image header")
    if dimensions[0] <= 0 or dimensions[1] <= 0:
        raise ValueError("image dimensions must be positive")
    return dimensions


def _jpeg_dimensions(payload: bytes) -> tuple[int, int] | None:
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    while offset + 3 < len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            break
        if marker in start_of_frame and segment_length >= 7:
            return (
                int.from_bytes(payload[offset + 5 : offset + 7], "big"),
                int.from_bytes(payload[offset + 3 : offset + 5], "big"),
            )
        offset += segment_length
    return None


def _webp_dimensions(payload: bytes) -> tuple[int, int] | None:
    chunk_type = payload[12:16]
    if chunk_type == b"VP8X" and len(payload) >= 30:
        return (
            1 + int.from_bytes(payload[24:27], "little"),
            1 + int.from_bytes(payload[27:30], "little"),
        )
    if chunk_type == b"VP8L" and len(payload) >= 25 and payload[20] == 0x2F:
        packed = int.from_bytes(payload[21:25], "little")
        return (1 + (packed & 0x3FFF), 1 + ((packed >> 14) & 0x3FFF))
    if (
        chunk_type == b"VP8 "
        and len(payload) >= 30
        and payload[23:26] == b"\x9d\x01\x2a"
    ):
        return (
            int.from_bytes(payload[26:28], "little") & 0x3FFF,
            int.from_bytes(payload[28:30], "little") & 0x3FFF,
        )
    return None


def _require_bounded_pixels(dimensions: tuple[int, int]) -> None:
    pixels = dimensions[0] * dimensions[1]
    if pixels > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"image dimensions exceed the {MAX_IMAGE_PIXELS:,} pixels limit"
        )


def _open_response(
    resolved: _ResolvedUrl,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    host = resolved.parsed.hostname
    if host is None:
        raise ValueError("expected an http(s) URL")
    target = resolved.parsed.path or "/"
    if resolved.parsed.query:
        target = f"{target}?{resolved.parsed.query}"
    host_header = _host_header(resolved.parsed, resolved.port)
    last_error: OSError | None = None
    for address in resolved.addresses:
        connection_type = (
            _PinnedHTTPSConnection
            if resolved.parsed.scheme.lower() == "https"
            else _PinnedHTTPConnection
        )
        connection = connection_type(host, resolved.port, address)
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "image/*",
                    "Host": host_header,
                    "User-Agent": "slidesmith-image-metadata/1",
                },
            )
            return connection, connection.getresponse()
        except OSError as exc:
            last_error = exc
            connection.close()
    if last_error is not None:
        raise last_error
    raise ValueError("image host resolved to no usable address")


def _host_header(parsed: SplitResult, port: int) -> str:
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return host if port == default_port else f"{host}:{port}"
