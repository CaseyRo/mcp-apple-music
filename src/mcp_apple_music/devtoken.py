"""Apple Music developer token: an ES256 JWT minted from the MusicKit key.

Apple caps a developer token at 15,777,000 seconds (about six months). The token is
re-minted on first use inside the renewal window, so no request ever carries an
expired one and nobody has to rotate it by hand.
"""

from __future__ import annotations

import re
import textwrap
import time
from collections.abc import Callable

import jwt

MAX_TTL_SECONDS = 15_777_000

_PEM_ARMOR = re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----")


def normalize_pem(raw: str) -> str:
    """Rebuild a PKCS#8 PEM from the `.p8` contents in whatever shape the env gave it.

    Env stores often flatten multi-line values (Komodo splits on newlines), so this
    accepts the file verbatim, with literal ``\\n`` escapes, or as the bare base64 body.
    """
    body = re.sub(r"\s+", "", _PEM_ARMOR.sub("", raw.replace("\\n", "\n")))
    if not body:
        raise ValueError("MusicKit private key is empty")
    lines = "\n".join(textwrap.wrap(body, 64))
    return f"-----BEGIN PRIVATE KEY-----\n{lines}\n-----END PRIVATE KEY-----\n"


class DeveloperToken:
    """Mints and caches the developer token; `get()` always returns a usable one."""

    def __init__(
        self,
        team_id: str,
        key_id: str,
        private_key: str,
        ttl_seconds: int = 7 * 86_400,
        renew_before_seconds: int = 86_400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be in 1..{MAX_TTL_SECONDS}")
        if not 0 <= renew_before_seconds < ttl_seconds:
            raise ValueError("renew_before_seconds must be smaller than ttl_seconds")
        if not team_id or not key_id:
            raise ValueError("Team ID and Key ID are required")
        self._team_id = team_id
        self._key_id = key_id
        self._key = normalize_pem(private_key)
        self._ttl = ttl_seconds
        self._renew_before = renew_before_seconds
        self._clock = clock
        self._token: str | None = None
        self.expires_at: int = 0
        # Fail at startup on a malformed key, not on the first Apple request.
        self.get()

    def get(self) -> str:
        now = int(self._clock())
        if self._token is None or now >= self.expires_at - self._renew_before:
            self.expires_at = now + self._ttl
            self._token = jwt.encode(
                {"iss": self._team_id, "iat": now, "exp": self.expires_at},
                self._key,
                algorithm="ES256",
                headers={"kid": self._key_id},
            )
        return self._token
