"""Configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server transport
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    # Bearer token auth for the MCP endpoint
    mcp_api_key: SecretStr = SecretStr("")

    # Base URL sign-in links point at; defaults to http://127.0.0.1:<port>.
    public_url: str = ""

    # All mutable state (SQLite) lives here; mount a volume on it in Docker.
    data_dir: Path = Path("data")

    # MusicKit key from the Apple Developer portal. The `.p8` contents may be given
    # verbatim, with `\n` escapes, or as the bare base64 body.
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: SecretStr = SecretStr("")
    apple_token_ttl_seconds: int = 7 * 86_400
    apple_token_renew_before_seconds: int = 86_400
    # Apple doesn't document the Music User Token's lifetime (reports: days to ~6
    # months); /health warns from this age, a month before the longest reports.
    apple_user_token_warn_days: int = 150
    # Daily end-to-end check of both rails on a scratch playlist; 0 turns it off.
    canary_interval_seconds: int = 86_400

    # Mirrors (see mirrors.example.yaml). Off by default: adopting writes to
    # playlists people may follow, so turning the loop on is a deliberate step.
    mirrors_file: Path | None = None
    # The same YAML inline (flow style fits one line), for hosts that hold
    # config in environment variables. MIRRORS_FILE wins when both are set.
    mirrors_config: str = ""
    mirrors_enabled: bool = False
    # Received music per hour of wall time below this flags possibly missed tracks.
    mirror_coverage_threshold: float = 0.9
    # POST /hooks/<secret> exists only when this is set.
    webhook_secret: SecretStr = SecretStr("")
    # A Spotify app (client credentials) gives each track's ISRC; without it,
    # mirrors match on title and artists only.
    spotify_client_id: str = ""
    spotify_client_secret: SecretStr = SecretStr("")

    model_config = {"env_prefix": "", "case_sensitive": False}

    @model_validator(mode="after")
    def require_api_key_for_http(self) -> "Settings":
        if self.transport == "http" and not self.mcp_api_key.get_secret_value():
            raise ValueError(
                "MCP_API_KEY is required when TRANSPORT=http. "
                "Refusing to start an unauthenticated server."
            )
        return self


settings = Settings()
