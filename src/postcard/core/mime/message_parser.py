import email
import email.utils
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy

from ..models.attachment import Attachment
from .invitation import Invitation, find_invitation

# RFC 2369 wraps each unsubscribe target in angle brackets and separates them
# with commas, which may also appear inside a target -- so match the brackets.
_TARGET = re.compile(r"<([^>]+)>")


@dataclass(frozen=True, slots=True)
class Unsubscribe:
    """Where a mailing list says it will accept an unsubscribe request."""

    url: str = ""
    mailto: str = ""
    is_one_click: bool = False


@dataclass
class ParsedMessage:
    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    subject: str = ""
    from_display: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    date: str = ""
    unsubscribe: Unsubscribe | None = None
    invitation: Invitation | None = None


def parse_message(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(msg, EmailMessage)

    result = ParsedMessage()
    result.subject = str(msg.get("Subject", ""))
    result.from_display = ", ".join(_addresses(msg, "From"))
    result.to = _addresses(msg, "To")
    result.cc = _addresses(msg, "Cc")
    result.bcc = _addresses(msg, "Bcc")
    result.date = _format_date(msg.get("Date"))
    result.unsubscribe = _unsubscribe(msg)
    result.invitation = find_invitation(msg)

    for part in msg.walk():
        if part.is_multipart():
            continue  # a container part -- its children are visited on their own

        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        if disposition == "attachment":
            result.attachments.append(_as_attachment(part))
        elif content_type == "text/plain" and result.text_body is None:
            result.text_body = part.get_content()
        elif content_type == "text/html" and result.html_body is None:
            result.html_body = part.get_content()
        elif content_type == "text/calendar" and result.invitation is not None:
            continue  # shown as the invitation card, not as a nameless file
        else:
            # anything else (an inline image, unrecognised type) -- treat
            # if as an attachment rather than silently dropping it
            result.attachments.append(_as_attachment(part))

    return result


def _unsubscribe(msg: EmailMessage) -> Unsubscribe | None:
    url = ""
    mailto = ""
    for bracketed in _TARGET.findall(str(msg.get("List-Unsubscribe", ""))):
        # These URLs carry a long opaque token, so senders fold the header --
        # and unfolding keeps the continuation whitespace inside the URL, where
        # it makes the request unsendable. Strip every space, not just the ends.
        target = "".join(bracketed.split())
        scheme = target.partition(":")[0].lower()
        # A stranger's header may name any scheme, and a registered handler
        # would happily take file: or smb: from one. http is honoured only as a
        # link, never as a request this app makes itself -- so an https target
        # wins even when an http one was published first.
        if scheme in ("https", "http") and not url.lower().startswith("https:"):
            url = target
        elif scheme == "mailto" and not mailto:
            mailto = target

    if not url and not mailto:
        return None

    post = str(msg.get("List-Unsubscribe-Post", "")).lower()
    is_one_click = url.lower().startswith("https:") and "one-click" in post
    return Unsubscribe(url=url, mailto=mailto, is_one_click=is_one_click)


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    raw = [str(value) for value in msg.get_all(header, [])]
    out = []
    for name, addr in email.utils.getaddresses(raw):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr:
            out.append(addr)
        elif name:
            out.append(name)
    return out


def _format_date(raw: object) -> str:
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return str(raw)
    return parsed.strftime("%b %d, %Y %H:%M")


def _as_attachment(part: EmailMessage) -> Attachment:
    content = part.get_content()
    if isinstance(content, str):
        content = content.encode("utf-8")
    return Attachment(
        filename=part.get_filename() or "attachment",
        mime_type=part.get_content_type(),
        content=content,
    )


# WebKit's auto-load-images setting only gates <img>; a remote stylesheet,
# @import, @font-face or <iframe> loads regardless and leaks the read just the
# same. Only img-src is toggled: remote CSS is never needed to read mail, so it
# stays blocked even after the user asks for images.
_CSP = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:"
_CSP_WITH_IMAGES = _CSP + " https: http:"


# Signs a message was designed with colours of its own: a bgcolor attribute, a
# <font color>, or a CSS background or color that isn't a no-op. Such a body is
# shown on its own light page by default -- darkening only the page behind it
# leaves light table cells with the reader's light text on them.
_OWN_COLORS = re.compile(
    r"\bbgcolor\s*=|<font\b[^>]*\bcolor\s*="
    r"|(?<![-\w])(?:background(?:-color)?|color)\s*:"
    r"(?!\s*(?:inherit|initial|unset|transparent|currentcolor|none)\b)",
    re.IGNORECASE,
)


def has_own_colors(html: str) -> bool:
    """Whether a message body sets colours, and so reads best as it was made."""
    return bool(_OWN_COLORS.search(html))


# The reader's own colours, so a message sits in the UI instead of on a white
# sheet. Only defaults: a message that sets its own colours keeps them, which is
# what the light/dark switch above each body is for.
_DARK_STYLE = (
    ":root{color-scheme:dark}html{background:#1e1e1e;color:#ffffffde}a{color:#78aeed}"
)
_LIGHT_STYLE = (
    ":root{color-scheme:light}html{background:#ffffff;color:#000000cc}a{color:#1c71d8}"
)
# Everyone's defaults: no margin doubling up on the frame's, and images that
# don't push the body wider than the reader.
_BASE_STYLE = "body{margin:12px;overflow-wrap:anywhere}img{max-width:100%;height:auto}"


def sandbox_html(
    html: str,
    *,
    are_remote_images_allowed: bool,
    is_dark: bool = False,
    font: tuple[str, str] = ("", ""),
) -> str:
    """Wrap a message body in a document whose CSP blocks remote subresources.

    `font` is the UI's (family, point size), the default for text the message
    doesn't style, so a plain message reads like the rest of the app.
    """
    policy = _CSP_WITH_IMAGES if are_remote_images_allowed else _CSP
    style = _BASE_STYLE + (_DARK_STYLE if is_dark else _LIGHT_STYLE)
    family, size = font
    if family and size.isdigit():
        quoted = "".join(char for char in family if char not in '\\"<>{};')
        style += f'html{{font-family:"{quoted}",sans-serif;font-size:{size}pt}}'
    return (
        '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" '
        f'content="{policy}"><style>{style}</style></head><body>{html}</body></html>'
    )
