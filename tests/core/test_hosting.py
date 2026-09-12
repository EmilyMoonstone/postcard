from postcard.core.hosting import match_host, match_mx, preset_named, resolve


def test_a_netcup_domain_uses_its_netcup_mx_host_not_its_own_alias():
    matched = match_mx(["mail.emilypauli.de.", "mxe80f.netcup.net."])

    assert matched is not None
    preset, host = matched
    assert preset.name == "netcup"
    settings = resolve(preset, host)
    assert (settings.imap_host, settings.smtp_host) == (
        "mxe80f.netcup.net",
        "mxe80f.netcup.net",
    )
    assert (settings.imap_port, settings.smtp_port) == (993, 465)
    assert settings.source == "netcup"


def test_fixed_name_providers_are_matched_by_mx():
    matched = match_mx(["mx00.emig.gmx.net"])
    assert matched is not None
    assert resolve(*matched).imap_host == "imap.gmx.net"


def test_an_unknown_mx_matches_nothing():
    assert match_mx(["mx.example.org"]) is None
    assert match_mx([]) is None


def test_a_typed_server_name_gives_the_provider_away():
    assert match_host("mx2f53.netcup.net").name == "netcup"  # type: ignore[union-attr]
    assert match_host("imap.ionos.de").name == "IONOS"  # type: ignore[union-attr]
    assert match_host("imap.example.org") is None


def test_presets_are_found_by_name():
    assert preset_named("STRATO") is not None
    assert preset_named("nope") is None
