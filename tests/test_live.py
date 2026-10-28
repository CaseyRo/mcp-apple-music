"""Against the real Apple Music account. Opt-in: TEST_LIVE=1 uv run pytest -m live -s

Needs the MusicKit key in the environment and a signed-in user token in DATA_DIR
(`mcp-apple-music sign-in-link apple`). Creates and deletes a scratch playlist.
"""

from __future__ import annotations

import os

import pytest

import mcp_apple_music.server as srv
from mcp_apple_music import canary

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("TEST_LIVE") != "1", reason="set TEST_LIVE=1"),
]


async def test_canary_round_trip():
    result = await canary.run(srv.library())
    print("\n".join(result.steps))
    assert result.ok, result.error
