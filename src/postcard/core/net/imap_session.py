import base64
import email
import imaplib
import logging
import re
import select
import socket
from email import policy
from typing import NamedTuple

from ..mime.preview import PREVIEW_BYTES, preview_text
from ..models.folder import FolderRole

# Re-exported (the "as" form): MailboxInfo is what list_folders returns, and
# lives in core.models only so the Graph backend can build one too.
from ..models.mailbox import MailboxInfo as MailboxInfo
from . import NET_TIMEOUT_SECONDS, ssl_context_for
from .auth import MECHANISM_LOGIN, MECHANISM_XOAUTH2, Credential, xoauth2_response

logger = logging.getLogger(__name__)

# imaplib returns the command status as the first element of every reply.
STATUS_OK = "OK"

# IMAP system flags (RFC 3501 2.3.2). These are the protocol contract: the same
# spellings are parsed out of a FETCH reply here and sent back by store_flags,
# so both halves have to name them from one place.
FLAG_SEEN = "\\Seen"
FLAG_FLAGGED = "\\Flagged"
FLAG_DRAFT = "\\Draft"

# A LIST attribute, not a message flag: the mailbox is a container that cannot
# hold mail (Gmail's "[Gmail]"), so it is shown but never selected.
ATTR_NOSELECT = "\\Noselect"

# RFC 6154 SPECIAL-USE attributes, which a server puts on LIST replies to say
# what a mailbox is for whatever it is called -- the reliable answer for a
# "Gesendete Objekte". \All is Gmail's All Mail, which the app has always
# treated as the archive.
SPECIAL_USE_ROLES: dict[str, FolderRole] = {
    "\\sent": FolderRole.SENT,
    "\\drafts": FolderRole.DRAFTS,
    "\\trash": FolderRole.TRASH,
    "\\junk": FolderRole.JUNK,
    "\\archive": FolderRole.ARCHIVE,
    "\\all": FolderRole.ARCHIVE,
    "\\flagged": FolderRole.STARRED,
}


def special_use_role(flags: str) -> str:
    """The FolderRole a LIST reply's attributes state, or "" when none does."""
    for attribute in flags.split():
        role = SPECIAL_USE_ROLES.get(attribute.lower())
        if role is not None:
            return role
    return ""


# Advertised by servers that can push changes to an open mailbox (RFC 2177).
IDLE_CAPABILITY = "IDLE"

# The tag of the one command imaplib doesn't know how to send for us.
_IDLE_TAG = b"PCIDLE"

# Untagged replies that mean the selected mailbox changed. RECENT is left
# out: a server sends it beside EXISTS, never alone.
_MAILBOX_CHANGE = re.compile(rb"^\* \d+ (EXISTS|EXPUNGE|FETCH)\b", re.IGNORECASE)


# Gmail files its own copy of everything sent through it. This capability is how
# it identifies itself, so we don't append a second copy on top.
GMAIL_CAPABILITY = "X-GM-EXT-1"


class FetchedHeader(NamedTuple):
    """One message's headers exactly as the server sent them.

    Raw on purpose: addresses are unparsed header text and `date` is the
    original RFC 5322 string. mail_sync turns this into a MessageHeader, which
    is the display-ready form.
    """

    uid: str
    from_header: str
    to_header: str
    cc_header: str
    subject: str
    date: str
    message_id: str
    in_reply_to: str
    references: str
    seen: bool
    flagged: bool
    preview: str = ""


# The headers a sync reads. Content-Type and Content-Transfer-Encoding are only
# there to decode the body slice fetched beside them into a preview.
_HEADER_FIELDS = (
    "DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES "
    "CONTENT-TYPE CONTENT-TRANSFER-ENCODING"
)

# A FETCH reply's first line for a message starts with its sequence number;
# the lines for its later literals start with a space.
_MESSAGE_START = re.compile(r"^\d+ \(")


