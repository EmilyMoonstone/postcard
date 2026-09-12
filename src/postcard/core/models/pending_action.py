from dataclasses import dataclass

# What a queued action does on the server.
ACTION_FLAG = "flag"
ACTION_MOVE = "move"


@dataclass(frozen=True, slots=True)
class PendingAction:
    """A flag change or move made while offline, waiting to reach the server.

    The local database already shows it done; replaying this is what makes
    the server agree. Frozen, since a replay worker gets it as its snapshot.
    """

    id: int
    account_id: int
    kind: str  # ACTION_FLAG or ACTION_MOVE
    folder_name: str
    uids: tuple[str, ...]
    flag: str = ""
    should_add: bool = False
    destination: str = ""
