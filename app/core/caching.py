"""Telling caches which responses belong to one person."""

from fastapi import Response


def never_cache(response: Response) -> None:
    """For a response that depends on who asked: no cache may keep it."""
    response.headers["Cache-Control"] = "private, no-store"


def varies_by_login(response: Response, *, logged_in: bool) -> None:
    """
    For a route that needs no login but answers differently with one.

    `Vary` tells a cache that the anonymous copy is not the answer for a
    request that carries a token. Without it, a cache that ignores the
    Authorization header could hand a logged-in reader the visitor's page.
    """
    response.headers["Vary"] = "Authorization"
    if logged_in:
        never_cache(response)
