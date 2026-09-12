from postcard.core.autoconfig import discover, fill_placeholders, parse_autoconfig

AUTOCONFIG = b"""<?xml version="1.0"?>
<clientConfig version="1.1">
  <emailProvider id="example.org">
    <incomingServer type="pop3">
      <hostname>pop.example.org</hostname><port>995</port><socketType>SSL</socketType>
    </incomingServer>
    <incomingServer type="imap">
      <hostname>imap.example.org</hostname><port>143</port>
      <socketType>plain</socketType>
    </incomingServer>
    <incomingServer type="imap">
      <hostname>imap.%EMAILDOMAIN%</hostname><port>993</port>
      <socketType>SSL</socketType><username>%EMAILLOCALPART%</username>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>smtp.example.org</hostname><port>587</port>
      <socketType>STARTTLS</socketType>
    </outgoingServer>
  </emailProvider>
</clientConfig>"""


def test_the_first_encrypted_imap_and_smtp_servers_are_taken():
    settings = parse_autoconfig(AUTOCONFIG, "example.org")

    assert settings is not None
    assert (settings.imap_host, settings.imap_port, settings.imap_security) == (
        "imap.%EMAILDOMAIN%",
        993,
        "tls",
    )
    assert (settings.smtp_port, settings.smtp_security) == (587, "starttls")
    assert settings.username == "%EMAILLOCALPART%"


def test_broken_xml_is_no_settings():
    assert parse_autoconfig(b"<not xml", "x") is None


def no_fetch(url):
    return None


def no_dns(name):
    return []


def test_a_known_provider_needs_no_network():
    def fail(*_args):
        raise AssertionError("no lookup for a known provider")

    settings = discover("ada@gmail.com", fetch=fail, lookup_mx=fail, lookup_srv=fail)

    assert settings is not None and settings.imap_host == "imap.gmail.com"


def test_the_domain_s_own_autoconfig_is_asked_first_and_filled_in():
    asked = []

    def fetch(url):
        asked.append(url)
        return AUTOCONFIG if url.startswith("https://autoconfig.example.org/") else None

    settings = discover(
        "ada@example.org", fetch=fetch, lookup_mx=no_dns, lookup_srv=no_dns
    )

    assert settings is not None
    assert settings.imap_host == "imap.example.org"
    assert settings.username == "ada"
    assert len(asked) == 1


def test_the_ispdb_follows_when_the_domain_has_nothing():
    def fetch(url):
        return AUTOCONFIG if "thunderbird.net" in url else None

    settings = discover(
        "ada@example.org", fetch=fetch, lookup_mx=no_dns, lookup_srv=no_dns
    )

    assert settings is not None and settings.source == "Thunderbird ISPDB"


def test_srv_records_come_before_the_mx_guess():
    records = {
        "_imaps._tcp.example.org": [("mail.example.org.", 993)],
        "_submission._tcp.example.org": [("mail.example.org.", 587)],
    }

    settings = discover(
        "ada@example.org",
        fetch=no_fetch,
        lookup_mx=lambda d: ["mxe80f.netcup.net"],
        lookup_srv=lambda name: records.get(name, []),
    )

    assert settings is not None
    assert (settings.imap_host, settings.smtp_security) == (
        "mail.example.org",
        "starttls",
    )


def test_a_hosted_domain_is_found_through_its_mx_host():
    settings = discover(
        "mail@emilypauli.de",
        fetch=no_fetch,
        lookup_mx=lambda d: ["mail.emilypauli.de", "mxe80f.netcup.net"],
        lookup_srv=no_dns,
    )

    assert settings is not None
    assert settings.imap_host == "mxe80f.netcup.net"
    assert settings.username == "mail@emilypauli.de"


def test_a_custom_domain_at_a_big_provider_is_found_in_the_ispdb_by_mx():
    def fetch(url):
        return AUTOCONFIG if url.endswith("/v1.1/bigmail.com") else None

    settings = discover(
        "ada@example.org",
        fetch=fetch,
        lookup_mx=lambda d: ["mx1.bigmail.com"],
        lookup_srv=no_dns,
    )

    assert settings is not None and settings.source == "Thunderbird ISPDB"


def test_nothing_known_is_none_and_no_address_asks_nothing():
    assert (
        discover("ada@example.org", fetch=no_fetch, lookup_mx=no_dns, lookup_srv=no_dns)
        is None
    )
    assert (
        discover("not-an-address", fetch=no_fetch, lookup_mx=no_dns, lookup_srv=no_dns)
        is None
    )


def test_placeholders_are_filled():
    assert fill_placeholders("%EMAILLOCALPART%@%EMAILDOMAIN%", "Ada@Example.org") == (
        "Ada@example.org"
    )
