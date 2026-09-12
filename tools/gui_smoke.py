"""Smoke-test the GTK layer: build the real window and poke what can crash it.

The unit tests can't reach window.py -- the Adw typelib only exists inside the
Flatpak -- and four crashes have shipped there, most from a background
callback firing into a window with no account. This runs inside the Flatpak
against a throwaway database, on GTK's Broadway backend so it needs no
display, and fails if any callback raises. It checks that nothing blows up,
not that anything looks right.

    just smoke
"""

import os
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

# Before GLib reads them, so the app's database and settings are throwaway.
_DATA_HOME = tempfile.mkdtemp(prefix="postcard-smoke-")
os.environ["XDG_DATA_HOME"] = _DATA_HOME
os.environ["GSETTINGS_BACKEND"] = "memory"
# A GTK critical is a bug waiting to crash -- a dangling widget reference once
# only showed as criticals until a later redraw segfaulted. Make them fatal.
os.environ["G_DEBUG"] = "fatal-criticals"

failures: list[str] = []


def _record(exc_type, exc, tb) -> None:
    failures.append("".join(traceback.format_exception(exc_type, exc, tb)))
    sys.__excepthook__(exc_type, exc, tb)


# PyGObject reports an exception raised in a signal handler or idle callback
# through sys.excepthook and carries on, so these are the only way to see one.
sys.excepthook = _record
threading.excepthook = lambda args: _record(
    args.exc_type, args.exc_value, args.exc_traceback
)

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk

Gio.Resource.load("/app/share/postcard/postcard.gresource")._register()

from postcard.application import PostcardApplication
from postcard.core import secrets
from postcard.core.models.account import PROTOCOL_GRAPH
from postcard.core.models.message_header import MessageHeader
from postcard.core.net.auth import Credential
from postcard.core.store.database import Database
from postcard.window import PostcardMainWindow

STEP_MS = 700

# A password for the IMAP account without touching the real keyring, so its
# operations get as far as the refused connection -- the path that queues
# actions. The Graph account still finds no Online Accounts entry.
secrets.credential_for = lambda account: (
    None if account.goa_id else Credential(account.login_name, "smoke")
)

INVITATION = (
    b"From: Ada <ada@example.com>\r\nTo: me@example.com\r\nSubject: Lunch\r\n"
    b"Message-ID: <lunch@example.com>\r\nMIME-Version: 1.0\r\n"
    b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
    b"--b\r\nContent-Type: text/plain\r\n\r\nLunch on Thursday?\r\n"
    b'--b\r\nContent-Type: text/calendar; charset="utf-8"; method=REQUEST\r\n\r\n'
    b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:lunch-1\r\n"
    b"SUMMARY:Lunch\r\nDTSTART:20260917T110000Z\r\nDTEND:20260917T120000Z\r\n"
    b"ORGANIZER;CN=Ada:mailto:ada@example.com\r\n"
    b"ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@example.com\r\n"
    b"END:VEVENT\r\nEND:VCALENDAR\r\n--b--\r\n"
)


def seed_accounts(path: Path) -> None:
    """One IMAP account nobody listens for, and one Graph account whose
    Online Accounts entry is gone -- the two ways a sync fails at once."""
    db = Database(str(path))
    imap = db.save_account(
        "me@example.com", "Me", "127.0.0.1", 9, "127.0.0.1", 9, username="me"
    )
    db.save_account(
        "ada@contoso.com",
        "Ada",
        "graph.microsoft.com",
        443,
        "graph.microsoft.com",
        443,
        goa_id="account_gone",
        protocol=PROTOCOL_GRAPH,
    )
    graph = db.accounts()[-1]
    inbox = db.get_or_create_folder(imap.id, "INBOX", "mail-unread-symbolic")
    db.get_or_create_folder(imap.id, "Archive", "mail-archive-symbolic")
    db.get_or_create_folder(imap.id, "Trash", "user-trash-symbolic")
    projects = db.get_or_create_folder(imap.id, "Projects")
    db.set_folder_parent(
        db.get_or_create_folder(imap.id, "Projects/2026").id, projects.id, "/"
    )
    graph_inbox = db.get_or_create_folder(graph.id, "AAMkInbox")
    db.set_folder_identity(graph_inbox.id, "inbox", "Posteingang")
    db.set_account_bundled(graph.id, True)
    for uid, subject, category in (
        ("1", "Lunch", "people"),
        ("2", "Invoice", "people"),
        ("3", "Weekly digest", "newsletter"),
        ("4", "Sign-in alert", "notification"),
    ):
        db.save_incoming_email(
            inbox.id,
            MessageHeader(
                uid=uid,
                sender="Ada",
                sender_address="ada@example.com",
                recipient="me@example.com",
                recipient_address="me@example.com",
                subject=subject,
                date="2026-09-12T10:00:00+00:00",
                is_unread=True,
                preview="Lunch on Thursday?",
                message_id=f"<{uid}@example.com>",
                category=category,
            ),
        )
        db.save_incoming_email(
            graph_inbox.id,
            MessageHeader(
                uid=f"g{uid}",
                sender="Grace",
                sender_address="grace@contoso.com",
                recipient="ada@contoso.com",
                recipient_address="ada@contoso.com",
                subject=f"Work {subject}",
                date="2026-09-11T10:00:00+00:00",
                is_unread=False,
                message_id=f"<g{uid}@contoso.com>",
                category=category,
            ),
        )
        db.save_raw_message(db.email_ids_for_server_ids(inbox.id, [uid])[0], INVITATION)
    db.reassign_conversations(inbox.id)
    db.reassign_conversations(graph_inbox.id)
    db.set_priority(db.email_ids_for_server_ids(inbox.id, ["2"]), True)
    db.close()


