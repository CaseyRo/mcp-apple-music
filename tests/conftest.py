"""Shared fixtures: a MusicKit-shaped key generated per run, never a real one."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


@pytest.fixture(scope="session")
def keypair() -> tuple[str, ec.EllipticCurvePublicKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture(autouse=True)
def isolated_state(request, tmp_path, monkeypatch):
    """Every test gets its own data dir and fresh server state; nothing touches ./data.

    Live tests are the exception: they need the real data dir and its user token.
    """
    from mcp_apple_music import server as srv
    from mcp_apple_music.config import settings

    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr(settings, "data_dir", tmp_path)
    cached = (
        srv.store,
        srv.developer_token,
        srv.apple_api,
        srv.apple_web,
        srv.library,
        srv.mirrors,
        srv.spotify,
        srv.engine,
        srv.scheduler,
    )
    for f in cached:
        f.cache_clear()
    yield
    if srv.store.cache_info().currsize:
        srv.store().close()
    for f in cached:
        f.cache_clear()
