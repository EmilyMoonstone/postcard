"""Noticing new mail as it arrives, instead of at the next sync tick.

One daemon thread per account. An IMAP account holds a connection of its own
open in IDLE on the inbox, so the server says when something changes; Graph
offers desktop apps no push, so a Graph account asks for the inbox counts once
a minute, which costs far less than a sync. Either way the thread only says
"this account changed" -- the window's usual sync does the fetching.
"""

import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import GLib

from .core import secrets
from .core.models.account import Account
from .core.net import errors, graph_folders
from .core.net.graph_session import GraphSession
from .core.net.imap_session import IDLE_CAPABILITY, ImapSession
from .mail_sync import inbox_name

logger = logging.getLogger(__name__)

# RFC 2177 has servers drop an IDLE after 30 minutes; re-entering well inside
# that also notices a connection that died without saying so.
IDLE_SECONDS = 9 * 60

GRAPH_POLL_SECONDS = 60

# After a failure: long enough not to hammer a server that is down, short
# enough that a blip costs only a minute of push.
RETRY_SECONDS = 60

# Returns whatever GLib.idle_add wants back from its callback.
ChangeCallback = Callable[[int], object]
Schedule = Callable[..., object]


@dataclass
class _Watch:
    stop: threading.Event = field(default_factory=threading.Event)
    # Written to on stop, so a thread waiting in select() wakes at once.
    wakeup: tuple[socket.socket, socket.socket] = field(
        default_factory=socket.socketpair
    )

    def close(self) -> None:
        self.stop.set()
        try:
            self.wakeup[1].send(b"x")
        except OSError:
            logger.debug("wakeup socket already closed", exc_info=True)


class MailWatcher:
    """Keeps one watch thread running per account it is given.

    Main thread only: watch() and stop_all() are called from the window, and
    on_change is delivered back onto the main loop through `schedule`.
    """

    def __init__(
        self, on_change: ChangeCallback, schedule: Schedule = GLib.idle_add
    ) -> None:
        self._on_change = on_change
        self._schedule = schedule
        self._watches: dict[int, _Watch] = {}

    def watch(self, accounts: list[Account]) -> None:
        """Watch exactly these accounts: start the new, stop the gone."""
        wanted = {account.id: account for account in accounts}
        for account_id in set(self._watches) - set(wanted):
            self._watches.pop(account_id).close()
        for account_id, account in wanted.items():
            if account_id in self._watches:
                continue
            watch = _Watch()
            self._watches[account_id] = watch
            threading.Thread(
                target=self._run, args=(account, watch), daemon=True
            ).start()

    def stop_all(self) -> None:
        for watch in self._watches.values():
            watch.close()
        self._watches.clear()

    # Runs on the watch thread: network only, no Gtk/database access.
    def _run(self, account: Account, watch: _Watch) -> None:
        try:
            while not watch.stop.is_set():
                try:
                    if account.is_graph:
                        self._poll_graph(account, watch)
                    else:
                        self._idle_imap(account, watch)
                except Exception as error:
                    level = (
                        logging.DEBUG
                        if errors.is_connectivity(error)
                        else logging.WARNING
                    )
                    logger.log(
                        level,
                        "watching %s for new mail failed; retrying",
                        account.email,
                        exc_info=True,
                    )
                watch.stop.wait(RETRY_SECONDS)
        finally:
            for end in watch.wakeup:
                end.close()

    def _changed(self, account: Account) -> None:
        self._schedule(self._on_change, account.id)

    def _idle_imap(self, account: Account, watch: _Watch) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            return
        session = ImapSession(
            account.imap_host, account.imap_port, account.imap_security
        )
        session.connect()
        try:
            session.sign_in(credential)
            session.refresh_capabilities()
            if not session.has_capability(IDLE_CAPABILITY):
                logger.debug("%s has no IDLE; the sync timer covers it", account.email)
                watch.stop.wait()
                return
            session.select(inbox_name([box.name for box in session.list_folders()]))
            while not watch.stop.is_set():
                if session.wait_for_change(IDLE_SECONDS, watch.wakeup[0]):
                    self._changed(account)
        finally:
            session.logout()

    def _poll_graph(self, account: Account, watch: _Watch) -> None:
        last: tuple[int, int] | None = None
        while not watch.stop.wait(0 if last is None else GRAPH_POLL_SECONDS):
            credential = secrets.credential_for(account)
            if credential is None:
                return
            counts = graph_folders.inbox_counts(GraphSession(credential))
            if last is not None and counts != last:
                self._changed(account)
            last = counts
