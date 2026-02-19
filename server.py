"""Main entrypoint — configures the linkedin-mcp-scheduler FastMCP instance
with OAuth and serves it over Streamable HTTP.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LINKEDIN_OAUTH_CLIENT_ID = os.environ["LINKEDIN_OAUTH_CLIENT_ID"]
LINKEDIN_OAUTH_CLIENT_SECRET = os.environ["LINKEDIN_OAUTH_CLIENT_SECRET"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
BASE_URL = os.environ["BASE_URL"]
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8002"))

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Apply the per-request client monkey-patch BEFORE importing mcp
#    (linkedin_mcp_scheduler registers tools at import time)
# ---------------------------------------------------------------------------

from client_patch import apply_patch  # noqa: E402

apply_patch()

# ---------------------------------------------------------------------------
# 2. Import the already-constructed FastMCP instance from linkedin-mcp-scheduler
# ---------------------------------------------------------------------------

from linkedin_mcp_scheduler.server import mcp  # noqa: E402

# ---------------------------------------------------------------------------
# 3. Set up auth provider and storage
# ---------------------------------------------------------------------------

from auth.provider import LinkedInOAuthProvider  # noqa: E402
from auth.storage import TokenStore  # noqa: E402

DATA_DIR = os.environ.get("DATA_DIR", "data")

store = TokenStore(secret=SESSION_SECRET, data_dir=DATA_DIR)
provider = LinkedInOAuthProvider(
    store=store,
    linkedin_client_id=LINKEDIN_OAUTH_CLIENT_ID,
    linkedin_client_secret=LINKEDIN_OAUTH_CLIENT_SECRET,
    base_url=BASE_URL,
)

# ---------------------------------------------------------------------------
# 3.5. Start publisher daemon in background thread
# ---------------------------------------------------------------------------

import linkedin_mcp_scheduler.daemon as _daemon_module  # noqa: E402
import linkedin_mcp_scheduler.db as _db_module  # noqa: E402

# Give the daemon thread its own SQLite connection via thread-local storage.
# The MCP server (main thread) uses the default get_db() singleton; the daemon
# thread gets a separate ScheduledPostsDB instance. SQLite handles file-level
# locking between connections, so no shared-connection threading issues arise.
_thread_local = threading.local()


def _thread_local_get_db(db_path: str | None = None) -> _db_module.ScheduledPostsDB:
    resolved = db_path or _db_module.DB_PATH
    db = getattr(_thread_local, "db", None)
    if db is not None and db._db_path != resolved:
        db.close()
        db = None
    if db is None:
        db = _db_module.ScheduledPostsDB(resolved)
        _thread_local.db = db
    return db


_daemon_module.get_db = _thread_local_get_db


def _build_client_from_store():
    from linkedin_sdk import LinkedInClient

    creds = store.get_any_linkedin_token()
    if not creds:
        raise RuntimeError("No LinkedIn credentials in token store — authenticate via OAuth first")
    access_token, _ = creds
    return LinkedInClient(access_token=access_token)


_daemon_module._build_client = _build_client_from_store


def _daemon_loop():
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    logger.info("Publisher daemon started (poll interval: %ds)", poll_interval)
    while True:
        try:
            _daemon_module.run_once()
        except RuntimeError as e:
            # Expected when no user has authenticated yet (_build_client_from_store raises)
            logger.debug("Daemon skipped: %s", e)
        except Exception as e:
            logger.error("Daemon error: %s", e)
        time.sleep(poll_interval)


_daemon_thread = threading.Thread(target=_daemon_loop, daemon=True, name="publisher-daemon")
_daemon_thread.start()

# ---------------------------------------------------------------------------
# 4. Configure auth on the existing mcp instance
#    (bypassing constructor validation since instance is already built)
# ---------------------------------------------------------------------------

from mcp.server.auth.provider import ProviderTokenVerifier  # noqa: E402
from mcp.server.auth.settings import (  # noqa: E402
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)

mcp.settings.auth = AuthSettings(
    issuer_url=BASE_URL,
    resource_server_url=f"{BASE_URL}/mcp",
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
)
mcp._auth_server_provider = provider
mcp._token_verifier = ProviderTokenVerifier(provider)

# ---------------------------------------------------------------------------
# 5. Configure HTTP transport settings
# ---------------------------------------------------------------------------

mcp.settings.host = HOST
mcp.settings.port = PORT
mcp.settings.stateless_http = True

# Allow the public hostname (and any additional internal hostnames) through
# transport security. ADDITIONAL_ALLOWED_HOSTS is comma-separated, used for
# K8s internal service names (e.g. "linkedin-scheduler-remote,linkedin-scheduler-remote:8003")
from urllib.parse import urlparse  # noqa: E402

_allowed: list[str] = []
_parsed = urlparse(BASE_URL)
if _parsed.hostname:
    _allowed.append(_parsed.hostname)
    if _parsed.port:
        _allowed.append(f"{_parsed.hostname}:{_parsed.port}")
_extra = os.environ.get("ADDITIONAL_ALLOWED_HOSTS", "")
if _extra:
    _allowed.extend(h.strip() for h in _extra.split(",") if h.strip())
if _allowed:
    mcp.settings.transport_security.allowed_hosts = _allowed

# ---------------------------------------------------------------------------
# 6. Custom routes (health check + LinkedIn OAuth callback)
# ---------------------------------------------------------------------------

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, RedirectResponse, Response  # noqa: E402


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> Response:
    """RFC 9728 — Protected Resource Metadata for MCP clients."""
    return JSONResponse({
        "resource": f"{BASE_URL}/mcp",
        "authorization_servers": [f"{BASE_URL}/"],
    })


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def linkedin_oauth_callback(request: Request) -> Response:
    """Handle LinkedIn's OAuth redirect after user authorizes.

    Exchanges LinkedIn's auth code for tokens, generates our own
    auth code, and redirects back to Claude's redirect_uri.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        error_desc = request.query_params.get("error_description", "")
        logger.error("LinkedIn OAuth error: %s — %s", error, error_desc)
        return JSONResponse(
            {"error": "linkedin_oauth_error", "detail": error, "description": error_desc},
            status_code=400,
        )

    if not code or not state:
        return JSONResponse(
            {"error": "missing_params", "detail": "code and state are required"},
            status_code=400,
        )

    try:
        redirect_url = await provider.exchange_linkedin_code(code, state)
        return RedirectResponse(url=redirect_url, status_code=302)
    except ValueError as exc:
        logger.error("OAuth callback failed: %s", exc)
        return JSONResponse(
            {"error": "callback_failed", "detail": str(exc)}, status_code=400
        )
    except Exception:
        logger.exception("Unexpected error in OAuth callback")
        return JSONResponse(
            {"error": "internal_error", "detail": "An internal error occurred"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# 7. Build the app with custom auth middleware for unauthenticated discovery
# ---------------------------------------------------------------------------

from auth.discovery_auth import MethodAwareAuthMiddleware  # noqa: E402


def _build_app():
    """Build the Starlette app and patch /mcp auth to allow tool discovery."""
    app = mcp.streamable_http_app()

    # Find the /mcp route and replace its endpoint with our custom middleware
    for route in app.routes:
        if hasattr(route, "path") and route.path == "/mcp":
            auth_middleware = route.app  # RequireAuthMiddleware
            inner_app = auth_middleware.app  # StreamableHTTPASGIApp
            route.app = MethodAwareAuthMiddleware(
                app=inner_app,
                auth_middleware=auth_middleware,
            )
            logger.info("Patched /mcp with MethodAwareAuthMiddleware")
            break
    else:
        logger.warning("Could not find /mcp route to patch — auth bypass for discovery will not work")

    return app


# ---------------------------------------------------------------------------
# 8. Run
# ---------------------------------------------------------------------------


def main():
    import uvicorn  # noqa: E402

    logger.info("Starting linkedin-scheduler-remote on %s:%d", HOST, PORT)
    logger.info("Base URL: %s", BASE_URL)
    logger.info("MCP endpoint: %s/mcp", BASE_URL)
    app = _build_app()
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