def fetch_items(payload: list) -> list[tuple[str, bytes, bytes]]:
    """Group imaplib's flat FETCH reply into (metadata, header, body) per message.

    imaplib hands back one (meta, literal) tuple per literal, so a message
    fetched with both a header and a body slice spans two tuples, and any text
    after the last literal (some servers put FLAGS there) comes as plain bytes.
    Which literal is which is read from the item name before it, since servers
    don't all answer in the order the items were asked for.
    """
    messages: list[list] = []
    for item in payload:
        if isinstance(item, bytes):
            if messages:
                messages[-1][0] += item.decode("utf-8", "replace")
            continue
        if not isinstance(item, tuple):
            continue
        meta = item[0].decode("utf-8", "replace")
        if _MESSAGE_START.match(meta) or not messages:
            messages.append(["", b"", b""])
        current = messages[-1]
        current[0] += meta
        item_name = meta[meta.upper().rfind("BODY[") :].upper()
        slot = 2 if item_name.startswith("BODY[TEXT]") else 1
        current[slot] = item[1]
    return [(meta, header, body) for meta, header, body in messages]


def decode_mailbox_name(name: str) -> str:
    """Decode a mailbox name from modified UTF-7 (RFC 3501 5.1.3), so
    "Entw&APw-rfe" reads as "Entwürfe"."""

    def chunk(match: re.Match[str]) -> str:
        if not match.group(1):
            return "&"  # "&-" encodes a literal ampersand
        try:
            padded = match.group(1).replace(",", "/") + "==="
            return base64.b64decode(padded).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return "�"

    return re.sub(r"&([A-Za-z0-9+,]*)-", chunk, name)


def _quote_mailbox(name: str) -> str:
    # imaplib sends mailbox names verbatim, so "[Gmail]/Sent Mail" arrives as
    # two tokens and the server answers BAD. Quote it (escaping \ and ") so the
    # space stays inside one astring, per RFC 3501.
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(token: str) -> str:
    """The inverse of _quote_mailbox: drop the quotes and backslash escapes."""
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    return re.sub(r"\\(.)", r"\1", token)


class ImapError(Exception):
    """Raised when talking to the server fails (bad login, dropped link, ..)"""


