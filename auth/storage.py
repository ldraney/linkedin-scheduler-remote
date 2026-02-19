"""Encrypted file-based token and state persistence.

Stores OAuth state (pending auth flows, auth codes, access/refresh tokens,
registered clients) in a Fernet-encrypted JSON file under data/.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class TokenStore:
    """Thread-safe, Fernet-encrypted JSON file store for OAuth state."""

    def __init__(self, secret: str, data_dir: str = "data") -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)
        self._path = Path(data_dir) / "tokens.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "clients": {},
            "pending_auth": {},
            "auth_codes": {},
            "access_tokens": {},
            "refresh_tokens": {},
        }
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._path.exists():
            try:
                encrypted = self._path.read_bytes()
                decrypted = self._fernet.decrypt(encrypted)
                self._data = json.loads(decrypted)
            except Exception:
                logger.warning(
                    "Could not load token store (corrupt or wrong key), starting fresh"
                )
                self._data = {
                    "clients": {},
                    "pending_auth": {},
                    "auth_codes": {},
                    "access_tokens": {},
                    "refresh_tokens": {},
                }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._fernet.encrypt(json.dumps(self._data).encode())
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent))
        closed = False
        try:
            os.write(tmp_fd, encrypted)
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            closed = True
            os.replace(tmp_path, str(self._path))
        except:
            if not closed:
                os.close(tmp_fd)
            os.unlink(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Registered clients (Dynamic Client Registration)
    # ------------------------------------------------------------------

    def store_client(self, client_id: str, client_data: dict) -> None:
        with self._lock:
            self._data["clients"][client_id] = client_data
            self._save()

    def get_client(self, client_id: str) -> dict | None:
        with self._lock:
            return self._data["clients"].get(client_id)

    # ------------------------------------------------------------------
    # Pending auth flows (authorize -> LinkedIn callback)
    # ------------------------------------------------------------------

    def store_pending_auth(self, state: str, auth_data: dict) -> None:
        with self._lock:
            self._data["pending_auth"][state] = auth_data
            self._save()

    def get_pending_auth(self, state: str) -> dict | None:
        with self._lock:
            data = self._data["pending_auth"].get(state)
            if data and data.get("expires_at", 0) < time.time():
                self._data["pending_auth"].pop(state, None)
                self._save()
                return None
            return data

    def delete_pending_auth(self, state: str) -> None:
        with self._lock:
            self._data["pending_auth"].pop(state, None)
            self._save()

    # ------------------------------------------------------------------
    # Authorization codes (our code -> linkedin token mapping)
    # ------------------------------------------------------------------

    def store_auth_code(self, code: str, code_data: dict) -> None:
        with self._lock:
            self._data["auth_codes"][code] = code_data
            self._save()

    def get_auth_code(self, code: str) -> dict | None:
        with self._lock:
            data = self._data["auth_codes"].get(code)
            if data and data.get("expires_at", 0) < time.time():
                self._data["auth_codes"].pop(code, None)
                self._save()
                return None
            return data

    def delete_auth_code(self, code: str) -> None:
        with self._lock:
            self._data["auth_codes"].pop(code, None)
            self._save()

    # ------------------------------------------------------------------
    # Access tokens
    # ------------------------------------------------------------------

    def store_access_token(self, token: str, token_data: dict) -> None:
        with self._lock:
            self._data["access_tokens"][token] = token_data
            self._save()

    def get_access_token(self, token: str) -> dict | None:
        with self._lock:
            data = self._data["access_tokens"].get(token)
            if data and data.get("expires_at") and data["expires_at"] < time.time():
                self._data["access_tokens"].pop(token, None)
                self._save()
                return None
            return data

    def delete_access_token(self, token: str) -> None:
        with self._lock:
            self._data["access_tokens"].pop(token, None)
            self._save()

    # ------------------------------------------------------------------
    # Refresh tokens
    # ------------------------------------------------------------------

    def store_refresh_token(self, token: str, token_data: dict) -> None:
        with self._lock:
            self._data["refresh_tokens"][token] = token_data
            self._save()

    def get_refresh_token(self, token: str) -> dict | None:
        with self._lock:
            return self._data["refresh_tokens"].get(token)

    def delete_refresh_token(self, token: str) -> None:
        with self._lock:
            self._data["refresh_tokens"].pop(token, None)
            self._save()

    # ------------------------------------------------------------------
    # Atomic token rotation
    # ------------------------------------------------------------------

    def rotate_tokens(
        self,
        old_access_token: str | None,
        old_refresh_token: str,
        new_access_token: str,
        new_access_data: dict,
        new_refresh_token: str,
        new_refresh_data: dict,
    ) -> None:
        """Atomically rotate access and refresh tokens."""
        with self._lock:
            if old_access_token:
                self._data["access_tokens"].pop(old_access_token, None)
            self._data["refresh_tokens"].pop(old_refresh_token, None)
            self._data["access_tokens"][new_access_token] = new_access_data
            self._data["refresh_tokens"][new_refresh_token] = new_refresh_data
            self._save()

    # ------------------------------------------------------------------
    # Bulk cleanup helpers
    # ------------------------------------------------------------------

    def delete_tokens_for_access_token(self, access_token: str) -> None:
        """Delete both access and associated refresh token."""
        with self._lock:
            token_data = self._data["access_tokens"].pop(access_token, None)
            if token_data and "refresh_token" in token_data:
                self._data["refresh_tokens"].pop(token_data["refresh_token"], None)
            self._save()

    def delete_tokens_for_refresh_token(self, refresh_token: str) -> None:
        """Delete both refresh and associated access token."""
        with self._lock:
            token_data = self._data["refresh_tokens"].pop(refresh_token, None)
            if token_data and "access_token" in token_data:
                self._data["access_tokens"].pop(token_data["access_token"], None)
            self._save()

    # ------------------------------------------------------------------
    # LinkedIn credential lookup (for publisher daemon)
    # ------------------------------------------------------------------

    def get_any_linkedin_token(self) -> tuple[str, str | None] | None:
        """Return (linkedin_access_token, linkedin_refresh_token) from any stored user, or None.

        Skips expired MCP tokens. In a multi-user deployment, returns the first
        valid token found — TODO: associate posts with specific user credentials.
        """
        with self._lock:
            now = time.time()
            for token_data in self._data["access_tokens"].values():
                if token_data.get("expires_at") and token_data["expires_at"] < now:
                    continue
                linkedin_token = token_data.get("linkedin_access_token")
                if linkedin_token:
                    return (linkedin_token, token_data.get("linkedin_refresh_token"))
        return None
