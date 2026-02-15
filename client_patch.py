"""Per-request LinkedInClient injection via ContextVar.

Monkey-patches get_client() in linkedin_mcp_scheduler.server so each
authenticated request gets its own LinkedInClient with the user's
LinkedIn OAuth credentials.

Note: The scheduling tools only interact with the local SQLite database
(via get_db()), so they don't need a LinkedInClient per se.  The patch
is applied for completeness and so that any future tools that call
get_client() will work correctly in multi-user mode.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from linkedin_sdk import LinkedInClient

_request_client: ContextVar[LinkedInClient | None] = ContextVar(
    "_request_client", default=None
)


def patched_get_client() -> LinkedInClient:
    """Return the per-request LinkedInClient set by the OAuth flow."""
    client = _request_client.get()
    if client is None:
        raise RuntimeError(
            "No LinkedInClient set for this request — is OAuth configured?"
        )
    return client


def set_client_for_request(access_token: str, person_id: str | None = None) -> None:
    """Create a LinkedInClient with the given access token and set it on the contextvar."""
    client = LinkedInClient(access_token=access_token, person_id=person_id)
    _request_client.set(client)


@contextmanager
def client_context(access_token: str, person_id: str | None = None):
    """Context manager that sets up a per-request LinkedInClient."""
    token = _request_client.set(
        LinkedInClient(access_token=access_token, person_id=person_id)
    )
    try:
        yield
    finally:
        _request_client.reset(token)


def apply_patch() -> None:
    """Replace get_client in linkedin_mcp_scheduler.server."""
    import linkedin_mcp_scheduler.server

    linkedin_mcp_scheduler.server.get_client = patched_get_client
