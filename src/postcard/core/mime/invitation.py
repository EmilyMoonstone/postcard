"""Calendar invitations carried in mail (iMIP, RFC 6047) and answers to them.

Only what reading and answering an invitation needs: the event's title, time,
place and organizer, and a METHOD:REPLY built from the request's own lines.
Everything is plain text in and out, so none of it touches the network.
"""

import email.utils
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.message import EmailMessage, Message
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

METHOD_REQUEST = "REQUEST"
METHOD_CANCEL = "CANCEL"
METHOD_REPLY = "REPLY"

RESPONSE_ACCEPTED = "ACCEPTED"
RESPONSE_TENTATIVE = "TENTATIVE"
RESPONSE_DECLINED = "DECLINED"

# The subject prefix Outlook and Google put on an answer, which organizers'
# clients recognise; left untranslated for the same reason.
_REPLY_SUBJECTS = {
    RESPONSE_ACCEPTED: "Accepted",
    RESPONSE_TENTATIVE: "Tentative",
    RESPONSE_DECLINED: "Declined",
}

# The request lines a reply repeats so the organizer can match it to the event.
_REPLY_PROPERTIES = frozenset(
    {"UID", "RECURRENCE-ID", "SEQUENCE", "DTSTART", "DTEND", "DURATION", "SUMMARY"}
)

# RFC 5545 3.1: a content line is at most 75 octets before folding.
_LINE_OCTETS = 75


@dataclass(frozen=True, slots=True)
class Property:
    name: str
    params: dict[str, str]
    value: str
    line: str  # unfolded, as it appeared


@dataclass(frozen=True, slots=True)
class Invitation:
    method: str
    uid: str
    summary: str = ""
    location: str = ""
    start: datetime | date | None = None
    end: datetime | date | None = None
    organizer: str = ""
    organizer_name: str = ""
    is_recurring: bool = False
    # PARTSTAT by lowercased attendee address.
    responses: dict[str, str] = field(default_factory=dict)
    event_lines: tuple[str, ...] = ()
    timezone_lines: tuple[str, ...] = ()


def find_invitation(message: Message) -> Invitation | None:
    """The first calendar part of a message that parses as an invitation."""
    for part in message.walk():
        if part.get_content_type() != "text/calendar":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        invitation = parse_invitation(text, str(part.get_param("method") or ""))
        if invitation is not None:
            return invitation
    return None


