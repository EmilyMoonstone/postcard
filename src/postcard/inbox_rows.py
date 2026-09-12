"""The widgets of a smart inbox list: section headings, bundles, conversations.

One InboxItemRow per list item, holding all three kinds and showing the one
its item needs -- the list recycles rows across kinds as it scrolls, so a row
can't be built for one kind only.
"""

from collections.abc import Callable
from datetime import date
from gettext import gettext as _
from html import escape

from gi.repository import Adw, Gtk, Pango

from .avatar_loader import AvatarLoader
from .conversation_row import ConversationRow
from .core.categories import (
    CATEGORY_INVITATION,
    CATEGORY_NEWSLETTER,
    CATEGORY_NOTIFICATION,
    CATEGORY_PEOPLE,
)
from .core.models.conversation import Conversation
from .core.smart_inbox import (
    BUNDLE_ACCOUNT,
    SECTION_LAST_WEEK,
    SECTION_MONTH,
    SECTION_PRIORITY,
    SECTION_THIS_MONTH,
    SECTION_THIS_WEEK,
    SECTION_TODAY,
    SECTION_YEAR,
    SECTION_YESTERDAY,
    Bundle,
    InboxSection,
)

# How many senders a bundle row names before "+N".
BUNDLE_SENDERS = 4

CATEGORY_ICONS = {
    CATEGORY_PEOPLE: "system-users-symbolic",
    CATEGORY_INVITATION: "x-office-calendar-symbolic",
    CATEGORY_NOTIFICATION: "preferences-system-notifications-symbolic",
    CATEGORY_NEWSLETTER: "view-paged-symbolic",
}
ACCOUNT_ICON = "avatar-default-symbolic"

AccountLabel = Callable[[int], str]


def category_label(category: str) -> str:
    return {
        CATEGORY_PEOPLE: _("People"),
        CATEGORY_INVITATION: _("Invitations"),
        CATEGORY_NOTIFICATION: _("Notifications"),
        CATEGORY_NEWSLETTER: _("Newsletters"),
    }.get(category, _("People"))


def bundle_label(key: str, account_label: AccountLabel) -> str:
    """What a bundle, or a Category view section, is called."""
    kind, _sep, value = key.partition(":")
    if kind == BUNDLE_ACCOUNT:
        return account_label(int(value)) if value.isdigit() else value
    return category_label(value)


def bundle_icon(key: str) -> str:
    kind, _sep, value = key.partition(":")
    return ACCOUNT_ICON if kind == BUNDLE_ACCOUNT else CATEGORY_ICONS.get(value, "")


def section_label(key: str, account_label: AccountLabel) -> str:
    kind, _sep, value = key.partition(":")
    fixed = {
        SECTION_PRIORITY: _("Priority"),
        SECTION_TODAY: _("Today"),
        SECTION_YESTERDAY: _("Yesterday"),
        SECTION_THIS_WEEK: _("This Week"),
        SECTION_LAST_WEEK: _("Last Week"),
        SECTION_THIS_MONTH: _("This Month"),
    }
    if key in fixed:
        return fixed[key]
    if kind == SECTION_MONTH:
        year, _dash, month = value.partition("-")
        return date(int(year), int(month), 1).strftime("%B %Y")
    if kind == SECTION_YEAR:
        return value if value != "0" else _("Undated")
    return bundle_label(key, account_label)


# A sender row names at most this many characters of each sender.
SENDER_CHARS = 22


def short_sender(name: str) -> str:
    """A bundle's sender as few words as will do: the address' local part for a
    sender with no name, cut short either way."""
    if "@" in name and " " not in name.strip():
        name = name.partition("@")[0]
    name = name.strip()
    return name if len(name) <= SENDER_CHARS else name[: SENDER_CHARS - 1] + "…"


class BundleRow(Gtk.Box):
    __gtype_name__ = "PostcardBundleRow"

    def __init__(self) -> None:
        super().__init__(
            spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12
        )
        self.add_css_class("bundle-row")
        self._avatar = Adw.Avatar(size=40, show_initials=False)
        self.append(self._avatar)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.append(text)
        title_line = Gtk.Box(spacing=6)
        text.append(title_line)
        self._title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._title.add_css_class("conversation-sender")
        title_line.append(self._title)
        self._count = Gtk.Label(xalign=0, hexpand=True)
        self._count.add_css_class("dim-label")
        title_line.append(self._count)
        self._unread_dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
        self._unread_dot.set_pixel_size(10)
        self._unread_dot.add_css_class("unread-dot")
        title_line.append(self._unread_dot)

        self._senders = Gtk.Label(
            xalign=0,
            wrap=True,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
            use_markup=True,
        )
        self._senders.add_css_class("bundle-senders")
        text.append(self._senders)

    def bind(self, bundle: Bundle, account_label: AccountLabel) -> None:
        self._avatar.set_icon_name(bundle_icon(bundle.key))
        self._title.set_label(bundle_label(bundle.key, account_label))
        self._count.set_label(str(bundle.count))
        self._unread_dot.set_visible(bundle.is_unread)
        senders = bundle.senders(bundle.count)
        shown = senders[:BUNDLE_SENDERS]
        parts = []
        for sender in shown:
            name = escape(short_sender(sender.name))
            text = f"<b>{name}</b>" if sender.is_unread else name
            if sender.count > 1:
                text += f" <span alpha='60%'>{sender.count}</span>"
            parts.append(text)
        rest = len(senders) - len(shown)
        if rest > 0:
            parts.append(f"<span alpha='60%'>+{rest}</span>")
        self._senders.set_markup("  ·  ".join(parts))
        if bundle.is_unread:
            self.add_css_class("unread")
        else:
            self.remove_css_class("unread")


class InboxItemRow(Gtk.Box):
    """One list row that is a section heading, a bundle or a conversation."""

    __gtype_name__ = "PostcardInboxItemRow"

    def __init__(self, avatars: AvatarLoader | None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.section = Gtk.Label(xalign=0)
        self.section.add_css_class("inbox-section")
        self.bundle = BundleRow()
        self.conversation = ConversationRow(avatars)
        for child in (self.section, self.bundle, self.conversation):
            self.append(child)

    def show_section(self, section: InboxSection, account_label: AccountLabel) -> None:
        self.section.set_label(section_label(section.key, account_label))
        self._show(self.section)

    def show_bundle(self, bundle: Bundle, account_label: AccountLabel) -> None:
        self.bundle.bind(bundle, account_label)
        self._show(self.bundle)

    def show_conversation(
        self,
        conversation: Conversation,
        is_outgoing: bool,
        account_label: str,
        account_color: int | None,
    ) -> None:
        self.conversation.bind(conversation, is_outgoing, account_label, account_color)
        self._show(self.conversation)

    def _show(self, shown: Gtk.Widget) -> None:
        for child in (self.section, self.bundle, self.conversation):
            child.set_visible(child is shown)
