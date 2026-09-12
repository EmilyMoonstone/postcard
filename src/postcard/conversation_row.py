from gi.repository import Adw, Gdk, Gtk, Pango

from . import mail_sync
from .avatar_loader import AvatarLoader
from .core.models.conversation import Conversation

# Adwaita's accent palette, one per account in the order they were added.
ACCOUNT_COLORS = 8


def account_color_class(index: int) -> str:
    return f"account-color-{index % ACCOUNT_COLORS}"


class ConversationRow(Gtk.Box):
    __gtype_name__ = "PostcardConversationRow"

    def __init__(self, avatars: AvatarLoader | None = None) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )

        self._avatars = avatars
        self._address = ""
        self.add_css_class("conversation-row")

        self._avatar = Adw.Avatar(size=40, show_initials=True)
        self.append(self._avatar)

        # Right: a vertical stack of sender/date, subject, preview/dot.
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.append(text)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text.append(top)

        self._sender_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        self._sender_label.add_css_class("conversation-sender")
        top.append(self._sender_label)

        self._pin = Gtk.Image.new_from_icon_name("view-pin-symbolic")
        self._pin.set_pixel_size(12)
        self._pin.add_css_class("dim-label")
        top.append(self._pin)

        self._priority = Gtk.Image.new_from_icon_name("mail-mark-important-symbolic")
        self._priority.set_pixel_size(12)
        self._priority.add_css_class("priority-mark")
        top.append(self._priority)

        self._star = Gtk.Image.new_from_icon_name("starred-symbolic")
        self._star.set_pixel_size(12)
        top.append(self._star)

        self._date_label = Gtk.Label(xalign=1)
        self._date_label.add_css_class("dim-label")
        top.append(self._date_label)

        self._subject_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._subject_label.add_css_class("conversation-subject")
        text.append(self._subject_label)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text.append(bottom)

        self._preview_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        self._preview_label.add_css_class("dim-label")
        bottom.append(self._preview_label)

        # Which account a thread arrived on, where several are merged: a dot
        # in that account's colour, the same one its sidebar row carries.
        self._account_dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
        self._account_dot.set_pixel_size(8)
        self._account_dot.set_valign(Gtk.Align.CENTER)
        self._account_dot.add_css_class("account-dot")
        bottom.append(self._account_dot)
        self._account_color_class = ""

        self._unread_dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
        self._unread_dot.set_pixel_size(10)
        self._unread_dot.set_valign(Gtk.Align.CENTER)
        self._unread_dot.add_css_class("unread-dot")
        bottom.append(self._unread_dot)

    # Fill this row from a conversation. Called every time the row is (re)used.
    # In an outgoing folder the sender of every message is the account itself,
    # so the row names the recipient instead -- and falls back to the sender for
    # mail that predates the recipient columns.
    def bind(
        self,
        conversation: Conversation,
        is_outgoing: bool,
        account_label: str,
        account_color: int | None = None,
    ) -> None:
        subject = conversation.subject
        if conversation.count > 1:
            subject = f"{subject}  ({conversation.count})"

        latest = conversation.latest
        if is_outgoing and latest.recipient:
            name, address = latest.recipient, latest.recipient_address
            participants = latest.recipient
        else:
            name, address = latest.sender, latest.sender_address
            participants = conversation.participants

        self._avatar.set_text(name)
        self._load_avatar(address)
        self._sender_label.set_label(participants)
        self._star.set_visible(conversation.is_starred)
        self._priority.set_visible(conversation.is_priority)
        self._pin.set_visible(conversation.is_pinned)
        self._date_label.set_label(mail_sync.format_date(conversation.date))
        self._subject_label.set_label(subject)
        self._preview_label.set_label(conversation.preview)
        self._account_dot.set_visible(account_color is not None)
        self._account_dot.set_tooltip_text(account_label or None)
        if self._account_color_class:
            self._account_dot.remove_css_class(self._account_color_class)
        self._account_color_class = (
            account_color_class(account_color) if account_color is not None else ""
        )
        if self._account_color_class:
            self._account_dot.add_css_class(self._account_color_class)
        self._unread_dot.set_visible(conversation.is_unread)

        # CSS class names, not Python identifiers: they have to match the
        # selectors in style.css, which the is_ prefix does not apply to.
        for css_class, is_set in (
            ("unread", conversation.is_unread),
            ("priority", conversation.is_priority),
        ):
            if is_set:
                self.add_css_class(css_class)
            else:
                self.remove_css_class(css_class)

    def _load_avatar(self, address: str) -> None:
        # Rows are recycled, so clear the old face and ignore a fetch that
        # lands after this row was rebound to someone else.
        self._address = address
        self._avatar.set_custom_image(None)
        if self._avatars is None:
            return

        def apply(texture: Gdk.Texture) -> None:
            if self._address == address:
                self._avatar.set_custom_image(texture)

        self._avatars.load(address, apply)
