"""The shape of a rate limit. Here, so that settings need nothing from the rest of the app."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimit:
    """At most ``limit`` times per ``window_seconds``, for each caller."""

    name: str
    limit: int
    window_seconds: int
