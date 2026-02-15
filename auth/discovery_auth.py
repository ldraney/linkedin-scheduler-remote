"""Custom ASGI middleware that allows unauthenticated MCP tool discovery.

The MCP SDK's RequireAuthMiddleware blocks ALL requests to /mcp without a
Bearer token. But protocol methods like `initialize` and `tools/list` don't
access user data — they only return server capabilities and tool schemas.

This middleware reads the JSON-RPC method from the request body and only
enforces auth for methods that touch user data (like `tools/call`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Methods that are safe without authentication (no user data accessed)
UNAUTHENTICATED_METHODS = frozenset({
    "initialize",
    "notifications/initialized",
    "tools/list",
    "prompts/list",
    "resources/list",
    "ping",
})


class MethodAwareAuthMiddleware:
    """Wraps an ASGI app and conditionally delegates to an auth middleware.

    For JSON-RPC methods in UNAUTHENTICATED_METHODS, forwards directly to the
    inner app (skipping auth). For all other methods (especially tools/call),
    delegates to the auth_middleware which enforces Bearer token validation.
    """

    def __init__(self, app: ASGIApp, auth_middleware: ASGIApp) -> None:
        self.app = app  # The actual MCP StreamableHTTPASGIApp
        self.auth_middleware = auth_middleware  # RequireAuthMiddleware wrapping app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.auth_middleware(scope, receive, send)
            return

        # For GET/DELETE (SSE, session management), always require auth
        method = scope.get("method", "GET")
        if method != "POST":
            await self.auth_middleware(scope, receive, send)
            return

        # Buffer the request body so we can peek at the JSON-RPC method
        body_parts: list[bytes] = []
        body_complete = False

        async def buffering_receive() -> dict[str, Any]:
            nonlocal body_complete
            message = await receive()
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    body_complete = True
            return message

        # Read the full body
        while not body_complete:
            await buffering_receive()

        full_body = b"".join(body_parts)

        # Try to parse JSON-RPC method(s)
        rpc_methods = _extract_jsonrpc_methods(full_body)

        # Create a replay receive that returns the buffered body
        body_sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": full_body,
                    "more_body": False,
                }
            # After body is sent, wait for disconnect
            return await receive()

        if rpc_methods and rpc_methods.issubset(UNAUTHENTICATED_METHODS):
            logger.debug("Allowing unauthenticated %s request", rpc_methods)
            await self.app(scope, replay_receive, send)
        else:
            await self.auth_middleware(scope, replay_receive, send)


def _extract_jsonrpc_methods(body: bytes) -> set[str]:
    """Extract all JSON-RPC method names from a request body.

    Handles both single requests and batch arrays.
    Returns a set of method names (empty on parse failure).
    """
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            m = parsed.get("method")
            return {m} if m else set()
        if isinstance(parsed, list):
            return {
                item.get("method")
                for item in parsed
                if isinstance(item, dict) and item.get("method")
            }
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return set()
