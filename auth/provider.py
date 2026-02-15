"""OAuthAuthorizationServerProvider that proxies LinkedIn's OAuth 2.0 flow.

Implements the three-party OAuth dance:
  Claude.ai <-> this server (MCP + OAuth AS) <-> LinkedIn OAuth
"""

from __future__ import annotations

import base64
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from auth.storage import TokenStore
from client_patch import set_client_for_request

logger = logging.getLogger(__name__)

# LinkedIn OAuth endpoints
LINKEDIN_AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"

# LinkedIn API scopes
LINKEDIN_SCOPES = "openid profile email w_member_social"

# Token lifetimes
AUTH_CODE_LIFETIME = 300  # 5 minutes
ACCESS_TOKEN_LIFETIME = 86400  # 24 hours


class LinkedInOAuthProvider:
    """OAuth provider that proxies to LinkedIn for user authorization.

    Implements OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
    """

    def __init__(
        self,
        store: TokenStore,
        linkedin_client_id: str,
        linkedin_client_secret: str,
        base_url: str,
    ) -> None:
        self.store = store
        self.linkedin_client_id = linkedin_client_id
        self.linkedin_client_secret = linkedin_client_secret
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Dynamic Client Registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client(client_id)
        if data is None:
            return None
        return OAuthClientInformationFull(**data)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = secrets.token_hex(16)
        client_secret = secrets.token_hex(32)
        client_info.client_id = client_id
        client_info.client_secret = client_secret
        client_info.client_id_issued_at = int(time.time())
        client_info.client_secret_expires_at = 0  # never expires
        self.store.store_client(client_id, client_info.model_dump(mode="json"))

    # ------------------------------------------------------------------
    # Authorization (redirect to LinkedIn)
    # ------------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        # Generate state for our LinkedIn OAuth request
        linkedin_state = secrets.token_urlsafe(32)

        # Store Claude's params so the callback can complete the flow
        self.store.store_pending_auth(
            linkedin_state,
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes,
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": str(params.resource) if params.resource else None,
                "expires_at": time.time() + 600,  # 10 minutes
            },
        )

        # Build LinkedIn OAuth URL
        linkedin_params = {
            "client_id": self.linkedin_client_id,
            "redirect_uri": f"{self.base_url}/oauth/callback",
            "response_type": "code",
            "scope": LINKEDIN_SCOPES,
            "state": linkedin_state,
        }
        return f"{LINKEDIN_AUTHORIZE_URL}?{urlencode(linkedin_params)}"

    # ------------------------------------------------------------------
    # LinkedIn token exchange (called from the callback route)
    # ------------------------------------------------------------------

    async def exchange_linkedin_code(self, code: str, state: str) -> str:
        """Exchange LinkedIn auth code for tokens, store them, return redirect URL.

        Called by the /oauth/callback custom route handler.
        Returns the full redirect URL to send the user back to Claude.
        """
        pending = self.store.get_pending_auth(state)
        if pending is None:
            raise ValueError("Unknown or expired auth state")

        # Exchange code with LinkedIn (form-encoded per LinkedIn docs)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                LINKEDIN_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{self.base_url}/oauth/callback",
                    "client_id": self.linkedin_client_id,
                    "client_secret": self.linkedin_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "LinkedIn token exchange failed: %s %s",
                    exc.response.status_code,
                    exc.response.text,
                )
                raise ValueError(
                    "Failed to exchange authorization code with LinkedIn"
                ) from exc
            linkedin_data = resp.json()

        linkedin_access_token = linkedin_data["access_token"]
        linkedin_refresh_token = linkedin_data.get("refresh_token")

        # Generate our own authorization code for Claude
        our_code = secrets.token_urlsafe(32)
        self.store.store_auth_code(
            our_code,
            {
                "linkedin_access_token": linkedin_access_token,
                "linkedin_refresh_token": linkedin_refresh_token,
                "client_id": pending["client_id"],
                "code_challenge": pending["code_challenge"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending[
                    "redirect_uri_provided_explicitly"
                ],
                "scopes": pending["scopes"] or [],
                "resource": pending.get("resource"),
                "expires_at": time.time() + AUTH_CODE_LIFETIME,
            },
        )

        # Clean up pending auth
        self.store.delete_pending_auth(state)

        # Build redirect back to Claude
        redirect_params = {"code": our_code}
        if pending["state"]:
            redirect_params["state"] = pending["state"]

        redirect_uri = pending["redirect_uri"]
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(redirect_params)}"

    # ------------------------------------------------------------------
    # Authorization code flow (called by FastMCP /token handler)
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        data = self.store.get_auth_code(authorization_code)
        if data is None:
            return None
        if data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            scopes=data.get("scopes") or [],
            expires_at=data["expires_at"],
            resource=data.get("resource"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        data = self.store.get_auth_code(authorization_code.code)
        if data is None:
            raise TokenError(
                error="invalid_grant", error_description="Authorization code not found"
            )

        linkedin_access_token = data["linkedin_access_token"]
        scopes = data.get("scopes") or []

        # Generate our tokens
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ACCESS_TOKEN_LIFETIME

        # Store tokens with linkedin_access_token mapping
        self.store.store_access_token(
            access_token,
            {
                "linkedin_access_token": linkedin_access_token,
                "linkedin_refresh_token": data.get("linkedin_refresh_token"),
                "client_id": client.client_id,
                "scopes": scopes,
                "expires_at": expires_at,
                "refresh_token": refresh_token,
                "resource": data.get("resource"),
            },
        )
        self.store.store_refresh_token(
            refresh_token,
            {
                "linkedin_access_token": linkedin_access_token,
                "linkedin_refresh_token": data.get("linkedin_refresh_token"),
                "client_id": client.client_id,
                "scopes": scopes,
                "access_token": access_token,
                "resource": data.get("resource"),
            },
        )

        # Delete used auth code
        self.store.delete_auth_code(authorization_code.code)

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_LIFETIME,
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    # ------------------------------------------------------------------
    # Token verification (called on every MCP request via BearerAuthBackend)
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.get_access_token(token)
        if data is None:
            return None

        # Set up per-request LinkedInClient via contextvar
        set_client_for_request(
            access_token=data["linkedin_access_token"],
        )

        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data.get("scopes") or [],
            expires_at=data.get("expires_at"),
            resource=data.get("resource"),
        )

    # ------------------------------------------------------------------
    # Refresh token flow
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        data = self.store.get_refresh_token(refresh_token)
        if data is None:
            return None
        if data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data.get("scopes") or [],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        data = self.store.get_refresh_token(refresh_token.token)
        if data is None:
            raise TokenError(
                error="invalid_grant", error_description="Refresh token not found"
            )

        linkedin_access_token = data["linkedin_access_token"]
        effective_scopes = scopes if scopes else data.get("scopes") or []
        resource = data.get("resource")

        # Rotate tokens atomically
        new_access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ACCESS_TOKEN_LIFETIME

        self.store.rotate_tokens(
            old_access_token=data.get("access_token"),
            old_refresh_token=refresh_token.token,
            new_access_token=new_access_token,
            new_access_data={
                "linkedin_access_token": linkedin_access_token,
                "linkedin_refresh_token": data.get("linkedin_refresh_token"),
                "client_id": client.client_id,
                "scopes": effective_scopes,
                "expires_at": expires_at,
                "refresh_token": new_refresh_token,
                "resource": resource,
            },
            new_refresh_token=new_refresh_token,
            new_refresh_data={
                "linkedin_access_token": linkedin_access_token,
                "linkedin_refresh_token": data.get("linkedin_refresh_token"),
                "client_id": client.client_id,
                "scopes": effective_scopes,
                "access_token": new_access_token,
                "resource": resource,
            },
        )

        return OAuthToken(
            access_token=new_access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_LIFETIME,
            refresh_token=new_refresh_token,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        if isinstance(token, AccessToken):
            self.store.delete_tokens_for_access_token(token.token)
        elif isinstance(token, RefreshToken):
            self.store.delete_tokens_for_refresh_token(token.token)
