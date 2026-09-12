import threading

import pytest

import postcard.mail_watch as mail_watch
from postcard.core.models.account import Account
from postcard.core.net.auth import Credential


def graph_account(account_id: int = 7) -> Account:
    return Account(
        id=account_id,
        email="ada@contoso.com",
        display_name="Ada",
        imap_host="graph.microsoft.com",
        imap_port=443,
        smtp_host="graph.microsoft.com",
        smtp_port=443,
        protocol="graph",
    )


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(mail_watch, "GRAPH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(mail_watch, "RETRY_SECONDS", 0.01)
    monkeypatch.setattr(
        mail_watch.secrets,
        "credential_for",
        lambda account: Credential(account.email, "token", "xoauth2"),
    )


def test_a_graph_account_reports_only_a_change_in_the_inbox_counts(fast, monkeypatch):
    counts = iter([(10, 2), (10, 2), (11, 3), (11, 3)])
    changed = threading.Event()

    def next_counts(session):
        try:
            return next(counts)
        except StopIteration:
            changed.wait()
            return (11, 3)

    monkeypatch.setattr(mail_watch.graph_folders, "inbox_counts", next_counts)
    reported: list[int] = []

    def schedule(callback, account_id):
        reported.append(account_id)
        changed.set()

    watcher = mail_watch.MailWatcher(lambda _id: None, schedule=schedule)
    watcher.watch([graph_account()])
    assert changed.wait(5)
    watcher.stop_all()

    assert reported == [7]


def test_watch_stops_the_accounts_no_longer_given(fast, monkeypatch):
    monkeypatch.setattr(mail_watch.graph_folders, "inbox_counts", lambda s: (1, 1))
    watcher = mail_watch.MailWatcher(lambda _id: None, schedule=lambda *a: None)

    watcher.watch([graph_account(1), graph_account(2)])
    first = watcher._watches[1]
    watcher.watch([graph_account(2)])

    assert first.stop.is_set()
    assert set(watcher._watches) == {2}
    watcher.stop_all()
    assert watcher._watches == {}


def test_a_failing_watch_retries_instead_of_dying(fast, monkeypatch):
    calls = []
    retried = threading.Event()

    def flaky(session):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError
        retried.set()
        return (1, 1)

    monkeypatch.setattr(mail_watch.graph_folders, "inbox_counts", flaky)
    watcher = mail_watch.MailWatcher(lambda _id: None, schedule=lambda *a: None)

    watcher.watch([graph_account()])
    assert retried.wait(5)
    watcher.stop_all()
