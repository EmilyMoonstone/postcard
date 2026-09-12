import email
from datetime import UTC, date, datetime
from email import policy
from zoneinfo import ZoneInfo

from postcard.core.mime.invitation import (
    METHOD_CANCEL,
    METHOD_REPLY,
    METHOD_REQUEST,
    RESPONSE_ACCEPTED,
    RESPONSE_DECLINED,
    build_reply,
    find_invitation,
    fold,
    parse_invitation,
    parse_property,
    unfold,
    when_text,
)

REQUEST = (
    "BEGIN:VCALENDAR\r\n"
    "METHOD:REQUEST\r\n"
    "PRODID:Microsoft Exchange Server 2010\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VTIMEZONE\r\n"
    "TZID:W. Europe Standard Time\r\n"
    "BEGIN:STANDARD\r\n"
    "DTSTART:16010101T030000\r\n"
    "TZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0100\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\n"
    'ORGANIZER;CN="Pauli, Emily":mailto:Emily.Pauli@ej-ffb.org\r\n'
    "ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN=Ada:mailto:a\r\n"
    " da@example.com\r\n"
    "SUMMARY;LANGUAGE=de-DE:Kammersitzung\\, Jugendwerk\r\n"
    "DTSTART;TZID=W. Europe Standard Time:20260917T190000\r\n"
    "DTEND;TZID=W. Europe Standard Time:20260917T210000\r\n"
    "UID:040000008200E00074C5B7101A82E008\r\n"
    "SEQUENCE:2\r\n"
    "LOCATION:Jugendwerk\\, Raum 1\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "SUMMARY:Reminder\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_a_request_reads_title_place_time_and_organizer():
    invitation = parse_invitation(REQUEST)

    assert invitation is not None
    assert invitation.method == METHOD_REQUEST
    assert invitation.summary == "Kammersitzung, Jugendwerk"
    assert invitation.location == "Jugendwerk, Raum 1"
    assert invitation.start == datetime(2026, 9, 17, 19, 0)
    assert invitation.organizer == "emily.pauli@ej-ffb.org"
    assert invitation.organizer_name == "Pauli, Emily"
    assert invitation.responses == {"ada@example.com": "NEEDS-ACTION"}
    assert len(invitation.timezone_lines) == 8


def test_an_alarm_inside_the_event_does_not_rename_it():
    invitation = parse_invitation(REQUEST)
    assert invitation is not None
    assert "Reminder" not in " ".join(invitation.event_lines)


def test_the_mime_method_is_used_when_the_calendar_names_none():
    text = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nEND:VEVENT\r\nEND:VCALENDAR"
    invitation = parse_invitation(text, "cancel")
    assert invitation is not None and invitation.method == METHOD_CANCEL


def test_a_calendar_without_an_event_is_no_invitation():
    assert (
        parse_invitation("BEGIN:VCALENDAR\r\nMETHOD:PUBLISH\r\nEND:VCALENDAR") is None
    )


def test_known_zones_utc_and_whole_days_parse():
    zoned = parse_invitation(
        "BEGIN:VEVENT\r\nDTSTART;TZID=Europe/Berlin:20260917T190000\r\n"
        "DTEND:20260917T190000Z\r\nEND:VEVENT"
    )
    assert zoned is not None
    assert zoned.start == datetime(2026, 9, 17, 19, tzinfo=ZoneInfo("Europe/Berlin"))
    assert zoned.end == datetime(2026, 9, 17, 19, tzinfo=UTC)

    whole_day = parse_invitation(
        "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260917\r\nEND:VEVENT"
    )
    assert whole_day is not None and whole_day.start == date(2026, 9, 17)


def test_a_quoted_parameter_may_hold_colons_and_semicolons():
    prop = parse_property('ATTENDEE;CN="Doe; Jane: PhD";PARTSTAT=ACCEPTED:mailto:j@x')
    assert prop.params == {"CN": "Doe; Jane: PhD", "PARTSTAT": "ACCEPTED"}
    assert prop.value == "mailto:j@x"


def test_folding_round_trips_without_splitting_characters():
    line = "SUMMARY:" + "Grüße " * 40
    folded = fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert unfold(folded) == [line]


def test_a_reply_repeats_the_event_and_answers_for_the_attendee():
    invitation = parse_invitation(REQUEST)
    assert invitation is not None
    now = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)

    message = build_reply(invitation, "ada@example.com", "Ada", RESPONSE_ACCEPTED, now)

    assert message["To"] == '"Pauli, Emily" <emily.pauli@ej-ffb.org>'
    assert message["Subject"] == "Accepted: Kammersitzung, Jugendwerk"
    reply = find_invitation(
        email.message_from_bytes(message.as_bytes(), policy=policy.default)
    )
    assert reply is not None
    assert reply.method == METHOD_REPLY
    assert reply.uid == invitation.uid
    assert reply.responses == {"ada@example.com": "ACCEPTED"}
    assert "SEQUENCE:2" in reply.event_lines
    assert "DTSTAMP:20260912T200000Z" in reply.event_lines
    assert reply.timezone_lines == invitation.timezone_lines
    assert not any(line.startswith("LOCATION") for line in reply.event_lines)


def test_a_declined_reply_says_so():
    invitation = parse_invitation(REQUEST)
    assert invitation is not None
    message = build_reply(
        invitation, "ada@example.com", "", RESPONSE_DECLINED, datetime.now(UTC)
    )
    assert message["Subject"].startswith("Declined:")
    assert "PARTSTAT=DECLINED" in message.as_string()


def test_when_text_shortens_an_end_on_the_same_day():
    assert when_text(datetime(2026, 9, 17, 19), datetime(2026, 9, 17, 21)) == (
        "Thu, Sep 17, 19:00 – 21:00"
    )
    assert (
        when_text(date(2026, 9, 17), date(2026, 9, 18)) == "Thu, Sep 17 – Fri, Sep 18"
    )
    assert when_text(None, None) == ""
