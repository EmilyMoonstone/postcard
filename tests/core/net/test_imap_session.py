import imaplib
import socket
import ssl
import threading
import time

import pytest

from postcard.core.net.auth import MECHANISM_XOAUTH2, Credential
from postcard.core.net.imap_session import (
    FetchedHeader,
    ImapError,
    ImapSession,
    special_use_role,
)


class FakeImap:
    def __init__(
        self, search_reply=("OK", [b"4 9 20"]), fetch_reply=None, status_reply=None
    ):
        self.calls = []
        self.welcome = b"fake imap"
        self._search_reply = search_reply
        self._fetch_reply = fetch_reply
        self._status_reply = status_reply

    def uid(self, *args):
        self.calls.append(args)
        return self._search_reply

    def fetch(self, *args):
        self.calls.append(args)
        return self._fetch_reply

    def status(self, *args):
        self.calls.append(args)
        return self._status_reply

    def authenticate(self, mechanism, authobject):
        self.calls.append(("AUTHENTICATE", mechanism, authobject(b"")))

    def login(self, user, password):
        self.calls.append(("LOGIN", user, password))

    def starttls(self, ssl_context=None):
        self.calls.append(("STARTTLS", ssl_context))


def connect(monkeypatch, imap: FakeImap) -> ImapSession:
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: imap,
    )
    session = ImapSession("imap.example.com", 993)
    session.connect()
    return session


def test_a_connection_that_answers_noop_is_alive(monkeypatch):
    imap = FakeImap()
    imap.noop = lambda: ("OK", [b""])

    assert connect(monkeypatch, imap).is_alive() is True


def _raising(error: Exception):
    def noop():
        raise error

    return noop


@pytest.mark.parametrize(
    "noop",
    [
        lambda: ("NO", [b"try again later"]),
        lambda: ("BAD", [b"unknown command"]),
        _raising(OSError("connection reset")),
        _raising(imaplib.IMAP4.abort("socket error: EOF")),
    ],
)
def test_a_connection_that_does_not_answer_a_noop_is_not_alive(monkeypatch, noop):
    imap = FakeImap()
    imap.noop = noop

    assert connect(monkeypatch, imap).is_alive() is False


def test_search_all_uids_uses_uid_search_all_and_returns_the_uids(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.search_all_uids() == {"4", "9", "20"}
    assert imap.calls == [("SEARCH", "ALL")]


# --- search failure messages ------------------------------------------------
# Each of these used to raise the same "malformed data" text, which said
# nothing about which of the four checks had tripped.


def test_a_non_ok_status_says_the_search_failed(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("NO", [b"nope"])))
    with pytest.raises(ImapError, match="search failed"):
        session.search_all_uids()


