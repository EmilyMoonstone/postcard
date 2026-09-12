import base64

from postcard.core.mime.preview import PREVIEW_LENGTH, preview_text

PLAIN = b"Content-Type: text/plain; charset=utf-8\r\n"


def test_a_plain_body_becomes_one_line():
    assert preview_text(PLAIN, b"Hallo zusammen,\r\n\r\n  wir laden euch ein.") == (
        "Hallo zusammen, wir laden euch ein."
    )


def test_the_preview_is_capped():
    assert len(preview_text(PLAIN, b"word " * 200)) == PREVIEW_LENGTH


def test_quoted_printable_is_decoded():
    headers = PLAIN + b"Content-Transfer-Encoding: quoted-printable\r\n"

    assert preview_text(headers, b"Gr=C3=BC=C3=9Fe aus M=C3=BCn=\r\nchen") == (
        "Grüße aus München"
    )


def test_base64_cut_mid_quantum_still_decodes_what_arrived():
    encoded = base64.b64encode("Schöne Grüße aus Fürstenfeldbruck".encode())
    headers = PLAIN + b"Content-Transfer-Encoding: base64\r\n"

    text = preview_text(headers, encoded[:30])

    assert text.startswith("Schöne Grüße")
    assert "�" not in text


def test_a_multipart_slice_prefers_the_plain_part():
    headers = b'Content-Type: multipart/alternative; boundary="b1"\r\n'
    body = (
        b"--b1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>From <b>HTML</b></p>\r\n"
        b"--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"From plain text, and then the slice ends mid-sen"
    )

    assert preview_text(headers, body) == (
        "From plain text, and then the slice ends mid-sen"
    )


def test_html_only_mail_is_stripped_to_text():
    headers = b"Content-Type: text/html; charset=iso-8859-1\r\n"

    assert preview_text(
        headers, "<div>Gr\xfc\xdfe<br>und <i>tsch\xfcss".encode("latin-1")
    ) == ("Grüße und tschüss")


def test_a_nested_multipart_reaches_the_text_inside():
    headers = b'Content-Type: multipart/mixed; boundary="outer"\r\n'
    body = (
        b'--outer\r\nContent-Type: multipart/alternative; boundary="inner"\r\n\r\n'
        b"--inner\r\nContent-Type: text/plain\r\n\r\nInside the alternative\r\n"
    )

    assert preview_text(headers, body) == "Inside the alternative"


def test_an_attachment_only_slice_has_no_preview():
    headers = b'Content-Type: multipart/mixed; boundary="b"\r\n'
    body = (
        b"--b\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment\r\n\r\nJVBERi0x"
    )

    assert preview_text(headers, body) == ""


def test_an_unknown_charset_falls_back_to_utf8():
    headers = b"Content-Type: text/plain; charset=x-made-up\r\n"

    assert preview_text(headers, "Grüße".encode()) == "Grüße"
