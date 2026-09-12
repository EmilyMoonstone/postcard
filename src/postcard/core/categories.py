"""Sorting mail into people, invitations, notifications and newsletters.

No model and no network: every signal is already in the message. Bulk senders
announce themselves -- a List-Unsubscribe, a Precedence: bulk, a mailing
service's tracking header -- and machines say so with Auto-Submitted or a
no-reply address. What these rules get wrong, the user corrects per sender,
and that correction (Database.set_sender_category) wins over all of it.
"""

import re
from collections.abc import Mapping

CATEGORY_PEOPLE = "people"
CATEGORY_INVITATION = "invitation"
CATEGORY_NOTIFICATION = "notification"
CATEGORY_NEWSLETTER = "newsletter"

CATEGORIES = (
    CATEGORY_PEOPLE,
    CATEGORY_INVITATION,
    CATEGORY_NOTIFICATION,
    CATEGORY_NEWSLETTER,
)

# The headers the rules read, lowercased. A sync fetches exactly these beside
# the ones it displays, so adding a rule on a new header means adding it here.
SIGNAL_HEADERS = (
    "list-id",
    "list-unsubscribe",
    "list-unsubscribe-post",
    "precedence",
    "auto-submitted",
    "feedback-id",
    "content-class",
    "x-campaign",
    "x-campaignid",
    "x-mailchimp-campaign",
    "x-mc-user",
    "x-sg-eid",
    "x-csa-complaints",
    "x-mailgun-tag",
    "x-ms-exchange-calendar-series-instance-id",
)

# Mailing services' own tracking headers: only bulk mail carries them.
_BULK_SERVICE_HEADERS = (
    "feedback-id",
    "x-campaign",
    "x-campaignid",
    "x-mailchimp-campaign",
    "x-mc-user",
    "x-sg-eid",
    "x-csa-complaints",
    "x-mailgun-tag",
)

# Local parts that name a machine rather than a person. Matched as whole words
# of the local part, so "alerts.team@" counts and "valerie@" does not.
_NOTIFICATION_SENDER = re.compile(
    r"(^|[._+-])(no-?reply|do-?not-?reply|notifications?|notify|alerts?|"
    r"security|mailer-daemon|postmaster|automated|bounces?|system|updates?)"
    r"($|[._+-])",
    re.IGNORECASE,
)
_NEWSLETTER_SENDER = re.compile(
    r"(^|[._+-])(newsletters?|news|marketing|promo(tions?)?|offers?|deals|"
    r"angebote|werbung)($|[._+-])",
    re.IGNORECASE,
)


def categorize(
    headers: Mapping[str, str], sender_address: str, *, is_invitation: bool = False
) -> str:
    """The category of one message, from its headers and sender.

    `headers` maps lowercased names to values; only SIGNAL_HEADERS matter.
    `is_invitation` is for what headers can't show: a calendar part the
    body slice or the server revealed.
    """
    content_class = headers.get("content-class", "").lower()
    if is_invitation or "calendarmessage" in content_class:
        return CATEGORY_INVITATION

    local_part = sender_address.partition("@")[0]
    if _NEWSLETTER_SENDER.search(local_part):
        return CATEGORY_NEWSLETTER

    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if (auto_submitted and auto_submitted != "no") or _NOTIFICATION_SENDER.search(
        local_part
    ):
        return CATEGORY_NOTIFICATION

    precedence = headers.get("precedence", "").strip().lower()
    if (
        precedence in ("bulk", "list", "junk")
        or headers.get("list-unsubscribe")
        or any(headers.get(name) for name in _BULK_SERVICE_HEADERS)
    ):
        return CATEGORY_NEWSLETTER

    # A List-Id alone is a discussion list, where people write to each other.
    return CATEGORY_PEOPLE
