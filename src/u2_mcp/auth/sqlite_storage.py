"""Durable OAuth state, so a restart does not sign everyone out.

The in-memory store loses every registration, token and in-flight login when the
process stops, and two instances behind a load balancer cannot recognise each
other's tokens. This backend keeps the same behaviour but puts the state in a
SQLite file: it survives restarts, and several workers on one host share it.

Bearer tokens are stored as SHA-256 hashes, never in the clear. A stolen database
file therefore yields no usable credentials -- the same reason passwords are not
stored in the clear.

SQLite is in the standard library, so this adds no dependency. For deployments
spread across hosts, the same interface is the seam a Redis or Postgres backend
would implement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .storage import (
    PendingAuthorization,
    StoredAuthCode,
    StoredClient,
    StoredToken,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id     TEXT PRIMARY KEY,
    redirect_key  TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clients_redirect_key ON clients (redirect_key);

CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash   TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT NOT NULL,
    token_type  TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    expires_at  REAL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (token_hash, token_type)
);
CREATE INDEX IF NOT EXISTS tokens_client ON tokens (client_id);

CREATE TABLE IF NOT EXISTS pending_auth (
    state_hash  TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL,
    payload     TEXT NOT NULL
);
"""


def hash_secret(value: str) -> str:
    """Return the SHA-256 hex digest used as a lookup key for a secret.

    Args:
        value: The bearer token, authorization code or state parameter

    Returns:
        Hex digest safe to store and to index on
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteAuthStorage:
    """OAuth state kept in a SQLite file, with tokens stored as hashes.

    Args:
        db_path: Path to the database file; parent directories are created
        cleanup_interval: Seconds between automatic purges of expired rows
    """

    def __init__(self, db_path: Path | str, cleanup_interval: int = 300) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(self.db_path.parent)
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        _restrict_to_owner(self.db_path)
        logger.info(f"OAuth state persisted to {self.db_path}")

    # -- plumbing -----------------------------------------------------------

    def _write(self, sql: str, parameters: tuple[Any, ...] = ()) -> int:
        """Run a statement that changes rows and return how many it changed."""
        with self._lock:
            cursor = self._connection.execute(sql, parameters)
            self._connection.commit()
            return cursor.rowcount

    def _read_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Run a query expected to match at most one row."""
        with self._lock:
            row: sqlite3.Row | None = self._connection.execute(sql, parameters).fetchone()
            return row

    def _claim_one(self, table: str, key_column: str, key_hash: str) -> sqlite3.Row | None:
        """Read a single-use row and delete it in one indivisible step.

        Reading and then deleting as two operations leaves a window in which two
        threads both read the row before either deletes it -- which would let one
        authorization code be exchanged twice. `DELETE ... RETURNING` makes the
        claim atomic; the lock additionally serializes writers on this connection.

        Args:
            table: Table holding single-use rows
            key_column: Hashed-key column to match on
            key_hash: The hashed key to claim

        Returns:
            The claimed row, or None if another caller claimed it first
        """
        with self._lock:
            cursor = self._connection.execute(
                f"DELETE FROM {table} WHERE {key_column} = ? RETURNING payload",  # noqa: S608
                (key_hash,),
            )
            row: sqlite3.Row | None = cursor.fetchone()
            self._connection.commit()
            return row

    def _maybe_cleanup(self) -> None:
        """Purge expired rows if enough time has passed since the last purge."""
        if time.time() - self._last_cleanup < self._cleanup_interval:
            return
        self.cleanup()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._connection.close()

    # -- clients ------------------------------------------------------------

    def store_client(self, client: StoredClient) -> None:
        """Store a registered client."""
        self._write(
            "INSERT OR REPLACE INTO clients (client_id, redirect_key, payload) VALUES (?, ?, ?)",
            (client.client_id, _redirect_key(client.redirect_uris), json.dumps(asdict(client))),
        )
        logger.info(f"Stored client: {client.client_id} ({client.client_name})")

    def get_client(self, client_id: str) -> StoredClient | None:
        """Retrieve a client by ID."""
        row = self._read_one("SELECT payload FROM clients WHERE client_id = ?", (client_id,))
        return StoredClient(**json.loads(row["payload"])) if row else None

    def find_client_by_redirect_uris(self, redirect_uris: list[str]) -> StoredClient | None:
        """Find an existing client with exactly these redirect URIs."""
        row = self._read_one(
            "SELECT payload FROM clients WHERE redirect_key = ?", (_redirect_key(redirect_uris),)
        )
        return StoredClient(**json.loads(row["payload"])) if row else None

    def delete_client(self, client_id: str) -> bool:
        """Delete a client and revoke everything issued to it."""
        deleted = self._write("DELETE FROM clients WHERE client_id = ?", (client_id,))
        if deleted:
            self._write("DELETE FROM tokens WHERE client_id = ?", (client_id,))
            logger.info(f"Deleted client: {client_id}")
        return bool(deleted)

    def validate_client_credentials(
        self, client_id: str, client_secret: str | None
    ) -> StoredClient | None:
        """Validate client credentials, comparing secrets in constant time."""
        client = self.get_client(client_id)
        if client is None:
            return None
        if client.client_secret is None:
            return client
        if secrets.compare_digest(client.client_secret or "", client_secret or ""):
            return client
        return None

    # -- authorization codes ------------------------------------------------

    def store_auth_code(self, auth_code: StoredAuthCode) -> None:
        """Store an authorization code, keyed by its hash."""
        self._maybe_cleanup()
        self._write(
            "INSERT OR REPLACE INTO auth_codes (code_hash, expires_at, payload) VALUES (?, ?, ?)",
            (hash_secret(auth_code.code), auth_code.expires_at, json.dumps(asdict(auth_code))),
        )

    def get_auth_code(self, code: str) -> StoredAuthCode | None:
        """Retrieve and consume an authorization code (one-time use)."""
        row = self._claim_one("auth_codes", "code_hash", hash_secret(code))
        if row is None:
            return None
        stored = StoredAuthCode(**json.loads(row["payload"]))
        if time.time() > stored.expires_at:
            logger.debug("Auth code expired")
            return None
        return stored

    # -- tokens -------------------------------------------------------------

    def _store_token(self, token: StoredToken, token_type: str) -> None:
        """Store a token under its hash, so the raw value never reaches disk."""
        self._maybe_cleanup()
        payload = asdict(token)
        payload["token"] = ""  # the caller already holds it; disk must not
        self._write(
            "INSERT OR REPLACE INTO tokens "
            "(token_hash, token_type, client_id, expires_at, payload) VALUES (?, ?, ?, ?, ?)",
            (
                hash_secret(token.token),
                token_type,
                token.client_id,
                token.expires_at,
                json.dumps(payload),
            ),
        )

    def _get_token(self, token: str, token_type: str) -> StoredToken | None:
        """Retrieve a token by hash, dropping it if it has expired."""
        token_hash = hash_secret(token)
        row = self._read_one(
            "SELECT payload FROM tokens WHERE token_hash = ? AND token_type = ?",
            (token_hash, token_type),
        )
        if row is None:
            return None
        stored = StoredToken(**json.loads(row["payload"]))
        if stored.expires_at and time.time() > stored.expires_at:
            self._revoke_token(token, token_type)
            logger.debug(f"{token_type.capitalize()} token expired")
            return None
        # The raw value is not on disk, so put it back for the caller.
        stored.token = token
        return stored

    def _revoke_token(self, token: str, token_type: str) -> bool:
        """Delete a token row."""
        return bool(
            self._write(
                "DELETE FROM tokens WHERE token_hash = ? AND token_type = ?",
                (hash_secret(token), token_type),
            )
        )

    def store_access_token(self, token: StoredToken) -> None:
        """Store an access token."""
        self._store_token(token, "access")
        logger.debug(f"Stored access token for user {token.user_subject}")

    def get_access_token(self, token: str) -> StoredToken | None:
        """Retrieve an access token."""
        return self._get_token(token, "access")

    def revoke_access_token(self, token: str) -> bool:
        """Revoke an access token."""
        return self._revoke_token(token, "access")

    def store_refresh_token(self, token: StoredToken) -> None:
        """Store a refresh token."""
        self._store_token(token, "refresh")

    def get_refresh_token(self, token: str) -> StoredToken | None:
        """Retrieve a refresh token."""
        return self._get_token(token, "refresh")

    def revoke_refresh_token(self, token: str) -> bool:
        """Revoke a refresh token."""
        return self._revoke_token(token, "refresh")

    # -- pending authorization ----------------------------------------------

    def store_pending_auth(self, pending: PendingAuthorization) -> None:
        """Store in-flight authorization state for the identity provider round trip."""
        self._maybe_cleanup()
        self._write(
            "INSERT OR REPLACE INTO pending_auth (state_hash, expires_at, payload) "
            "VALUES (?, ?, ?)",
            (hash_secret(pending.state), pending.expires_at, json.dumps(asdict(pending))),
        )

    def get_pending_auth(self, state: str) -> PendingAuthorization | None:
        """Retrieve and consume pending authorization state (one-time use)."""
        row = self._claim_one("pending_auth", "state_hash", hash_secret(state))
        if row is None:
            return None
        stored = PendingAuthorization(**json.loads(row["payload"]))
        if time.time() > stored.expires_at:
            logger.debug("Pending auth expired")
            return None
        return stored

    # -- housekeeping -------------------------------------------------------

    def cleanup(self) -> None:
        """Remove every expired row."""
        now = time.time()
        removed = self._write("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
        removed += self._write(
            "DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
        )
        removed += self._write("DELETE FROM pending_auth WHERE expires_at < ?", (now,))
        self._last_cleanup = now
        if removed:
            logger.debug(f"Cleaned up {removed} expired auth entries")

    def get_stats(self) -> dict[str, int]:
        """Get storage statistics."""
        with self._lock:
            counts = {
                "clients": "SELECT COUNT(*) FROM clients",
                "auth_codes": "SELECT COUNT(*) FROM auth_codes",
                "access_tokens": "SELECT COUNT(*) FROM tokens WHERE token_type = 'access'",
                "refresh_tokens": "SELECT COUNT(*) FROM tokens WHERE token_type = 'refresh'",
                "pending_auth": "SELECT COUNT(*) FROM pending_auth",
            }
            return {
                name: int(self._connection.execute(sql).fetchone()[0])
                for name, sql in counts.items()
            }


def _restrict_to_owner(path: Path) -> None:
    """Restrict a file or directory holding credentials to its owner.

    On POSIX this is enforced with permission bits. Windows ignores them, so the
    caller must rely on directory ACLs there; the attempt is still made rather
    than skipped, and a failure is logged rather than raised, because failing to
    tighten permissions must not stop the server from starting.

    Args:
        path: File or directory to restrict
    """
    mode = 0o700 if path.is_dir() else 0o600
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.warning(f"Could not restrict permissions on {path}: {exc}")


def _redirect_key(redirect_uris: list[str]) -> str:
    """Return an order-independent key for a client's redirect URIs."""
    return "\n".join(sorted(redirect_uris))