def window_of(app: PostcardApplication) -> PostcardMainWindow:
    return next(w for w in app.get_windows() if isinstance(w, PostcardMainWindow))


def poke_empty(app: PostcardApplication) -> list[Callable[[], object]]:
    """What background callbacks do to a window built on an empty database."""
    monitor = Gio.NetworkMonitor.get_default()
    return [
        lambda: window_of(app)._on_sync_tick(),
        lambda: window_of(app)._on_network_changed(monitor, False),
        lambda: window_of(app)._on_network_changed(monitor, True),
        lambda: window_of(app)._on_mail_arrived(1),
        lambda: window_of(app).open_email(1, "1"),
        lambda: app.activate_action("open-mail", GLib.Variant("(is)", (1, "1"))),
        lambda: window_of(app).activate_action("win.toggle-read", None),
        lambda: window_of(app).activate_action("win.compose", None),
        lambda: window_of(app).search_entry.set_text("lunch"),
        lambda: window_of(app)._on_search_timeout(),
        lambda: window_of(app).reload_accounts(),
    ]


def poke_accounts(app: PostcardApplication) -> list[Callable[[], object]]:
    """Reading, flagging, moving, searching and composing with accounts whose
    servers can't be reached."""
    monitor = Gio.NetworkMonitor.get_default()

    def select_first() -> None:
        win = window_of(app)
        store = win._conversation_store
        first = next(
            i
            for i in range(store.get_n_items())
            if type(store.get_item(i)).__name__ == "Conversation"
        )
        win._selection.select_item(first, True)

    def open_first_bundle() -> None:
        win = window_of(app)
        store = win._conversation_store
        bundle = next(
            store.get_item(i)
            for i in range(store.get_n_items())
            if type(store.get_item(i)).__name__ == "Bundle"
        )
        win._open_bundle(bundle.key)

    def open_dialogs() -> None:
        from postcard.account_dialog import PostcardAccountDialog
        from postcard.preferences_dialog import PostcardPreferencesDialog

        win = window_of(app)
        app.db.save_signature("Work", "Ada\nExample Ltd")
        add = PostcardAccountDialog(app.db)
        add.present(win)
        add.email_row.set_text("ada@example.org")
        add.hosting_row.set_selected(3)
        add.close()
        account = app.db.accounts()[0]
        edit = PostcardAccountDialog(app.db, account)
        edit.present(win)
        edit.remote_images_row.set_active(True)
        edit.signature_row.set_selected(1)
        edit._save()
        preferences = PostcardPreferencesDialog(app.settings, app.db)
        preferences.present(win)
        preferences.close()

    return [
        lambda: window_of(app)._sync_all(),
        lambda: window_of(app)._select_folder_by_id(-1),
        open_first_bundle,
        select_first,
        lambda: window_of(app)._on_bundle_back(None),
        lambda: window_of(app)._select_folder_by_id(-2),
        lambda: window_of(app)._select_folder_by_id(-6),
        lambda: window_of(app)._show_more_folders(window_of(app).folder_list),
        lambda: window_of(app)._show_other_folder(
            next(
                f
                for f in window_of(app)._folders_by_id.values()
                if f.name == "Projects/2026"
            )
        ),
        lambda: app.settings.set_boolean("show-account-display-name", True),
        lambda: window_of(app)._select_folder_by_id(-1),
        lambda: app.settings.set_string("inbox-view", "category"),
        lambda: app.settings.set_string("inbox-view", "date"),
        lambda: app.settings.set_string("inbox-view", "importance"),
        select_first,
        lambda: window_of(app)._on_toggle_priority(None, None),
        lambda: window_of(app)._on_toggle_pin(None, None),
        lambda: window_of(app)._on_set_category(
            None, GLib.Variant.new_string("newsletter")
        ),
        lambda: window_of(app).open_email(1, "3"),
        select_first,
        lambda: window_of(app)._on_toggle_star(None, None),
        lambda: window_of(app)._on_network_changed(monitor, False),
        lambda: window_of(app)._on_toggle_read(None, None),
        lambda: window_of(app)._on_archive(None, None),
        lambda: window_of(app)._commit_pending_moves(),
        lambda: window_of(app)._on_network_changed(monitor, True),
        lambda: window_of(app).search_entry.set_text("thursday"),
        lambda: window_of(app)._on_search_timeout(),
        lambda: window_of(app).search_entry.set_text(""),
        lambda: window_of(app)._on_compose_clicked(),
        open_dialogs,
        lambda: window_of(app)._on_mail_arrived(1),
        lambda: window_of(app)._on_refresh_clicked(),
        lambda: window_of(app).close(),
    ]


def run(poke: Callable[[PostcardApplication], list[Callable[[], object]]]) -> None:
    app = PostcardApplication("smoke")
    # Beside a Postcard the user has open, rather than handing it our activate.
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    steps: list[Callable[[], object]] = []

    def next_step() -> bool:
        if not steps:
            for window in app.get_windows():
                window.destroy()
            app.db.close()
            app.quit()
            return False
        try:
            steps.pop(0)()
        except Exception:
            _record(*sys.exc_info())
        return True

    def on_activate(_app: PostcardApplication) -> None:
        steps.extend(poke(app))
        GLib.timeout_add(STEP_MS, next_step)

    app.connect_after("activate", on_activate)
    app.run([])


def main() -> int:
    if Gtk.get_major_version() != 4:
        return 2
    run(poke_empty)
    # GLib settles the data directory once per process, so the second run
    # seeds the same database the first one created, still empty.
    seed_accounts(Path(GLib.get_user_data_dir()) / "postcard" / "postcard.db")
    run(poke_accounts)
    if failures:
        print(f"\n{len(failures)} exception(s) in the GTK layer", file=sys.stderr)
        return 1
    print("GTK smoke test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
