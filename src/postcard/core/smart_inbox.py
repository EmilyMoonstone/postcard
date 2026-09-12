"""The rows of a smart inbox: sections, bundles and conversations.

Pure layout, no widgets: given the conversations a folder view shows, decide
what goes in the Pinned section, which mail folds into a bundle row
("Newsletter 36", or a whole account the user chose to see as one), and where
the date sections break. The window puts the result in its list store as is.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from gi.repository import GObject

from .categories import (
    CATEGORY_INVITATION,
    CATEGORY_NEWSLETTER,
    CATEGORY_NOTIFICATION,
    CATEGORY_PEOPLE,
)
from .models.conversation import Conversation

VIEW_IMPORTANCE = "importance"
VIEW_CATEGORY = "category"
VIEW_DATE = "date"
VIEWS = (VIEW_IMPORTANCE, VIEW_CATEGORY, VIEW_DATE)

SECTION_PINNED = "pinned"
SECTION_TODAY = "today"
SECTION_YESTERDAY = "yesterday"
SECTION_THIS_WEEK = "this-week"
SECTION_LAST_WEEK = "last-week"
SECTION_THIS_MONTH = "this-month"
# Older sections carry their period: "month:2026-08", "year:2025".
SECTION_MONTH = "month"
SECTION_YEAR = "year"

# The categories that fold into a bundle; people stay one row each.
BUNDLED_CATEGORIES = (CATEGORY_INVITATION, CATEGORY_NOTIFICATION, CATEGORY_NEWSLETTER)

# The Category view's order, after Priority.
CATEGORY_ORDER = (CATEGORY_PEOPLE, *BUNDLED_CATEGORIES)

BUNDLE_CATEGORY = "category"
BUNDLE_ACCOUNT = "account"

AccountOf = Callable[[Conversation], int | None]


class InboxSection(GObject.Object):
    __gtype_name__ = "PostcardInboxSection"

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key: str = key


class Bundle(GObject.Object):
    """Many conversations shown as one row, opened to list them."""

    __gtype_name__ = "PostcardBundle"

    def __init__(self, key: str) -> None:
        super().__init__()
        # The kind and what it bundles, such as category:newsletter or account:3.
        self.key: str = key
        self.conversations: list[Conversation] = []

    @property
    def kind(self) -> str:
        return self.key.partition(":")[0]

    @property
    def value(self) -> str:
        return self.key.partition(":")[2]

    @property
    def count(self) -> int:
        return len(self.conversations)

    @property
    def is_unread(self) -> bool:
        return any(conversation.is_unread for conversation in self.conversations)

    @property
    def date(self) -> str:
        return self.conversations[0].date if self.conversations else ""

    def senders(self, limit: int) -> list["BundleSender"]:
        """The newest distinct senders, each with how many threads they have."""
        by_name: dict[str, BundleSender] = {}
        for conversation in self.conversations:
            latest = conversation.latest
            name = latest.sender or latest.sender_address
            sender = by_name.get(name)
            if sender is None:
                by_name[name] = BundleSender(
                    name, latest.sender_address, 1, conversation.is_unread
                )
            else:
                by_name[name] = BundleSender(
                    name,
                    sender.address,
                    sender.count + 1,
                    sender.is_unread or conversation.is_unread,
                )
        return list(by_name.values())[:limit]


@dataclass(frozen=True, slots=True)
class BundleSender:
    name: str
    address: str
    count: int
    is_unread: bool


def timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).astimezone().timestamp()
    except (TypeError, ValueError):
        return 0.0


def date_section(value: str, today: date) -> str:  # noqa: PLR0911
    # noqa PLR0911: one return per section, in calendar order, reads best.
    """Which date section a timestamp falls in, relative to today."""
    stamp = timestamp(value)
    if not stamp:
        return f"{SECTION_YEAR}:0"
    day = datetime.fromtimestamp(stamp).date()
    if day >= today:
        return SECTION_TODAY
    if day == today - timedelta(days=1):
        return SECTION_YESTERDAY
    week_start = today - timedelta(days=today.weekday())
    if day >= week_start:
        return SECTION_THIS_WEEK
    if day >= week_start - timedelta(days=7):
        return SECTION_LAST_WEEK
    if (day.year, day.month) == (today.year, today.month):
        return SECTION_THIS_MONTH
    if day.year == today.year:
        return f"{SECTION_MONTH}:{day:%Y-%m}"
    return f"{SECTION_YEAR}:{day:%Y}"


def bundle_key(
    conversation: Conversation, account_of: AccountOf, bundled_accounts: set[int]
) -> str | None:
    """The bundle a conversation folds into, or None to list it on its own.

    A bundled account takes all its mail, whatever the category: that is what
    choosing to see the account as one row means. A pinned or priority thread
    never folds away: the user marked it to be seen.
    """
    if conversation.is_pinned or conversation.is_priority:
        return None
    account_id = account_of(conversation)
    if account_id is not None and account_id in bundled_accounts:
        return f"{BUNDLE_ACCOUNT}:{account_id}"
    if conversation.category in BUNDLED_CATEGORIES:
        return f"{BUNDLE_CATEGORY}:{conversation.category}"
    return None


def build(
    conversations: Iterable[Conversation],
    view: str,
    account_of: AccountOf,
    bundled_accounts: set[int],
    today: date,
) -> list[GObject.Object]:
    """The rows of an inbox in one of the three views, pinned threads on top."""
    ordered = sorted(conversations, key=lambda c: timestamp(c.date), reverse=True)
    pinned = [conversation for conversation in ordered if conversation.is_pinned]
    rest = [conversation for conversation in ordered if not conversation.is_pinned]
    rows: list[GObject.Object] = []
    if pinned:
        rows.append(InboxSection(SECTION_PINNED))
        rows.extend(pinned)

    if view == VIEW_DATE:
        rows.extend(_by_date(rest, today))
    elif view == VIEW_CATEGORY:
        rows.extend(_by_category(rest, account_of, bundled_accounts))
    else:
        rows.extend(_with_bundles(rest, account_of, bundled_accounts, today))
    return rows


def open_bundle(
    conversations: Iterable[Conversation],
    key: str,
    account_of: AccountOf,
    bundled_accounts: set[int],
    today: date,
) -> list[GObject.Object]:
    """The rows of one opened bundle: its conversations, by date."""
    members = [
        conversation
        for conversation in conversations
        if bundle_key(conversation, account_of, bundled_accounts) == key
    ]
    members.sort(key=lambda c: timestamp(c.date), reverse=True)
    return _by_date(members, today)


def _by_date(ordered: list[Conversation], today: date) -> list[GObject.Object]:
    rows: list[GObject.Object] = []
    current = None
    for conversation in ordered:
        section = date_section(conversation.date, today)
        if section != current:
            rows.append(InboxSection(section))
            current = section
        rows.append(conversation)
    return rows


def _with_bundles(
    ordered: list[Conversation],
    account_of: AccountOf,
    bundled_accounts: set[int],
    today: date,
) -> list[GObject.Object]:
    """Date sections where bundled mail appears once, at its newest thread."""
    bundles: dict[str, Bundle] = {}
    items: list[GObject.Object] = []
    for conversation in ordered:
        key = bundle_key(conversation, account_of, bundled_accounts)
        if key is None:
            items.append(conversation)
            continue
        bundle = bundles.get(key)
        if bundle is None:
            bundle = bundles[key] = Bundle(key)
            items.append(bundle)
        bundle.conversations.append(conversation)

    rows: list[GObject.Object] = []
    current = None
    for item in items:
        stamp = item.date if isinstance(item, Bundle | Conversation) else ""
        section = date_section(stamp, today)
        if section != current:
            rows.append(InboxSection(section))
            current = section
        rows.append(item)
    return rows


def _by_category(
    ordered: list[Conversation], account_of: AccountOf, bundled_accounts: set[int]
) -> list[GObject.Object]:
    """One section per category, then one per bundled account."""
    sections: dict[str, list[Conversation]] = {}
    for conversation in ordered:
        account_id = account_of(conversation)
        if account_id is not None and account_id in bundled_accounts:
            key = f"{BUNDLE_ACCOUNT}:{account_id}"
        else:
            key = f"{BUNDLE_CATEGORY}:{conversation.category or CATEGORY_PEOPLE}"
        sections.setdefault(key, []).append(conversation)

    order = [f"{BUNDLE_CATEGORY}:{category}" for category in CATEGORY_ORDER]
    order += sorted(key for key in sections if key.startswith(BUNDLE_ACCOUNT))
    rows: list[GObject.Object] = []
    for key in order:
        if key in sections:
            rows.append(InboxSection(key))
            rows.extend(sections[key])
    return rows
