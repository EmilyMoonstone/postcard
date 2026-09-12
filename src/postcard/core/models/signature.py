from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Signature:
    """A named signature; each account picks one as its default."""

    id: int
    name: str
    body: str  # plain text, turned into HTML by compose.signature_block
