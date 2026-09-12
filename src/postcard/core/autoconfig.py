"""Finding a mail domain's server settings, the way Thunderbird does.

In order, stopping at the first answer: the built-in provider table; the
domain's own autoconfig file; Mozilla's ISPDB; RFC 6186 SRV records; and the
domain's MX host, matched against the hosting presets or looked up in the
ISPDB under the MX host's own domain (which is how a custom domain at a big
provider is found). Runs on a worker thread: DNS and HTTP, nothing else.
"""

import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from xml.etree import ElementTree

from gi.repository import Gio, GLib

from . import hosting, providers
from .hosting import ServerSettings
from .models.account import SECURITY_STARTTLS, SECURITY_TLS

logger = logging.getLogger(__name__)

ISPDB_URL = "https://autoconfig.thunderbird.net/v1.1/{domain}"
DOMAIN_URLS = (
    "https://autoconfig.{domain}/mail/config-v1.1.xml?emailaddress={email}",
    "https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
)
HTTP_TIMEOUT_SECONDS = 6

# (url) -> body, or None when there is nothing there.
Fetch = Callable[[str], bytes | None]
# (name, kind) -> records; kind is "MX" (host names) or "SRV" (host, port) pairs.
MxLookup = Callable[[str], list[str]]
SrvLookup = Callable[[str], list[tuple[str, int]]]


def fetch(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": "Postcard"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as reply:
            return reply.read(512_000)
    except (urllib.error.URLError, OSError, ValueError):
        logger.debug("no autoconfig at %s", url, exc_info=True)
        return None


def lookup_mx(domain: str) -> list[str]:
    try:
        records = Gio.Resolver.get_default().lookup_records(
            domain, Gio.ResolverRecordType.MX, None
        )
    except GLib.Error:
        return []
    # (preference, host): lowest preference first, as mail delivery tries them.
    return [host for _preference, host in sorted(r.unpack() for r in records)]


def lookup_srv(name: str) -> list[tuple[str, int]]:
    try:
        records = Gio.Resolver.get_default().lookup_records(
            name, Gio.ResolverRecordType.SRV, None
        )
    except GLib.Error:
        return []
    found = sorted(record.unpack() for record in records)
    # "." as the target means the service is explicitly not offered.
    return [(target, port) for _p, _w, port, target in found if target not in ("", ".")]


def parse_autoconfig(xml: bytes, source: str) -> ServerSettings | None:
    """The first IMAP and SMTP servers of a Thunderbird autoconfig file."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    imap = _server(root, "incomingServer", "imap")
    smtp = _server(root, "outgoingServer", "smtp")
    if imap is None or smtp is None:
        return None
    return ServerSettings(
        imap[0], imap[1], imap[2], smtp[0], smtp[1], smtp[2], imap[3], source
    )


def _server(root: ElementTree.Element, tag: str, kind: str) -> tuple | None:
    for server in root.iter(tag):
        if server.get("type") != kind:
            continue
        socket_type = (server.findtext("socketType") or "").upper()
        # Plain text would send the password in the clear; Account can't even
        # say it, so such an entry is skipped for the next one.
        if socket_type not in ("SSL", "STARTTLS"):
            continue
        host = (server.findtext("hostname") or "").strip()
        port = (server.findtext("port") or "").strip()
        if not host or not port.isdigit():
            continue
        security = SECURITY_TLS if socket_type == "SSL" else SECURITY_STARTTLS
        username = (server.findtext("username") or "%EMAILADDRESS%").strip()
        return host, int(port), security, username
    return None


def discover(
    email: str,
    *,
    fetch: Fetch = fetch,
    lookup_mx: MxLookup = lookup_mx,
    lookup_srv: SrvLookup = lookup_srv,
) -> ServerSettings | None:
    """Server settings for an address, or None when nothing knows the domain.

    Host names and the username come back with their placeholders filled in.
    """
    found = _discover(email, fetch, lookup_mx, lookup_srv)
    if found is None:
        return None
    return ServerSettings(
        fill_placeholders(found.imap_host, email),
        found.imap_port,
        found.imap_security,
        fill_placeholders(found.smtp_host, email),
        found.smtp_port,
        found.smtp_security,
        fill_placeholders(found.username, email),
        found.source,
    )


def _discover(
    email: str, fetch: Fetch, lookup_mx: MxLookup, lookup_srv: SrvLookup
) -> ServerSettings | None:
    _local, at_sign, domain = email.strip().rpartition("@")
    domain = domain.lower()
    if not at_sign or "." not in domain:
        return None

    steps: list[Callable[[], ServerSettings | None]] = [
        lambda: _from_providers(email, domain),
        *(
            lambda template=template: _from_url(
                template.format(domain=domain, email=email), domain, fetch
            )
            for template in DOMAIN_URLS
        ),
        lambda: _from_url(ISPDB_URL.format(domain=domain), "Thunderbird ISPDB", fetch),
        lambda: _from_srv(domain, lookup_srv),
        lambda: _from_mx(domain, fetch, lookup_mx),
    ]
    for step in steps:
        found = step()
        if found is not None:
            return found
    return None


def _from_providers(email: str, domain: str) -> ServerSettings | None:
    known = providers.settings_for_email(email)
    return ServerSettings(*known, source=domain) if known is not None else None


def _from_url(url: str, source: str, fetch: Fetch) -> ServerSettings | None:
    body = fetch(url)
    return parse_autoconfig(body, source) if body else None


def _from_mx(domain: str, fetch: Fetch, lookup_mx: MxLookup) -> ServerSettings | None:
    """A hosting preset the MX host gives away, else the ISPDB entry of the
    MX host's own domain -- a custom domain at a big provider."""
    mx_hosts = lookup_mx(domain)
    matched = hosting.match_mx(mx_hosts)
    if matched is not None:
        return hosting.resolve(*matched)
    if not mx_hosts:
        return None
    mx_domain = ".".join(mx_hosts[0].rstrip(".").split(".")[-2:])
    return _from_url(ISPDB_URL.format(domain=mx_domain), "Thunderbird ISPDB", fetch)


def _from_srv(domain: str, lookup_srv: SrvLookup) -> ServerSettings | None:
    imap = lookup_srv(f"_imaps._tcp.{domain}")
    smtp = lookup_srv(f"_submissions._tcp.{domain}")
    smtp_security = SECURITY_TLS
    if not smtp:
        smtp = lookup_srv(f"_submission._tcp.{domain}")
        smtp_security = SECURITY_STARTTLS
    if not imap or not smtp:
        return None
    return ServerSettings(
        imap[0][0].rstrip("."),
        imap[0][1],
        SECURITY_TLS,
        smtp[0][0].rstrip("."),
        smtp[0][1],
        smtp_security,
        source="DNS",
    )


def fill_placeholders(text: str, email: str) -> str:
    """Autoconfig's %EMAILADDRESS%, %EMAILLOCALPART% and %EMAILDOMAIN%."""
    local, _at, domain = email.strip().rpartition("@")
    return (
        text.replace("%EMAILADDRESS%", email.strip())
        .replace("%EMAILLOCALPART%", local)
        .replace("%EMAILDOMAIN%", domain.lower())
    )