def unfold(text: str) -> list[str]:
    """Content lines with RFC 5545 folding undone."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def parse_property(line: str) -> Property:
    """Split NAME;PARAM=VALUE;PARAM="quoted:value":VALUE into its parts.

    A colon inside a quoted parameter (a CN, a URL) is not the separator.
    """
    in_quotes = False
    separator = len(line)
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            separator = index
            break
    head, value = line[:separator], line[separator + 1 :]
    name, *raw_params = _split_params(head)
    params = {}
    for raw in raw_params:
        key, _, param_value = raw.partition("=")
        params[key.upper()] = param_value.strip('"')
    return Property(name.upper(), params, value, line)


def _split_params(head: str) -> list[str]:
    parts, current, in_quotes = [], "", False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
        if char == ";" and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def unescape(value: str) -> str:
    out, is_escaped = [], False
    for char in value:
        if is_escaped:
            out.append("\n" if char in "nN" else char)
            is_escaped = False
        elif char == "\\":
            is_escaped = True
        else:
            out.append(char)
    return "".join(out)


def parse_invitation(text: str, method: str = "") -> Invitation | None:
    """The first event of a calendar object, or None if it holds none.

    `method` is the MIME part's own method parameter, used when the calendar
    doesn't repeat it.
    """
    event: list[Property] = []
    timezone_lines: list[str] = []
    stack: list[str] = []
    for line in unfold(text):
        prop = parse_property(line)
        if prop.name == "BEGIN":
            stack.append(prop.value.upper())
        is_timezone = "VTIMEZONE" in stack
        if prop.name == "END" and stack:
            finished = stack.pop()
            if is_timezone:
                timezone_lines.append(line)
            if finished == "VEVENT":
                break
            continue
        if is_timezone:
            timezone_lines.append(line)
        elif stack == ["VCALENDAR"] and prop.name == "METHOD":
            method = prop.value
        elif stack[-1:] == ["VEVENT"] and prop.name != "BEGIN":
            # A VALARM inside the event sits one level deeper and is skipped.
            event.append(prop)
    if not event:
        return None
    return _invitation(method.upper(), event, tuple(timezone_lines))


def _invitation(
    method: str, event: list[Property], timezone_lines: tuple[str, ...]
) -> Invitation:
    first = {}
    responses = {}
    for prop in event:
        first.setdefault(prop.name, prop)
        if prop.name == "ATTENDEE":
            responses[_address(prop.value)] = prop.params.get(
                "PARTSTAT", "NEEDS-ACTION"
            )
    organizer = first.get("ORGANIZER")
    return Invitation(
        method=method,
        uid=first["UID"].value if "UID" in first else "",
        summary=unescape(first["SUMMARY"].value) if "SUMMARY" in first else "",
        location=unescape(first["LOCATION"].value) if "LOCATION" in first else "",
        start=parse_time(first["DTSTART"]) if "DTSTART" in first else None,
        end=parse_time(first["DTEND"]) if "DTEND" in first else None,
        organizer=_address(organizer.value) if organizer else "",
        organizer_name=organizer.params.get("CN", "") if organizer else "",
        is_recurring="RRULE" in first,
        responses=responses,
        event_lines=tuple(prop.line for prop in event),
        timezone_lines=timezone_lines,
    )


def _address(value: str) -> str:
    return value[7:].lower() if value.lower().startswith("mailto:") else value.lower()


def parse_time(prop: Property) -> datetime | date | None:
    """A DATE, a UTC DATE-TIME, or a local one in its TZID.

    Outlook names its zones the Windows way ("W. Europe Standard Time"), which
    zoneinfo doesn't know; those read as floating wall-clock time, right for
    everyone in the organizer's zone and an hour or so off for the rest.
    """
    value = prop.value.strip()
    try:
        if prop.params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date()
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        moment = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    tzid = prop.params.get("TZID", "")
    if not tzid:
        return moment
    try:
        return moment.replace(tzinfo=ZoneInfo(tzid))
    except (ZoneInfoNotFoundError, ValueError):
        return moment


def when_text(start: datetime | date | None, end: datetime | date | None) -> str:
    """ "Thu, Sep 17, 19:00 – 21:00", with the end in full when it's another day.

    Times are shown in the local zone; a floating one, as it was written.
    """
    if start is None:
        return ""
    local_start = _local(start)
    if end is None:
        return _moment_text(local_start)
    local_end = _local(end)
    if (
        isinstance(local_start, datetime)
        and isinstance(local_end, datetime)
        and local_start.date() == local_end.date()
    ):
        return f"{_moment_text(local_start)} – {local_end:%H:%M}"
    return f"{_moment_text(local_start)} – {_moment_text(local_end)}"


def _local(moment: datetime | date) -> datetime | date:
    if isinstance(moment, datetime) and moment.tzinfo is not None:
        return moment.astimezone()
    return moment


def _moment_text(moment: datetime | date) -> str:
    if isinstance(moment, datetime):
        return f"{moment:%a, %b %d, %H:%M}"
    return f"{moment:%a, %b %d}"


def build_reply(
    invitation: Invitation,
    attendee: str,
    attendee_name: str,
    response: str,
    now: datetime,
) -> EmailMessage:
    """An iMIP REPLY from attendee to the organizer, as a complete message."""
    attendee_params = f";PARTSTAT={response}"
    if attendee_name:
        attendee_params += f';CN="{attendee_name.replace(chr(34), "")}"'
    event = [
        line
        for line in invitation.event_lines
        if parse_property(line).name in _REPLY_PROPERTIES
        or parse_property(line).name == "ORGANIZER"
    ]
    calendar = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Postcard//Postcard//EN",
        "VERSION:2.0",
        f"METHOD:{METHOD_REPLY}",
        *invitation.timezone_lines,
        "BEGIN:VEVENT",
        *event,
        f"DTSTAMP:{now.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"ATTENDEE{attendee_params}:mailto:{attendee}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    ics = "\r\n".join(fold(line) for line in calendar) + "\r\n"

    verb = _REPLY_SUBJECTS[response]
    message = EmailMessage()
    message["From"] = email.utils.formataddr((attendee_name, attendee))
    message["To"] = email.utils.formataddr(
        (invitation.organizer_name, invitation.organizer)
    )
    message["Subject"] = f"{verb}: {invitation.summary}".strip()
    message["Date"] = email.utils.format_datetime(now)
    message["Message-ID"] = email.utils.make_msgid()
    message.set_content(f"{attendee_name or attendee}: {verb}\n")
    message.add_alternative(ics, subtype="calendar", params={"method": METHOD_REPLY})
    return message


def fold(line: str) -> str:
    """Fold a content line at 75 octets without splitting a UTF-8 character."""
    out, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        limit = _LINE_OCTETS if not out else _LINE_OCTETS - 1
        if size + width > limit:
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return "\r\n ".join(out)