def test_a_non_list_payload_names_the_type_it_got(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", "4 9")))
    with pytest.raises(ImapError, match="search returned str, not a list"):
        session.search_all_uids()


def test_a_non_bytes_item_is_reported_as_such(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", [b"4", 9])))
    with pytest.raises(ImapError, match="non-bytes item"):
        session.search_all_uids()


def test_non_numeric_uids_are_listed(monkeypatch):
    session = connect(monkeypatch, FakeImap(search_reply=("OK", [b"4 bogus"])))
    with pytest.raises(ImapError, match=r"non-numeric UIDs: \['bogus'\]"):
        session.search_all_uids()


# --- unseen_count -----------------------------------------------------------


def test_unseen_count_asks_for_status_and_reads_the_number(monkeypatch):
    imap = FakeImap(status_reply=("OK", [b'"[Gmail]/All Mail" (UNSEEN 12)']))
    session = connect(monkeypatch, imap)

    assert session.unseen_count("[Gmail]/All Mail") == 12
    # Quoted, so the space stays inside one token and the server doesn't say BAD.
    assert imap.calls == [('"[Gmail]/All Mail"', "(UNSEEN)")]


def test_a_refused_status_names_the_mailbox(monkeypatch):
    session = connect(monkeypatch, FakeImap(status_reply=("NO", [b"unavailable"])))
    with pytest.raises(ImapError, match="could not read the status of Junk"):
        session.unseen_count("Junk")


def test_a_status_reply_without_an_unseen_field_is_an_error(monkeypatch):
    session = connect(monkeypatch, FakeImap(status_reply=("OK", [b'"X" (MESSAGES 9)'])))
    with pytest.raises(ImapError, match="no UNSEEN"):
        session.unseen_count("X")


def test_an_empty_status_reply_is_an_error(monkeypatch):
    session = connect(monkeypatch, FakeImap(status_reply=("OK", [None])))
    with pytest.raises(ImapError, match="no UNSEEN"):
        session.unseen_count("X")


# --- fetch_recent_headers ---------------------------------------------------

HEADER_BYTES = (
    b"Date: Thu, 16 Jul 2026 10:00:00 +0000\r\n"
    b"From: Ada Lovelace <ada@example.com>\r\n"
    b"To: Me <me@example.com>\r\n"
    b"Subject: Lunch\r\n"
    b"Message-ID: <a@example.com>\r\n"
    b"\r\n"
)


def test_fetch_recent_headers_returns_typed_rows(monkeypatch):
    # Arrange: one message, seen but not flagged, plus the stray ")" line
    # imaplib appends as plain bytes rather than a tuple.
    reply = (
        "OK",
        [(b"1 (UID 42 FLAGS (\\Seen) BODY[HEADER.FIELDS ...]", HEADER_BYTES), b")"],
    )
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    # Act
    (header,) = session.fetch_recent_headers(exists=1, limit=50)

    # Assert: raw wire values, not display-ready ones -- the date is still the
    # original RFC 5322 string and the address is unparsed.
    assert isinstance(header, FetchedHeader)
    assert header.uid == "42"
    assert header.from_header == "Ada Lovelace <ada@example.com>"
    assert header.subject == "Lunch"
    assert header.date == "Thu, 16 Jul 2026 10:00:00 +0000"
    assert header.seen is True
    assert header.flagged is False


def test_a_missing_header_reads_back_as_empty(monkeypatch):
    reply = ("OK", [(b"1 (UID 7 FLAGS ()", b"Subject: Hi\r\n\r\n")])
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    (header,) = session.fetch_recent_headers(exists=1, limit=50)

    assert header.cc_header == ""
    assert header.in_reply_to == ""
    assert header.references == ""
    assert header.seen is False


def test_an_empty_mailbox_is_not_fetched_at_all(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.fetch_recent_headers(exists=0, limit=50) == []
    assert imap.calls == []


def test_paging_past_the_oldest_message_returns_nothing(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    assert session.fetch_recent_headers(exists=10, limit=50, offset=10) == []
    assert imap.calls == []


# --- append -----------------------------------------------------------------


class AppendingImap(FakeImap):
    def __init__(self, reply=("OK", [b"[APPENDUID 1 7] APPEND completed"])):
        super().__init__()
        self._reply = reply
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1")

    def append(self, *args):
        self.calls.append(args)
        return self._reply


def test_append_quotes_the_mailbox_and_stores_the_copy_as_read(monkeypatch):
    # A Sent mailbox with a space in its name arrives as two tokens unless it
    # is quoted, and the server answers BAD.
    imap = AppendingImap()
    session = connect(monkeypatch, imap)

    session.append("[Gmail]/Sent Mail", b"raw")

    assert imap.calls == [('"[Gmail]/Sent Mail"', "\\Seen", None, b"raw")]


def test_append_raises_when_the_server_refuses(monkeypatch):
    session = connect(monkeypatch, AppendingImap(reply=("NO", [b"[TRYCREATE]"])))

    with pytest.raises(ImapError, match="could not append to Sent"):
        session.append("Sent", b"raw")


def test_has_capability_is_case_insensitive(monkeypatch):
    session = connect(monkeypatch, AppendingImap())

    assert session.has_capability("x-gm-ext-1")
    assert not session.has_capability("X-SOMETHING-ELSE")


def test_xoauth2_hands_imaplib_the_raw_sasl_response(monkeypatch):
    # imaplib base64-encodes the callback's return itself, so it has to get the
    # bytes unencoded.
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    session.sign_in(Credential("me@example.com", "token", MECHANISM_XOAUTH2))

    assert imap.calls == [
        ("AUTHENTICATE", "XOAUTH2", b"user=me@example.com\x01auth=Bearer token\x01\x01")
    ]


def test_a_password_account_still_uses_plain_login(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    session.sign_in(Credential("me@example.com", "hunter2"))

    assert imap.calls == [("LOGIN", "me@example.com", "hunter2")]


def test_an_unknown_mechanism_fails_rather_than_staying_unauthenticated(monkeypatch):
    imap = FakeImap()
    session = connect(monkeypatch, imap)

    with pytest.raises(ImapError, match="unsupported mechanism none"):
        session.sign_in(Credential("me@example.com", "", "none"))

    assert imap.calls == []


# --- TLS -------------------------------------------------------------------
# imaplib defaults to ssl._create_stdlib_context(): check_hostname off,
# verify_mode CERT_NONE. Left alone, every sync accepts any certificate and
# hands the password to whoever is on the path.


def _assert_verifies(context: ssl.SSLContext) -> None:
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_connect_gives_imap4_ssl_a_verifying_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: captured.update(kwargs) or FakeImap(),
    )

    ImapSession("imap.example.com", 993).connect()

    _assert_verifies(captured["ssl_context"])


def test_connect_gives_starttls_a_verifying_context(monkeypatch):
    imap = FakeImap()
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4",
        lambda *args, **kwargs: imap,
    )

    ImapSession("imap.example.com", 143, "starttls").connect()

    assert imap.calls[0][0] == "STARTTLS"
    _assert_verifies(imap.calls[0][1])


# --- SPECIAL-USE -------------------------------------------------------------


def test_list_folders_reads_the_role_a_special_use_attribute_states(monkeypatch):
    imap = FakeImap()
    imap.list = lambda: (
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "Gesendete Objekte"',
            b'(\\HasNoChildren \\Trash) "/" "Papierkorb"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"',
        ],
    )

    folders = connect(monkeypatch, imap).list_folders()

    assert [(folder.name, folder.role) for folder in folders] == [
        ("INBOX", ""),
        ("Gesendete Objekte", "sent"),
        ("Papierkorb", "trash"),
        ("[Gmail]", ""),
        ("[Gmail]/Alle Nachrichten", "archive"),
    ]


def test_special_use_attributes_match_regardless_of_case():
    assert special_use_role("\\HasNoChildren \\JUNK") == "junk"
    assert special_use_role("\\Marked \\Drafts") == "drafts"
    assert special_use_role("\\HasNoChildren") == ""


# --- previews from a body slice -----------------------------------------------


def test_each_message_s_header_and_body_slice_are_kept_together(monkeypatch):
    reply = (
        "OK",
        [
            (
                b"1 (UID 42 FLAGS (\\Seen) BODY[HEADER.FIELDS (DATE)] {60}",
                b"Content-Type: text/plain\r\n" + HEADER_BYTES,
            ),
            (b" BODY[TEXT]<0> {11}", b"First mail\r\n"),
            b")",
            # Answered body first, and FLAGS after the last literal.
            (b"2 (UID 43 BODY[TEXT]<0> {12}", b"Second mail\r\n"),
            (b" BODY[HEADER.FIELDS (DATE)] {40}", b"Subject: Two\r\n\r\n"),
            b" FLAGS (\\Flagged))",
        ],
    )
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    first, second = session.fetch_recent_headers(exists=2, limit=50)

    assert (first.uid, first.subject, first.preview) == ("42", "Lunch", "First mail")
    assert (second.uid, second.subject, second.preview) == ("43", "Two", "Second mail")
    assert second.flagged is True


def test_the_fetch_asks_for_a_body_slice_without_marking_it_seen(monkeypatch):
    imap = FakeImap(fetch_reply=("OK", []))
    session = connect(monkeypatch, imap)

    session.fetch_recent_headers(exists=3, limit=50)

    [(_sequence, items)] = imap.calls
    assert "BODY.PEEK[TEXT]<0.2048>" in items
    assert "CONTENT-TRANSFER-ENCODING" in items


# --- server search ------------------------------------------------------------


def test_search_text_sends_the_query_as_a_utf8_literal(monkeypatch):
    imap = FakeImap(search_reply=("OK", [b"3 17"]))
    imap.literal = None
    session = connect(monkeypatch, imap)

    assert session.search_text('Grüße "Kammer"') == {"3", "17"}
    assert imap.calls == [("SEARCH", "CHARSET", "UTF-8", "TEXT")]
    assert imap.literal == 'Grüße "Kammer"'.encode()


def test_fetch_headers_by_uid_fetches_exactly_those_uids(monkeypatch):
    reply = (
        "OK",
        [
            (
                b"9 (UID 17 FLAGS () BODY[HEADER.FIELDS (SUBJECT)] {20}",
                b"Subject: Hit\r\n\r\n",
            )
        ],
    )
    imap = FakeImap(search_reply=reply)
    session = connect(monkeypatch, imap)

    [header] = session.fetch_headers_by_uid(["17", "3"])

    assert (header.uid, header.subject) == ("17", "Hit")
    assert imap.calls[0][:2] == ("FETCH", "17,3")


def test_fetching_no_uids_asks_nothing(monkeypatch):
    imap = FakeImap()
    assert connect(monkeypatch, imap).fetch_headers_by_uid([]) == []
    assert imap.calls == []


# --- IDLE -----------------------------------------------------------------------


class SocketImap:
    """imaplib's send/readline/socket over a real socket, so select() works."""

    def __init__(self, client: socket.socket) -> None:
        self._client = client
        self._file = client.makefile("rb")
        self.sent: list[bytes] = []
        self.welcome = b"* OK"

    def send(self, data):
        self.sent.append(data)

    def readline(self):
        return self._file.readline()

    def socket(self):
        return self._client


@pytest.fixture
def idle_link(monkeypatch):
    client, server = socket.socketpair()
    wakeup, waker = socket.socketpair()
    imap = SocketImap(client)
    session = connect(monkeypatch, imap)  # type: ignore[arg-type]
    yield session, imap, server, wakeup, waker
    for end in (client, server, wakeup, waker):
        end.close()


def test_idle_reports_a_change_the_server_pushes(idle_link):
    session, imap, server, wakeup, _waker = idle_link
    server.sendall(b"+ idling\r\n")
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(session.wait_for_change(30, wakeup))
    )
    waiter.start()
    time.sleep(0.1)
    server.sendall(b"* OK still here\r\n")
    time.sleep(0.05)
    server.sendall(b"* 12 EXISTS\r\nPCIDLE OK IDLE terminated\r\n")
    waiter.join(5)

    assert result == [True]
    assert imap.sent == [b"PCIDLE IDLE\r\n", b"DONE\r\n"]


def test_idle_ends_quietly_when_nothing_changes(idle_link):
    session, _imap, server, wakeup, _waker = idle_link
    server.sendall(b"+ idling\r\nPCIDLE OK IDLE terminated\r\n")

    assert session.wait_for_change(0.1, wakeup) is False


def test_a_wakeup_stops_the_wait_at_once(idle_link):
    session, _imap, server, wakeup, waker = idle_link
    server.sendall(b"+ idling\r\nPCIDLE OK IDLE terminated\r\n")
    waker.sendall(b"x")

    started = time.monotonic()
    assert session.wait_for_change(30, wakeup) is False
    assert time.monotonic() - started < 5


def test_a_refused_idle_is_an_error(idle_link):
    session, _imap, server, wakeup, _waker = idle_link
    server.sendall(b"PCIDLE BAD unknown command\r\n")

    with pytest.raises(ImapError, match="IDLE refused"):
        session.wait_for_change(1, wakeup)


# --- category signals ---------------------------------------------------------


def test_the_categorizing_headers_come_back_with_the_row(monkeypatch):
    header_block = (
        b"From: GitHub <notifications@github.com>\r\n"
        b"Subject: [repo] PR\r\n"
        b"List-Unsubscribe: <https://github.com/u>\r\n"
        b"Precedence: list\r\n\r\n"
    )
    reply = ("OK", [(b"1 (UID 5 FLAGS () BODY[HEADER.FIELDS (X)] {10}", header_block)])
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    [header] = session.fetch_recent_headers(exists=1, limit=50)

    assert dict(header.signals) == {
        "list-unsubscribe": "<https://github.com/u>",
        "precedence": "list",
    }
    assert header.is_invitation is False


def test_a_calendar_part_in_the_body_slice_marks_an_invitation(monkeypatch):
    reply = (
        "OK",
        [
            (
                b"1 (UID 6 FLAGS () BODY[HEADER.FIELDS (X)] {10}",
                b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n',
            ),
            (
                b" BODY[TEXT]<0> {80}",
                b"--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
                b"--b\r\nContent-Type: text/calendar; method=REQUEST\r\n",
            ),
            b")",
        ],
    )
    session = connect(monkeypatch, FakeImap(fetch_reply=reply))

    [header] = session.fetch_recent_headers(exists=1, limit=50)

    assert header.is_invitation is True


def test_a_gmail_search_is_limited_to_the_uids_given(monkeypatch):
    imap = FakeImap(search_reply=("OK", [b"2"]))
    session = connect(monkeypatch, imap)

    assert session.search_gmail(["1", "2"], "category:updates") == {"2"}
    assert imap.calls == [("SEARCH", "UID", "1,2", "X-GM-RAW", '"category:updates"')]
    assert session.search_gmail([], "category:updates") == set()
