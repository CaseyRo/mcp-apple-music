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

    # Mirrors (see mirrors.example.yaml). Off by default: adopting writes to
    # playlists people may follow, so turning the loop on is a deliberate step.
    mirrors_file: Path | None = None
    mirrors_enabled: bool = False
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
