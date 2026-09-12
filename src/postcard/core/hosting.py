"""Mail hosting providers, for filling in server settings people don't know.

A web host's mailboxes rarely sit behind imap.<your-domain>. netcup and
all-inkl put each customer on a named server, which is also the domain's MX
host; the rest use one fixed name. Each preset knows which, and the MX hosts
that give the provider away, so a domain can be matched without asking.
"""

from dataclasses import dataclass

from .models.account import SECURITY_STARTTLS, SECURITY_TLS

# Stands for the domain's own MX host in a preset's server names.
MX_HOST = "{mx}"


@dataclass(frozen=True, slots=True)
class ServerSettings:
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    # "%EMAILADDRESS%" or "%EMAILLOCALPART%", as in Thunderbird's autoconfig.
    username: str = "%EMAILADDRESS%"
    # Where the settings came from, for telling the user.
    source: str = ""


@dataclass(frozen=True, slots=True)
class HostingPreset:
    name: str
    settings: ServerSettings
    # Suffixes of MX host names that belong to this provider.
    mx_suffixes: tuple[str, ...] = ()
    # Shown under the name: where to find what the preset can't know.
    hint: str = ""

    @property
    def needs_mx(self) -> bool:
        return MX_HOST in (self.settings.imap_host, self.settings.smtp_host)


def _tls(imap: str, smtp: str, smtp_port: int = 465) -> ServerSettings:
    security = SECURITY_TLS if smtp_port == 465 else SECURITY_STARTTLS
    return ServerSettings(imap, 993, SECURITY_TLS, smtp, smtp_port, security)


PRESETS: tuple[HostingPreset, ...] = (
    HostingPreset(
        "netcup",
        _tls(MX_HOST, MX_HOST),
        (".netcup.net",),
        "The server is the mxXXXX.netcup.net name in your customer panel.",
    ),
    HostingPreset(
        "all-inkl.com",
        _tls(MX_HOST, MX_HOST),
        (".kasserver.com",),
        "The server is the w0XXXXXX.kasserver.com name in KAS.",
    ),
    HostingPreset(
        "Hetzner",
        _tls("mail.your-server.de", "mail.your-server.de"),
        (".your-server.de",),
    ),
    HostingPreset(
        "IONOS",
        _tls("imap.ionos.de", "smtp.ionos.de"),
        (".ionos.de", ".1and1.com", ".kundenserver.de"),
    ),
    HostingPreset("STRATO", _tls("imap.strato.de", "smtp.strato.de"), (".rzone.de",)),
    HostingPreset(
        "Hostinger",
        _tls("imap.hostinger.com", "smtp.hostinger.com"),
        (".hostinger.com",),
    ),
    HostingPreset(
        "mailbox.org", _tls("imap.mailbox.org", "smtp.mailbox.org"), (".mailbox.org",)
    ),
    HostingPreset("Posteo", _tls("posteo.de", "posteo.de"), (".posteo.de",)),
    HostingPreset("GMX", _tls("imap.gmx.net", "mail.gmx.net"), (".gmx.net",)),
    HostingPreset("WEB.DE", _tls("imap.web.de", "smtp.web.de", 587), (".web.de",)),
    HostingPreset(
        "Telekom (T-Online)",
        _tls("secureimap.t-online.de", "securesmtp.t-online.de"),
        (".t-online.de",),
    ),
    HostingPreset("Uberspace", _tls(MX_HOST, MX_HOST), (".uberspace.de",)),
    HostingPreset(
        "Fastmail",
        _tls("imap.fastmail.com", "smtp.fastmail.com"),
        (".messagingengine.com",),
    ),
    HostingPreset(
        "Zoho Mail", _tls("imap.zoho.eu", "smtp.zoho.eu"), (".zoho.eu", ".zoho.com")
    ),
    HostingPreset(
        "Google Workspace",
        _tls("imap.gmail.com", "smtp.gmail.com", 587),
        (".google.com", ".googlemail.com"),
        "Needs an app password, or add the account through Online Accounts.",
    ),
    HostingPreset(
        "Microsoft 365",
        _tls("outlook.office365.com", "smtp.office365.com", 587),
        (".outlook.com",),
        "Microsoft no longer accepts passwords; add it through Online Accounts.",
    ),
    HostingPreset(
        "iCloud Mail",
        _tls("imap.mail.me.com", "smtp.mail.me.com", 587),
        (".icloud.com",),
    ),
    HostingPreset(
        "Yahoo Mail",
        _tls("imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
        (".yahoodns.net",),
    ),
    HostingPreset(
        "Proton Mail Bridge",
        ServerSettings(
            "127.0.0.1", 1143, SECURITY_STARTTLS, "127.0.0.1", 1025, SECURITY_STARTTLS
        ),
        (".protonmail.ch",),
        "Use the password the Bridge shows, not your Proton password.",
    ),
)


def preset_named(name: str) -> HostingPreset | None:
    return next((preset for preset in PRESETS if preset.name == name), None)


def match_mx(mx_hosts: list[str]) -> tuple[HostingPreset, str] | None:
    """The provider a domain's MX records point at, and the matching MX host.

    The host matters: a domain can list its own name first (mail.example.de)
    in front of the provider's server, and only the latter has a certificate.
    """
    for host in mx_hosts:
        lowered = host.rstrip(".").lower()
        for preset in PRESETS:
            if any(lowered.endswith(suffix) for suffix in preset.mx_suffixes):
                return preset, lowered
    return None


def match_host(host: str) -> HostingPreset | None:
    """The provider a server name the user typed belongs to."""
    lowered = host.strip().rstrip(".").lower()
    for preset in PRESETS:
        names = {preset.settings.imap_host, preset.settings.smtp_host} - {MX_HOST}
        if lowered in names or any(lowered.endswith(s) for s in preset.mx_suffixes):
            return preset
    return None


def resolve(preset: HostingPreset, mx_host: str) -> ServerSettings:
    """A preset's settings with {mx} filled in; unchanged when it has none."""
    settings = preset.settings
    host = mx_host.rstrip(".").lower()
    return ServerSettings(
        settings.imap_host.replace(MX_HOST, host),
        settings.imap_port,
        settings.imap_security,
        settings.smtp_host.replace(MX_HOST, host),
        settings.smtp_port,
        settings.smtp_security,
        settings.username,
        preset.name,
    )
