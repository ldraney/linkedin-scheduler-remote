"""Main entrypoint — configures the linkedin-mcp-scheduler FastMCP instance
with OAuth and serves it over Streamable HTTP using mcp-remote-auth.
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
ONBOARD_SECRET = os.environ.get("ONBOARD_SECRET", "")
DATA_DIR = os.environ.get("DATA_DIR", "data")

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Apply the per-request client monkey-patch BEFORE importing mcp
#    (linkedin_mcp_scheduler registers tools at import time)
# ---------------------------------------------------------------------------

from client_patch import apply_patch, set_client_for_request  # noqa: E402

apply_patch()

# ---------------------------------------------------------------------------
# 2. Import the already-constructed FastMCP instance from linkedin-mcp-scheduler
# ---------------------------------------------------------------------------

from linkedin_mcp_scheduler.server import mcp  # noqa: E402

# ---------------------------------------------------------------------------
# 3. Configure auth via mcp-remote-auth
# ---------------------------------------------------------------------------

from mcp_remote_auth import (  # noqa: E402
    ProviderConfig,
    TokenStore,
    OAuthProxyProvider,
    configure_mcp_auth,
    configure_transport_security,
    register_standard_routes,
    register_onboarding_routes,
    build_app_with_middleware,
)


def _setup_linkedin_client(token_data, config):
    """Inject a per-request LinkedInClient from the stored access token."""
    set_client_for_request(access_token=token_data["linkedin_access_token"])


config = ProviderConfig(
    provider_name="LinkedIn",
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    token_url="https://www.linkedin.com/oauth/v2/accessToken",
    client_id=LINKEDIN_OAUTH_CLIENT_ID,
    client_secret=LINKEDIN_OAUTH_CLIENT_SECRET,
    base_url=BASE_URL,
    scopes="openid profile email w_member_social",
    upstream_token_key="linkedin_access_token",
    upstream_response_token_field="access_token",
    upstream_refresh_key="linkedin_refresh_token",
    upstream_response_refresh_field="refresh_token",
    access_token_lifetime=31536000,
    setup_client_for_request=_setup_linkedin_client,
    user_info_url="https://api.linkedin.com/v2/userinfo",
    user_info_identity_field="email",
    onboard_extra_scopes="openid email",
)

store = TokenStore(secret=SESSION_SECRET, data_dir=DATA_DIR)
provider = OAuthProxyProvider(store=store, config=config)

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

    creds = store.get_any_upstream_token("linkedin_access_token")
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
# 4. Wire up auth, transport, routes
# ---------------------------------------------------------------------------

configure_mcp_auth(mcp, provider, BASE_URL)
mcp.settings.host = HOST
mcp.settings.port = PORT
mcp.settings.stateless_http = True
configure_transport_security(mcp, BASE_URL, os.environ.get("ADDITIONAL_ALLOWED_HOSTS", ""))
register_standard_routes(mcp, provider, BASE_URL)
register_onboarding_routes(mcp, provider, store, config, ONBOARD_SECRET)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import uvicorn  # noqa: E402

    logger.info("Starting linkedin-scheduler-remote on %s:%d", HOST, PORT)
    app = build_app_with_middleware(mcp, use_body_inspection=True)
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
