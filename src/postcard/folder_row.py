from gi.repository import Adw, GObject, Gtk

from . import mail_sync
from .core.models.folder import Folder


class FolderRow(Gtk.Box):
    __gtype_name__ = "PostcardFolderRow"

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )

        self._icon = Gtk.Image()
        self.append(self._icon)

        self._name_label = Gtk.Label(xalign=0, hexpand=True)
        self.append(self._name_label)

        # Adw.Spinner spins whenever it is visible -- no start/stop to track.
        # It lives in a fixed-size slot so an account row keeps the same width
        # between syncs; hiding the spinner itself shifts the whole sidebar.
        self._spinner_slot = Gtk.Box(width_request=16, height_request=16, visible=False)
        self._spinner = Adw.Spinner(visible=False)
        self._spinner_slot.append(self._spinner)
        self.append(self._spinner_slot)

        self._badge = Gtk.Label()
        self._badge.add_css_class("dim-label")
        self.append(self._badge)

    # Fill this row from a folder. Called every time the row is (re)used.
    # `label` stands in for the folder's own name where the sidebar lists a
    # role folder under its account, and `is_syncable` gives it the spinner.
    def bind(
        self,
        folder: Folder,
        unread_count: int,
        label: str | None = None,
        is_syncable: bool = False,
    ) -> None:
        self._icon.set_visible(True)
        self._icon.set_from_icon_name(folder.icon_name)
        self._name_label.set_label(label or mail_sync.folder_label(folder))
        self._name_label.remove_css_class("sidebar-heading")
        self._spinner_slot.set_visible(is_syncable)
        self.set_syncing(False)
        self._badge.set_label(str(unread_count))
        self._badge.set_visible(unread_count > 0)

    # A section title between the groups, like Spark's "Ordner".
    def bind_heading(self, label: str) -> None:
        self._icon.set_visible(False)
        self._name_label.set_label(label)
        self._name_label.add_css_class("sidebar-heading")
        self._spinner_slot.set_visible(False)
        self._badge.set_visible(False)

    # The row that opens every folder the groups don't show.
    def bind_more(self, label: str) -> None:
        self._icon.set_visible(True)
        self._icon.set_from_icon_name("view-more-horizontal-symbolic")
        self._name_label.set_label(label)
        self._name_label.remove_css_class("sidebar-heading")
        self._spinner_slot.set_visible(False)
        self._badge.set_visible(False)

    # The window calls this as an account's syncs start and finish.
    def set_syncing(self, is_syncing: bool) -> None:
        self._spinner.set_visible(is_syncing)


class SidebarHeading(GObject.Object):
    __gtype_name__ = "PostcardSidebarHeading"

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label: str = label


class SidebarMore(GObject.Object):
    __gtype_name__ = "PostcardSidebarMore"
