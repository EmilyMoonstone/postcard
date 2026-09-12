from datetime import date

from postcard.core.models.conversation import Conversation
from postcard.core.models.email import Email
from postcard.core.smart_inbox import (
    VIEW_CATEGORY,
    VIEW_DATE,
    VIEW_IMPORTANCE,
    Bundle,
    InboxSection,
    build,
    date_section,
    open_bundle,
)

TODAY = date(2026, 9, 12)  # a Saturday

_next_id = iter(range(1, 10_000))


def thread(day: str, **fields) -> Conversation:
    fields.setdefault("category", "people")
    fields.setdefault("sender", "Ada")
    fields.setdefault("is_unread", False)
    account = fields.pop("account", 1)
    mail = Email(
        id=next(_next_id),
        folder_id=account,
        server_id="1",
        subject=f"{fields['category']} {day}",
        preview="",
        date=f"{day}T10:00:00",
        **fields,
    )
    return Conversation([mail])


def account_of(conversation: Conversation) -> int:
    return conversation.latest.folder_id


def shape(rows) -> list[str]:
    """Rows as short strings: sections as '# key', bundles as '[key count]'."""
    out = []
    for row in rows:
        if isinstance(row, InboxSection):
            out.append(f"# {row.key}")
        elif isinstance(row, Bundle):
            out.append(f"[{row.key} {row.count}]")
        else:
            out.append(row.subject)
    return out


def test_date_sections_follow_the_calendar():
    assert date_section("2026-09-12T08:00:00", TODAY) == "today"
    assert date_section("2026-09-11T08:00:00", TODAY) == "yesterday"
    assert date_section("2026-09-08T08:00:00", TODAY) == "this-week"
    assert date_section("2026-09-01T08:00:00", TODAY) == "last-week"
    assert date_section("2026-09-01T08:00:00", date(2026, 9, 25)) == "this-month"
    assert date_section("2026-07-20T08:00:00", TODAY) == "month:2026-07"
    assert date_section("2025-12-24T08:00:00", TODAY) == "year:2025"


def test_importance_folds_categories_into_bundles_and_leaves_priority_in_place():
    rows = build(
        [
            thread("2026-09-12", category="newsletter"),
            thread("2026-09-10", is_starred=True, category="newsletter"),
            thread("2026-09-12"),
            thread("2026-09-11", category="newsletter"),
            thread("2026-09-11", category="notification"),
        ],
        VIEW_IMPORTANCE,
        account_of,
        set(),
        TODAY,
    )

    assert shape(rows) == [
        "# today",
        "[category:newsletter 2]",
        "people 2026-09-12",
        "# yesterday",
        "[category:notification 1]",
        "# this-week",
        "newsletter 2026-09-10",
    ]


def test_pinned_threads_sit_on_top_in_every_view():
    conversations = [
        thread("2026-09-12"),
        thread("2026-09-01", category="newsletter", is_pinned=True),
    ]

    for view in (VIEW_IMPORTANCE, VIEW_CATEGORY, VIEW_DATE):
        rows = build(conversations, view, account_of, set(), TODAY)
        assert shape(rows)[:2] == ["# pinned", "newsletter 2026-09-01"], view
        assert shape(rows).count("newsletter 2026-09-01") == 1


def test_a_bundled_account_takes_all_its_mail_whatever_the_category():
    rows = build(
        [
            thread("2026-09-12", account=2),
            thread("2026-09-12", account=2, category="newsletter"),
            thread("2026-09-12", account=1, category="newsletter"),
            thread("2026-09-12", account=2, is_starred=True),
        ],
        VIEW_IMPORTANCE,
        account_of,
        {2},
        TODAY,
    )

    assert shape(rows) == [
        "# today",
        "[account:2 2]",
        "[category:newsletter 1]",
        "people 2026-09-12",
    ]


def test_the_category_view_lists_every_thread_under_its_category():
    rows = build(
        [
            thread("2026-09-12", category="newsletter"),
            thread("2026-09-11"),
            thread("2026-09-10", account=2),
            thread("2026-09-09", category=""),
        ],
        VIEW_CATEGORY,
        account_of,
        {2},
        TODAY,
    )

    assert shape(rows) == [
        "# category:people",
        "people 2026-09-11",
        " 2026-09-09",
        "# category:newsletter",
        "newsletter 2026-09-12",
        "# account:2",
        "people 2026-09-10",
    ]


def test_the_date_view_has_no_bundles():
    rows = build(
        [
            thread("2026-09-11", category="newsletter", is_starred=True),
            thread("2026-09-12", category="newsletter"),
        ],
        VIEW_DATE,
        account_of,
        {1},
        TODAY,
    )

    assert shape(rows) == [
        "# today",
        "newsletter 2026-09-12",
        "# yesterday",
        "newsletter 2026-09-11",
    ]


def test_an_opened_bundle_lists_its_threads_by_date():
    conversations = [
        thread("2026-09-01", category="newsletter"),
        thread("2026-09-12", category="newsletter"),
        thread("2026-09-12"),
        thread("2026-09-11", category="newsletter", is_starred=True),
    ]

    rows = open_bundle(conversations, "category:newsletter", account_of, set(), TODAY)

    assert shape(rows) == [
        "# today",
        "newsletter 2026-09-12",
        "# last-week",
        "newsletter 2026-09-01",
    ]


def test_a_bundle_summarizes_its_senders_newest_first():
    bundle = Bundle("category:newsletter")
    bundle.conversations = [
        thread("2026-09-12", sender="ChatGPT", is_unread=True),
        thread("2026-09-11", sender="Pascal"),
        thread("2026-09-10", sender="ChatGPT"),
    ]

    senders = bundle.senders(limit=5)

    assert [(s.name, s.count, s.is_unread) for s in senders] == [
        ("ChatGPT", 2, True),
        ("Pascal", 1, False),
    ]
    assert bundle.is_unread
    assert bundle.date == "2026-09-12T10:00:00"
