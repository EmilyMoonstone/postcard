import pytest

from postcard.core.categories import (
    CATEGORY_INVITATION,
    CATEGORY_NEWSLETTER,
    CATEGORY_NOTIFICATION,
    CATEGORY_PEOPLE,
    categorize,
)


def test_a_person_writing_is_people():
    assert categorize({}, "grace@example.com") == CATEGORY_PEOPLE


def test_a_discussion_list_is_still_people():
    # List-Id alone: a Verteiler where people write to each other.
    assert categorize({"list-id": "<kammer.ej-ffb.org>"}, "ada@example.com") == (
        CATEGORY_PEOPLE
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"list-unsubscribe": "<https://example.com/u>"},
        {"precedence": "bulk"},
        {"precedence": "List"},
        {"feedback-id": "123:campaign:mailchimp"},
        {"x-csa-complaints": "csa-complaints@eco.de"},
    ],
)
def test_bulk_mail_is_a_newsletter(headers):
    assert categorize(headers, "hello@shop.example") == CATEGORY_NEWSLETTER


@pytest.mark.parametrize(
    "sender", ["newsletter@verein.de", "news@example.com", "info.marketing@x.io"]
)
def test_a_newsletter_address_is_a_newsletter(sender):
    assert categorize({}, sender) == CATEGORY_NEWSLETTER


@pytest.mark.parametrize(
    "sender",
    [
        "noreply@github.com",
        "no-reply@accounts.google.com",
        "notifications@github.com",
        "account-security-noreply@accountprotection.microsoft.com",
        "do-not-reply@doctolib.de",
    ],
)
def test_a_machine_sender_is_a_notification(sender):
    # Even with the list headers GitHub sends along.
    assert categorize({"list-unsubscribe": "<x>", "precedence": "list"}, sender) == (
        CATEGORY_NOTIFICATION
    )


def test_auto_submitted_mail_is_a_notification():
    assert categorize({"auto-submitted": "auto-generated"}, "ims@firma.de") == (
        CATEGORY_NOTIFICATION
    )
    assert categorize({"auto-submitted": "no"}, "ims@firma.de") == CATEGORY_PEOPLE


def test_a_name_containing_a_machine_word_is_not_a_machine():
    assert categorize({}, "valerts@example.com") == CATEGORY_PEOPLE
    assert categorize({}, "newsom@example.com") == CATEGORY_PEOPLE


def test_invitations_win_over_everything():
    assert categorize({}, "noreply@x.com", is_invitation=True) == CATEGORY_INVITATION
    assert (
        categorize({"content-class": "urn:content-classes:calendarmessage"}, "a@b")
        == CATEGORY_INVITATION
    )
