from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeMediaURL(ValueError):
    """Raised when a user supplied URL is not safe to fetch."""


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _public_ip(str(mapped))
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def normalize_host(host: str) -> str:
    value = host.strip().lower()
    if not value or value.endswith(".") or "%" in value:
        raise UnsafeMediaURL("Enter a public HTTPS video page or media link.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise UnsafeMediaURL("The video link hostname is invalid.") from exc
        if len(value) > 253 or value.endswith((".localhost", ".local", ".internal")):
            raise UnsafeMediaURL("The video link must use a public website.")
        labels = value.split(".")
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        ):
            raise UnsafeMediaURL("The video link hostname is invalid.")
        return value
    if not _public_ip(str(address)):
        raise UnsafeMediaURL("The video link must point to a public website.")
    return str(address)


def resolve_public_host(host: str, port: int) -> list[tuple[int, tuple]]:
    """Resolve a hostname once, reject any non-public answer, and return pinned socket addresses."""
    normalized = normalize_host(host)
    if port not in {80, 443}:
        raise UnsafeMediaURL("The video link uses an unsupported network port.")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            records = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeMediaURL("The video link hostname could not be resolved.") from exc
        if not records:
            raise UnsafeMediaURL("The video link hostname could not be resolved.")
        addresses: list[tuple[int, tuple]] = []
        for family, socktype, proto, _canonname, sockaddr in records:
            if not _public_ip(str(sockaddr[0])):
                raise UnsafeMediaURL("The video link resolves to a restricted network address.")
            item = (family, sockaddr)
            if item not in addresses:
                addresses.append(item)
        return addresses
    if not _public_ip(str(literal)):
        raise UnsafeMediaURL("The video link must point to a public website.")
    family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
    sockaddr = (normalized, port, 0, 0) if family == socket.AF_INET6 else (normalized, port)
    return [(family, sockaddr)]


def validate_media_url(value: str, *, resolve_dns: bool = True) -> str:
    """Validate a public HTTP(S) page/media URL; the egress proxy repeats DNS checks and pins connections."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise UnsafeMediaURL("Enter a video page or direct media link up to 2048 characters.")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise UnsafeMediaURL("The video link is malformed.") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        raise UnsafeMediaURL("Use a public HTTP or HTTPS video link.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeMediaURL("Video links with embedded usernames or passwords are not accepted.")
    expected_port = 443 if scheme == "https" else 80
    if port is not None and port != expected_port:
        raise UnsafeMediaURL("The video link uses an unsupported network port.")
    host = normalize_host(host)
    if resolve_dns:
        resolve_public_host(host, expected_port)
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    clean = SplitResult(scheme, netloc, parsed.path or "/", parsed.query, "")
    return urlunsplit(clean)
