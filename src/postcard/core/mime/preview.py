"""A short plain-text preview from the first bytes of a message.

IMAP can hand over a byte range of a body without the rest, so a sync fetches
the headers plus a few kilobytes of the body and builds the list's preview
line from those. The slice ends wherever it ends: mid-part, mid-base64
quantum, mid-UTF-8 sequence. Everything here is tolerant of that.
"""

import base64
import binascii
import email
from email import policy
from email.message import Message

from ..compose import html_to_text

PREVIEW_LENGTH = 200

# How much of the body a sync asks for: enough to get past a multipart
# preamble and a part's headers to the first sentences of text.
PREVIEW_BYTES = 2048


def preview_text(header_bytes: bytes, partial_body: bytes) -> str:
    """The first words of a message's readable text, "" when there are none.

    header_bytes must carry Content-Type and Content-Transfer-Encoding, since
    the body alone doesn't say how to read itself.
    """
    headers = header_bytes.rstrip(b"\r\n") + b"\r\n\r\n"
    message = email.message_from_bytes(headers + partial_body, policy=policy.compat32)
    part = _readable_part(message)
    if part is None:
        return ""
    text = _decoded_text(part)
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    # A slice can end inside a multi-byte character, which decodes as U+FFFD.
    return " ".join(text.split()).rstrip("�")[:PREVIEW_LENGTH]


def _readable_part(message: Message) -> Message | None:
    html = None
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            return part
        if content_type == "text/html" and html is None:
            html = part
    return html


def _decoded_text(part: Message) -> str:
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if encoding == "base64":
        # The email package gives up on the whole payload when the last quantum
        # is cut short, so this one is decoded by hand.
        payload = part.get_payload()
        data = _truncated_base64(payload) if isinstance(payload, str) else b""
    else:
        # Raw bytes, quoted-printable already undone: without decode=True the
        # package decodes 8-bit text itself, and badly for an unknown charset.
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""

    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _truncated_base64(payload: str) -> bytes:
    # Drop the incomplete quantum the slice may have cut, and whatever follows
    # the part (a boundary line) so it doesn't read as base64 too.
    encoded = "".join(payload.split("--", 1)[0].split())
    encoded = encoded[: len(encoded) // 4 * 4]
    try:
        return base64.b64decode(encoded)
    except (binascii.Error, ValueError):
        return b""
