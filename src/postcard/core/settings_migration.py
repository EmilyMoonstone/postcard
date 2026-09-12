"""Moving settings that became per-account off the app-wide GSettings keys."""

from gettext import gettext as _
from typing import Protocol

from .store.database import Database

SIGNATURE_NAME = _("Signature")


class Settings(Protocol):
    """The slice of Gio.Settings the migration reads and clears."""

    def get_boolean(self, key: str) -> bool: ...
    def set_boolean(self, key: str, value: bool) -> bool: ...
    def get_string(self, key: str) -> str: ...


def migrate_global_settings(db: Database, settings: Settings) -> None:
    """Carry the old app-wide signature and remote-images choice to accounts.

    Both became per-account settings. Run on every start, but each half only
    acts while its old key is still set, and clears it once copied.
    """
    if settings.get_boolean("load-remote-images"):
        for account in db.accounts():
            account.load_remote_images = True
            db.update_account(account)
        settings.set_boolean("load-remote-images", False)

    text = settings.get_string("signature-text").strip()
    if settings.get_boolean("signature-enabled") and text:
        signature_id = db.save_signature(SIGNATURE_NAME, text)
        for account in db.accounts():
            if account.signature_id is None:
                account.signature_id = signature_id
                db.update_account(account)
        settings.set_boolean("signature-enabled", False)
