"""Basic SSRF guard for admin-entered upstream URLs, checked when a server is saved.

Blocked always: non-http(s) schemes, the cloud metadata range 169.254.0.0/16
(and its IPv6 equivalent), and the well-known metadata hostnames.
Blocked unless allow_loopback: 127.0.0.0/8 and ::1 (for local dev and tests).
Allowed on purpose: private ranges (10.x, 172.16.x, *.svc), because in-cluster
upstreams live there.

Known limit: a hostname is resolved once, now. DNS can later be changed to point
somewhere forbidden (rebinding). Re-checking at connect time is a later hardening.
"""

import ipaddress
import socket
from urllib.parse import urlparse

_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
_LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")


class UrlPolicyError(ValueError):
    pass


def _check_ip(ip: ipaddress._BaseAddress, allow_loopback: bool, host: str) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in _LINK_LOCAL or ip in _LINK_LOCAL_V6:
        raise UrlPolicyError(f"{host} is a link-local / cloud metadata address")
    if ip.is_loopback and not allow_loopback:
        raise UrlPolicyError(f"{host} is a loopback address (not allowed outside development)")
    if ip.is_unspecified or ip.is_multicast:
        raise UrlPolicyError(f"{host} is not a valid upstream address")


def validate_upstream_url(url: str, allow_loopback: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlPolicyError("only http and https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise UrlPolicyError("URL has no host")
    if host.lower() in _METADATA_HOSTS:
        raise UrlPolicyError(f"{host} is a cloud metadata hostname")
    try:
        _check_ip(ipaddress.ip_address(host), allow_loopback, host)
        return
    except ValueError as exc:
        if isinstance(exc, UrlPolicyError):
            raise
    # Hostname: check every address it resolves to right now.
    if host.lower() == "localhost" and not allow_loopback:
        raise UrlPolicyError("localhost is not allowed outside development")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return  # e.g. an in-cluster name that only resolves inside the cluster
    for info in infos:
        _check_ip(ipaddress.ip_address(info[4][0]), allow_loopback, host)
