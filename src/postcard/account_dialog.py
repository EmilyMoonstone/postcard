import logging
import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, GObject, Gtk

from . import mail_sync
from .core import autoconfig, hosting, secrets
from .core.hosting import HostingPreset, ServerSettings
from .core.models.account import SECURITY_OPTIONS, Account, parse_port
from .core.net import errors
from .core.net.auth import Credential
from .core.store.database import Database

logger = logging.getLogger(__name__)

# Wait for typing to settle before asking DNS and the web about a domain.
DISCOVERY_DEBOUNCE_MS = 700

# Row 0 of the provider combo: let Postcard look the address up.
AUTOMATIC = 0


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/account-dialog.ui")
class PostcardAccountDialog(Adw.Dialog):
    """Adds an account, or edits one when given it.

    Either way nothing is saved until both servers have accepted the sign-in,
    unless the user says to save anyway.
    """

    __gtype_name__ = "PostcardAccountDialog"

    cancel_button: Gtk.Button = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()
    check_spinner: Adw.Spinner = Gtk.Template.Child()
    status_banner: Adw.Banner = Gtk.Template.Child()
    account_group: Adw.PreferencesGroup = Gtk.Template.Child()
    display_name_row: Adw.EntryRow = Gtk.Template.Child()
    email_row: Adw.EntryRow = Gtk.Template.Child()
    username_row: Adw.EntryRow = Gtk.Template.Child()
    password_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    provider_group: Adw.PreferencesGroup = Gtk.Template.Child()
    hosting_row: Adw.ComboRow = Gtk.Template.Child()
    discovery_row: Adw.ActionRow = Gtk.Template.Child()
    discovery_spinner: Adw.Spinner = Gtk.Template.Child()
    imap_group: Adw.PreferencesGroup = Gtk.Template.Child()
    imap_host_row: Adw.EntryRow = Gtk.Template.Child()
    imap_port_row: Adw.EntryRow = Gtk.Template.Child()
    imap_security_row: Adw.ComboRow = Gtk.Template.Child()
    smtp_group: Adw.PreferencesGroup = Gtk.Template.Child()
    smtp_host_row: Adw.EntryRow = Gtk.Template.Child()
    smtp_port_row: Adw.EntryRow = Gtk.Template.Child()
    smtp_security_row: Adw.ComboRow = Gtk.Template.Child()
    remote_images_row: Adw.SwitchRow = Gtk.Template.Child()
    signature_row: Adw.ComboRow = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "account-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db: Database, account: Account | None = None) -> None:
        super().__init__()
        self._db = db
        self._account = account
        self._is_closed = False
        self._discovery_timeout = 0
        # Bumped per lookup and per check, so a late answer for an address or
        # settings the user has since changed is dropped.
        self._discovery_generation = 0
        self._check_generation = 0
        self._signatures = db.signatures()

        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.add_button.connect("clicked", self._on_save_clicked)
        self.status_banner.connect("button-clicked", lambda _b: self._save())
        self.connect("closed", self._on_closed)

        self.hosting_row.set_model(
            Gtk.StringList.new(
                [_("Find Automatically"), *(p.name for p in hosting.PRESETS)]
            )
        )
        self.signature_row.set_model(
            Gtk.StringList.new([_("None"), *(s.name for s in self._signatures)])
        )

        if account is not None:
            self._fill_from(account)

        # Values we put in the fields ourselves, so a later autofill can tell
        # its own text from something the user typed and never clobber it.
        # Seeded with what the fields hold now, which counts as ours.
        self._autofilled_text = {
            row: row.get_text()
            for row in (
                self.username_row,
                self.imap_host_row,
                self.imap_port_row,
                self.smtp_host_row,
                self.smtp_port_row,
            )
        }
        self._autofilled_security = {
            combo: combo.get_selected()
            for combo in (self.imap_security_row, self.smtp_security_row)
        }

        if account is None:
            self.email_row.connect("changed", self._autofill_username)
            self.email_row.connect("changed", self._schedule_discovery)
        self.hosting_row.connect("notify::selected", self._on_hosting_selected)
        self.imap_host_row.connect("changed", self._on_imap_host_changed)

        for row in (
            self.display_name_row,
            self.email_row,
            self.password_row,
            self.imap_host_row,
            self.smtp_host_row,
            self.imap_port_row,
            self.smtp_port_row,
        ):
            row.connect("changed", self._update_add_sensitivity)
        self._update_add_sensitivity()

    # --- editing an existing account -----------------------------------------

    def _fill_from(self, account: Account) -> None:
        self.set_title(_("Edit Account"))
        self.add_button.set_label(_("Save"))
        self.display_name_row.set_text(account.display_name)
        self.email_row.set_text(account.email)
        self.username_row.set_text(account.username or account.email)
        self.password_row.set_title(_("Password (leave empty to keep it)"))
        self.imap_host_row.set_text(account.imap_host)
        self.imap_port_row.set_text(str(account.imap_port))
        self.imap_security_row.set_selected(_security_index(account.imap_security))
        self.smtp_host_row.set_text(account.smtp_host)
        self.smtp_port_row.set_text(str(account.smtp_port))
        self.smtp_security_row.set_selected(_security_index(account.smtp_security))
        self.remote_images_row.set_active(account.load_remote_images)
        ids = [signature.id for signature in self._signatures]
        if account.signature_id in ids:
            self.signature_row.set_selected(ids.index(account.signature_id) + 1)

        if account.goa_id:
            # Online Accounts owns the address, sign-in and servers.
            self.account_group.set_description(
                _("Signed in through Online Accounts in Settings.")
            )
            for widget in (
                self.email_row,
                self.username_row,
                self.password_row,
                self.provider_group,
                self.imap_group,
                self.smtp_group,
            ):
                widget.set_visible(False)

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._is_closed = True
        if self._discovery_timeout:
            GLib.source_remove(self._discovery_timeout)
            self._discovery_timeout = 0

    # --- filling in server settings --------------------------------------------

    def _autofill_username(self, *_args: object) -> None:
        # The address is a sensible username for every server, known or not.
        email = self.email_row.get_text()
        if self.username_row.get_text() == self._autofilled_text[self.username_row]:
            self._set_autofilled(self.username_row, email)

    def _set_autofilled(self, row: Adw.EntryRow, value: str) -> None:
        if row.get_text() == self._autofilled_text[row]:
            row.set_text(value)
            self._autofilled_text[row] = value

    def _apply_settings(self, settings: ServerSettings, *, is_forced: bool) -> None:
        """Fill the server fields; is_forced overwrites what the user typed."""
        for row, value in (
            (self.imap_host_row, settings.imap_host),
            (self.imap_port_row, str(settings.imap_port)),
            (self.smtp_host_row, settings.smtp_host),
            (self.smtp_port_row, str(settings.smtp_port)),
        ):
            if is_forced:
                self._autofilled_text[row] = row.get_text()
            self._set_autofilled(row, value)
        for combo, security in (
            (self.imap_security_row, settings.imap_security),
            (self.smtp_security_row, settings.smtp_security),
        ):
            selected = SECURITY_OPTIONS.index(security)
            if is_forced or combo.get_selected() == self._autofilled_security[combo]:
                combo.set_selected(selected)
                self._autofilled_security[combo] = selected
        email = self.email_row.get_text().strip()
        if email:
            username = autoconfig.fill_placeholders(settings.username, email)
            if is_forced:
                self._autofilled_text[self.username_row] = self.username_row.get_text()
            self._set_autofilled(self.username_row, username)

    def _show_discovery(
        self, title: str, subtitle: str = "", busy: bool = False
    ) -> None:
        self.discovery_row.set_visible(bool(title))
        self.discovery_row.set_title(title)
        self.discovery_row.set_subtitle(subtitle)
        self.discovery_spinner.set_visible(busy)

    def _schedule_discovery(self, *_args: object) -> None:
        if self._discovery_timeout:
            GLib.source_remove(self._discovery_timeout)
        self._discovery_timeout = GLib.timeout_add(
            DISCOVERY_DEBOUNCE_MS, self._start_discovery
        )

    def _start_discovery(self) -> bool:
        self._discovery_timeout = 0
        email = self.email_row.get_text().strip()
        if self.hosting_row.get_selected() != AUTOMATIC or "@" not in email:
            return False
        self._discovery_generation += 1
        self._show_discovery(_("Looking up the settings…"), busy=True)
        threading.Thread(
            target=self._discovery_worker,
            args=(email, self._discovery_generation),
            daemon=True,
        ).start()
        return False

    # Runs on the worker thread: DNS and HTTP only, no Gtk.
    def _discovery_worker(self, email: str, generation: int) -> None:
        try:
            settings = autoconfig.discover(email)
        except Exception:
            logger.warning("looking up the servers for %s failed", email, exc_info=True)
            settings = None
        GLib.idle_add(self._on_discovered, generation, settings)

    def _on_discovered(self, generation: int, settings: ServerSettings | None) -> bool:
        if self._is_closed or generation != self._discovery_generation:
            return False
        if settings is None:
            self._show_discovery(
                _("No settings found"),
                _("Choose your mail provider above, or enter the servers below."),
            )
            return False
        self._apply_settings(settings, is_forced=False)
        self._show_discovery(
            _("Settings found"), _("From {source}").format(source=settings.source)
        )
        return False

    def _on_hosting_selected(self, row: Adw.ComboRow, _param: object) -> None:
        index = row.get_selected()
        if index == AUTOMATIC:
            self._start_discovery()
            return
        preset = hosting.PRESETS[index - 1]
        if not preset.needs_mx:
            self._apply_preset(preset, "")
            return
        email = self.email_row.get_text().strip()
        self._discovery_generation += 1
        self._show_discovery(_("Looking up your mail server…"), busy=True)
        threading.Thread(
            target=self._mx_worker,
            args=(preset, email, self._discovery_generation),
            daemon=True,
        ).start()

    # Runs on the worker thread: DNS only, no Gtk.
    def _mx_worker(self, preset: HostingPreset, email: str, generation: int) -> None:
        matched = hosting.match_mx(autoconfig.lookup_mx(email.rpartition("@")[2]))
        mx_host = matched[1] if matched is not None and matched[0] is preset else ""
        GLib.idle_add(self._on_mx_found, preset, mx_host, generation)

    def _on_mx_found(
        self, preset: HostingPreset, mx_host: str, generation: int
    ) -> bool:
        if not self._is_closed and generation == self._discovery_generation:
            self._apply_preset(preset, mx_host)
        return False

    def _apply_preset(self, preset: HostingPreset, mx_host: str) -> None:
        settings = hosting.resolve(preset, mx_host)
        if preset.needs_mx and not mx_host:
            # The server name is the customer's own; say where to find it.
            settings = ServerSettings(
                "",
                settings.imap_port,
                settings.imap_security,
                "",
                settings.smtp_port,
                settings.smtp_security,
                settings.username,
                preset.name,
            )
        self._apply_settings(settings, is_forced=True)
        self._show_discovery(preset.name, preset.hint)

    # A server name typed by hand can give the provider away, and with it the
    # outgoing server and the ports.
    def _on_imap_host_changed(self, *_args: object) -> None:
        host = self.imap_host_row.get_text().strip()
        preset = hosting.match_host(host)
        if preset is None:
            return
        settings = hosting.resolve(preset, host)
        self._set_autofilled(self.smtp_host_row, settings.smtp_host)
        self._set_autofilled(self.smtp_port_row, str(settings.smtp_port))

    # --- saving ------------------------------------------------------------------

    def _update_add_sensitivity(self, *_args: object) -> None:
        required = [self.display_name_row.get_text()]
        if self._account is None or not self._account.goa_id:
            required += [
                self.email_row.get_text(),
                self.imap_host_row.get_text(),
                self.smtp_host_row.get_text(),
            ]
        if self._account is None:
            required.append(self.password_row.get_text())
        # The ports are validated too, not just non-empty: saving has to parse
        # them, and a raise inside a clicked handler just looks like nothing.
        ports_are_valid = (
            parse_port(self.imap_port_row.get_text()) is not None
            and parse_port(self.smtp_port_row.get_text()) is not None
        )
        self.add_button.set_sensitive(
            ports_are_valid and all(field.strip() for field in required)
        )

    def _edited_account(self) -> Account | None:
        imap_port = parse_port(self.imap_port_row.get_text())
        smtp_port = parse_port(self.smtp_port_row.get_text())
        if imap_port is None or smtp_port is None:
            return None
        base = self._account
        signature_index = self.signature_row.get_selected()
        return Account(
            id=base.id if base else 0,
            email=self.email_row.get_text().strip(),
            display_name=self.display_name_row.get_text().strip(),
            imap_host=self.imap_host_row.get_text().strip(),
            imap_port=imap_port,
            imap_security=SECURITY_OPTIONS[self.imap_security_row.get_selected()],
            smtp_host=self.smtp_host_row.get_text().strip(),
            smtp_port=smtp_port,
            smtp_security=SECURITY_OPTIONS[self.smtp_security_row.get_selected()],
            username=self.username_row.get_text().strip(),
            goa_id=base.goa_id if base else "",
            protocol=base.protocol if base else "imap",
            is_bundled=base.is_bundled if base else False,
            load_remote_images=self.remote_images_row.get_active(),
            signature_id=self._signatures[signature_index - 1].id
            if signature_index > 0
            else None,
        )

    def _needs_check(self, edited: Account) -> bool:
        base = self._account
        if base is None:
            return True
        if base.goa_id:
            return False
        return bool(self.password_row.get_text()) or any(
            getattr(base, name) != getattr(edited, name)
            for name in (
                "email",
                "username",
                "imap_host",
                "imap_port",
                "imap_security",
                "smtp_host",
                "smtp_port",
                "smtp_security",
            )
        )

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        edited = self._edited_account()
        if edited is None:
            return
        if not self._needs_check(edited):
            self._save()
            return
        self._check_generation += 1
        self._set_checking(True)
        self.status_banner.set_revealed(False)
        threading.Thread(
            target=self._check_worker,
            args=(edited, self.password_row.get_text(), self._check_generation),
            daemon=True,
        ).start()

    def _set_checking(self, is_checking: bool) -> None:
        self.check_spinner.set_visible(is_checking)
        self.add_button.set_sensitive(not is_checking)

    # Runs on the worker thread: network and the keyring only, no Gtk or DB.
    def _check_worker(self, account: Account, password: str, generation: int) -> None:
        if not password and account.id:
            password = secrets.lookup_password(account.id) or ""
        credential = Credential(account.login_name, password)
        try:
            mail_sync.check_account(account, credential)
        except mail_sync.AccountCheckError as error:
            logger.warning("%s (account %s)", error, account.email, exc_info=True)
            GLib.idle_add(self._on_checked, generation, error)
            return
        GLib.idle_add(self._on_checked, generation, None)

    def _on_checked(
        self, generation: int, error: mail_sync.AccountCheckError | None
    ) -> bool:
        if self._is_closed or generation != self._check_generation:
            return False
        self._set_checking(False)
        if error is None:
            self._save()
            return False
        cause = error.__cause__
        _is_auth, reason = errors.classify(
            cause if isinstance(cause, Exception) else error, error.host
        )
        self.status_banner.set_title(
            _("{server}: {reason}").format(server=error.server, reason=reason)
        )
        self.status_banner.set_revealed(True)
        return False

    def _save(self) -> None:
        edited = self._edited_account()
        if edited is None:
            return
        password = self.password_row.get_text()
        if self._account is None:
            account = self._db.save_account(
                email=edited.email,
                display_name=edited.display_name,
                imap_host=edited.imap_host,
                imap_port=edited.imap_port,
                imap_security=edited.imap_security,
                smtp_host=edited.smtp_host,
                smtp_port=edited.smtp_port,
                smtp_security=edited.smtp_security,
                username=edited.username,
            )
            edited.id = account.id
            self._db.update_account(edited)
            secrets.store_password(account.id, password)
            self.emit("account-added")
        else:
            self._db.update_account(edited)
            if password and not edited.goa_id:
                secrets.store_password(edited.id, password)
            # New settings mean new connections.
            mail_sync.close_sessions(edited.id)
            self.emit("account-changed")
        self.close()


def _security_index(security: str) -> int:
    return SECURITY_OPTIONS.index(security) if security in SECURITY_OPTIONS else 0