class ImapSession:
    def __init__(self, host: str, port: int, security: str = "tls") -> None:
        self._host = host
        self._port = port
        self._security = security
        self._imap: imaplib.IMAP4 | None = None

    def connect(self) -> str:
        context = ssl_context_for(self._host)
        if self._security == "starttls":
            self._imap = imaplib.IMAP4(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS
            )
            self._imap.starttls(context)
        else:
            self._imap = imaplib.IMAP4_SSL(
                self._host,
                self._port,
                ssl_context=context,
                timeout=NET_TIMEOUT_SECONDS,
            )
        return self._imap.welcome.decode("utf-8", "replace")

    def sign_in(self, credential: Credential) -> None:
        imap = self._require_imap()
        try:
            if credential.mechanism == MECHANISM_XOAUTH2:
                # imaplib base64-encodes whatever the callback returns.
                imap.authenticate(
                    "XOAUTH2",
                    lambda _challenge: xoauth2_response(
                        credential.user, credential.secret
                    ).encode(),
                )
            elif credential.mechanism == MECHANISM_LOGIN:
                imap.login(credential.user, credential.secret)
            else:
                # Falling through would leave the session unauthenticated and
                # surface as a puzzling SELECT failure instead of a login one.
                raise ImapError(f"unsupported mechanism {credential.mechanism}")
        except imaplib.IMAP4.error as error:
            raise ImapError(str(error)) from error

    def is_alive(self) -> bool:
        # A server that hung up on an idle connection says nothing about it
        # until the next command, so probe before reusing one.
        try:
            status, _payload = self._require_imap().noop()
        except (OSError, imaplib.IMAP4.error):
            return False
        return status == STATUS_OK

    def logout(self) -> None:
        # Runs from a `finally:` on every operation, so it must not raise and
        # mask the error that is already on its way out. Logged at debug
        # because a server hanging up first is normal, not a problem.
        try:
            if self._imap is not None:
                self._imap.logout()
        except Exception:
            logger.debug("IMAP logout from %s failed", self._host, exc_info=True)

    def _require_imap(self) -> imaplib.IMAP4:
        # Never return None: a caller that skipped connect() has to fail loudly
        # rather than quietly do nothing and look like it succeeded.
        if self._imap is None:
            raise ImapError(f"not connected to {self._host}:{self._port}")
        return self._imap

    def wait_for_change(self, seconds: float, wakeup: socket.socket) -> bool:
        """Wait in IDLE until the selected mailbox changes; True if it did.

        Returns False once `seconds` pass, or as soon as anything is written
        to `wakeup` -- which is how another thread stops the wait without
        touching this connection. imaplib has no IDLE before Python 3.14, so
        the command is spoken by hand; select() on the socket rather than a
        socket timeout, because a timed-out read leaves imaplib's buffered
        reader unusable. A reply the reader had already buffered is only seen
        at DONE, so a change can be reported up to `seconds` late -- in
        practice servers send the continuation on its own.
        """
        imap = self._require_imap()
        imap.send(_IDLE_TAG + b" IDLE\r\n")
        continuation = imap.readline()
        if not continuation.startswith(b"+"):
            raise ImapError(f"IDLE refused: {continuation!r}")

        is_changed = False
        connection = imap.socket()
        pending = getattr(connection, "pending", lambda: 0)
        while not is_changed:
            if not pending():
                readable, _, _ = select.select([connection, wakeup], [], [], seconds)
                if connection not in readable:
                    break
            line = imap.readline()
            if not line:
                raise ImapError(f"{self._host} closed the connection during IDLE")
            is_changed = bool(_MAILBOX_CHANGE.match(line))

        imap.send(b"DONE\r\n")
        while True:
            line = imap.readline()
            if not line:
                raise ImapError(f"{self._host} closed the connection ending IDLE")
            if line.startswith(_IDLE_TAG):
                return is_changed or bool(_MAILBOX_CHANGE.match(line))
            is_changed = is_changed or bool(_MAILBOX_CHANGE.match(line))

    def list_folders(self) -> list[MailboxInfo]:
        """Return every listed mailbox.

        \\Noselect containers are included so the caller can rebuild the
        hierarchy. The delimiter is "" when the server reports NIL, meaning a
        flat namespace whose names must not be split into parent and child.
        """
        status, payload = self._require_imap().list()
        result: list[MailboxInfo] = []
        for raw in payload:
            if not isinstance(raw, bytes):
                continue
            line = raw.decode("utf-8", "replace")
            match = re.match(r'\(([^)]*)\) ("[^"]*"|NIL) (.+)$', line)
            if match is None:
                continue
            flags_part = match.group(1)
            delim_raw = match.group(2)
            name = _unquote(match.group(3).strip())
            delimiter = "" if delim_raw == "NIL" else _unquote(delim_raw)
            result.append(
                MailboxInfo(
                    name, delimiter, flags_part, role=special_use_role(flags_part)
                )
            )
        return result

    def select(self, mailbox: str, is_readonly: bool = True) -> int:
        """Open a mailbox; return how many messages it holds.

        is_readonly=True (the default) keeps us non-destructive and never marks
        mail as read. Flag/move actions open it writable.
        """
        status, payload = self._require_imap().select(
            _quote_mailbox(mailbox), readonly=is_readonly
        )
        if status != STATUS_OK:
            raise ImapError(f"could not open {mailbox}: {payload}")
        return int(payload[0]) if payload and payload[0] else 0

    def unseen_count(self, mailbox: str) -> int:
        """How many unread messages a mailbox holds.

        STATUS rather than SELECT + SEARCH UNSEEN: one command, and it leaves
        the currently selected mailbox alone.
        """
        try:
            status, payload = self._require_imap().status(
                _quote_mailbox(mailbox), "(UNSEEN)"
            )
        except imaplib.IMAP4.abort:
            # A dropped connection, not one mailbox refusing: it has to end the
            # sync rather than be skipped once per remaining folder.
            raise
        except imaplib.IMAP4.error as error:
            raise ImapError(
                f"could not read the status of {mailbox}: {error}"
            ) from error
        if status != STATUS_OK:
            raise ImapError(f"could not read the status of {mailbox}: {payload}")
        first = payload[0] if payload else None
        match = (
            re.search(rb"UNSEEN\s+(\d+)", first) if isinstance(first, bytes) else None
        )
        if match is None:
            raise ImapError(f"no UNSEEN in the status of {mailbox}: {payload}")
        return int(match.group(1))

    def refresh_capabilities(self) -> None:
        """Ask again after signing in: a server may only offer some then, and
        imaplib keeps what it heard in the greeting."""
        imap = self._require_imap()
        status, payload = imap.capability()
        if status == STATUS_OK and payload and isinstance(payload[0], bytes):
            imap.capabilities = tuple(
                payload[0].decode("ascii", "replace").upper().split()
            )

    def has_capability(self, name: str) -> bool:
        """Whether the server advertises a capability. imaplib upper-cases the
        ones it parsed, so the comparison has to as well."""
        return name.upper() in self._require_imap().capabilities

    def append(self, mailbox: str, raw: bytes, flags: str = FLAG_SEEN) -> None:
        """Upload a message into a mailbox, without selecting it first.

        Stored \\Seen by default: it is our own copy of something we sent or
        wrote, and arriving as unread mail would be wrong.
        """
        status, payload = self._require_imap().append(
            _quote_mailbox(mailbox), flags, None, raw
        )
        if status != STATUS_OK:
            raise ImapError(f"could not append to {mailbox}: {payload}")

    def store_flags(self, uids: str, flags: str, should_add: bool) -> None:
        """Add or remove flags (e.g. "\\Seen") on a UID set: "7" or "7,9,20"."""
        command = "+FLAGS" if should_add else "-FLAGS"
        status, payload = self._require_imap().uid("STORE", uids, command, f"({flags})")
        if status != STATUS_OK:
            raise ImapError(f"could not update flags on {uids}: {payload}")

    def search_all_uids(self) -> set[str]:
        """Return every UID in the currently selected mailbox."""
        return self._uid_search("ALL")

    def search_text(self, query: str) -> set[str]:
        """UIDs in the selected mailbox whose headers or body contain query.

        Sent as a UTF-8 literal, so quotes, backslashes and umlauts in what the
        user typed need no escaping and can't break out of the command.
        """
        imap = self._require_imap()
        # typeshed says str, but imaplib writes the literal to the socket as is,
        # and only bytes go through.
        imap.literal = query.encode("utf-8")  # pyright: ignore[reportAttributeAccessIssue]
        return self._uid_search("CHARSET", "UTF-8", "TEXT")

    def _uid_search(self, *criteria: str) -> set[str]:
        try:
            status, payload = self._require_imap().uid("SEARCH", *criteria)
        except imaplib.IMAP4.error as error:
            raise ImapError(f"search failed: {error}") from error

        if status != STATUS_OK:
            raise ImapError(f"search failed: {payload}")
        if not isinstance(payload, (list, tuple)):
            raise ImapError(
                f"search returned {type(payload).__name__}, not a list: {payload}"
            )

        tokens: list[bytes] = []
        for item in payload:
            if not isinstance(item, bytes):
                raise ImapError(f"search returned a non-bytes item: {item!r}")
            tokens.extend(item.split())

        try:
            uids = {token.decode("ascii") for token in tokens}
        except UnicodeDecodeError as error:
            raise ImapError(f"search returned non-ASCII UIDs: {payload}") from error
        non_numeric = sorted(uid for uid in uids if not uid.isdigit())
        if non_numeric:
            raise ImapError(f"search returned non-numeric UIDs: {non_numeric}")
        return uids

    def move(self, uid: str, destination: str) -> str | None:
        """Move one message and return its destination UID when reported.

        COPYUID is the response code used by most servers; MOVEUID is used by
        some servers implementing RFC 6851.  ``response`` is imaplib's public
        response-code API and must be queried immediately after the command.
        """
        status, payload = self._require_imap().uid(
            "MOVE", uid, _quote_mailbox(destination)
        )
        if status != STATUS_OK:
            raise ImapError(f"could not move {uid} to {destination}: {payload}")
        imap = self._require_imap()
        for code in ("COPYUID", "MOVEUID"):
            _status, response = imap.response(code)
            destination_uid = self._destination_uid(response)
            if destination_uid is not None:
                return destination_uid
        return None

    @staticmethod
    def _destination_uid(response: object) -> str | None:
        """Extract a single destination UID from a COPYUID/MOVEUID response."""
        values = response if isinstance(response, (list, tuple)) else [response]
        text = " ".join(
            value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
            for value in values
            if value is not None
        )
        match = re.search(r"\b\d+\s+\d+(?::\d+)?\s+(\d+)(?::\d+)?\b", text)
        return match.group(1) if match else None

    def fetch_recent_headers(
        self, exists: int, limit: int, offset: int = 0
    ) -> list[FetchedHeader]:
        """Fetch UID + flags + a few headers for a window of `limit` messages,
        `offset` messages back from the newest. offset=0 is the newest page;
        offset=50 is the 50 before that, and so on (used for load-on-scroll)."""
        if exists == 0:
            return []

        end = exists - offset
        if end < 1:
            return []
        start = max(1, end - limit + 1)  # exists=1000,limit=50,offset=50 -> 901:950
        status, payload = self._require_imap().fetch(
            f"{start}:{end}",
            # BODY.PEEK[...] = look WITHOUT marking the message \Seen. The
            # partial TEXT is only the first bytes, for the preview line.
            f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})] "
            f"BODY.PEEK[TEXT]<0.{PREVIEW_BYTES}>)",
        )
        if status != STATUS_OK:
            raise ImapError(f"fetch failed: {payload}")
        return [
            self._parse(meta, header_bytes, body)
            for meta, header_bytes, body in fetch_items(payload)
        ]

    def fetch_headers_by_uid(self, uids: list[str]) -> list[FetchedHeader]:
        """The same rows as fetch_recent_headers, for a chosen set of UIDs."""
        if not uids:
            return []
        status, payload = self._require_imap().uid(
            "FETCH",
            ",".join(uids),
            f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})] "
            f"BODY.PEEK[TEXT]<0.{PREVIEW_BYTES}>)",
        )
        if status != STATUS_OK:
            raise ImapError(f"fetch failed: {payload}")
        return [
            self._parse(meta, header_bytes, body)
            for meta, header_bytes, body in fetch_items(payload)
        ]

    def fetch_message(self, uid: str) -> bytes:
        """Fetch one full message (headers + body) by its stable UID.

        Does not mark it seen.
        """
        status, payload = self._require_imap().uid("fetch", uid, "(BODY.PEEK[])")
        if status != STATUS_OK:
            raise ImapError(f"could not fetch message {uid}: {payload}")

        for item in payload:
            if isinstance(item, tuple):
                return item[1]

        raise ImapError(f"no message body returned for uid {uid}")

    def _parse(self, meta: str, header_bytes: bytes, body: bytes) -> FetchedHeader:
        uid = re.search(r"UID (\d+)", meta)
        flags = re.search(r"FLAGS \(([^)]*)\)", meta)
        flag_text = flags.group(1) if flags else ""

        # Let the stdlib decode the header block: it handles line folding and
        # the =?utf-8?...?= encoding you'd otherwise see as gibberish. Full MIME
        # body parsing with GMime comes in Phase 6 — this is just three headers.
        headers = email.message_from_bytes(header_bytes, policy=policy.default)

        def header(name: str) -> str:
            value = headers[name]
            return str(value) if value else ""

        return FetchedHeader(
            uid=uid.group(1) if uid else "",
            from_header=header("From"),
            to_header=header("To"),
            cc_header=header("Cc"),
            subject=header("Subject"),
            date=header("Date"),
            message_id=header("Message-ID"),
            in_reply_to=header("In-Reply-To"),
            references=header("References"),
            seen=FLAG_SEEN in flag_text,
            flagged=FLAG_FLAGGED in flag_text,
            preview=preview_text(header_bytes, body) if body else "",
        )
