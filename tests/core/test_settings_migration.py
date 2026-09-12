import pytest

from postcard.core.settings_migration import migrate_global_settings
from postcard.core.store.database import Database


class FakeSettings:
    def __init__(self, **values):
        self.values = values

    def get_boolean(self, key):
        return self.values.get(key, False)

    def set_boolean(self, key, value):
        self.values[key] = value
        return True

    def get_string(self, key):
        return self.values.get(key, "")


@pytest.fixture
def db():
    database = Database(":memory:")
    database.save_account("a@x", "A", "imap.x", 993, "smtp.x", 465)
    database.save_account("b@x", "B", "imap.x", 993, "smtp.x", 465)
    yield database
    database.close()


def test_the_old_global_choices_move_to_every_account_once(db):
    settings = FakeSettings(
        **{
            "load-remote-images": True,
            "signature-enabled": True,
            "signature-text": "Emily\n",
        }
    )

    migrate_global_settings(db, settings)
    migrate_global_settings(db, settings)

    assert all(account.load_remote_images for account in db.accounts())
    assert len(db.signatures()) == 1
    assert {db.signature_text_for(a.id) for a in db.accounts()} == {"Emily"}
    assert settings.values["load-remote-images"] is False
    assert settings.values["signature-enabled"] is False


def test_nothing_set_globally_changes_nothing(db):
    migrate_global_settings(db, FakeSettings(**{"signature-text": "unused"}))

    assert db.signatures() == []
    assert not any(account.load_remote_images for account in db.accounts())
