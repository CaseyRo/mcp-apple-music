"""Developer token minting, on a key generated per test run."""

from __future__ import annotations

import jwt
import pytest

from mcp_apple_music.devtoken import MAX_TTL_SECONDS, DeveloperToken, normalize_pem


class Clock:
    def __init__(self, now: float = 1_800_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def decode(token: str, public_key) -> tuple[dict, dict]:
    claims = jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    return jwt.get_unverified_header(token), claims


def test_claims_and_header(keypair):
    pem, public = keypair
    clock = Clock()
    tok = DeveloperToken(
        "TEAM123", "KEY456", pem, ttl_seconds=3600, clock=clock, renew_before_seconds=60
    )
    header, claims = decode(tok.get(), public)
    assert header == {"alg": "ES256", "kid": "KEY456", "typ": "JWT"}
    assert claims == {"iss": "TEAM123", "iat": 1_800_000_000, "exp": 1_800_003_600}
    assert tok.expires_at == 1_800_003_600


def test_ttl_capped_at_apple_maximum(keypair):
    pem, public = keypair
    tok = DeveloperToken("T", "K", pem, ttl_seconds=MAX_TTL_SECONDS, clock=Clock())
    _, claims = decode(tok.get(), public)
    assert claims["exp"] - claims["iat"] == MAX_TTL_SECONDS
    with pytest.raises(ValueError, match="ttl_seconds"):
        DeveloperToken("T", "K", pem, ttl_seconds=MAX_TTL_SECONDS + 1)


def test_renewal_timing(keypair):
    pem, public = keypair
    clock = Clock()
    tok = DeveloperToken(
        "T", "K", pem, ttl_seconds=1000, renew_before_seconds=100, clock=clock
    )
    first = tok.get()

    clock.now += 899  # one second before the renewal window opens
    assert tok.get() == first

    clock.now += 1  # window open: re-minted, and still valid when handed out
    second = tok.get()
    assert second != first
    _, claims = decode(second, public)
    assert claims["iat"] == int(clock.now)
    assert claims["exp"] > clock.now


@pytest.mark.parametrize(
    "shape",
    [
        lambda pem: pem,
        lambda pem: pem.replace("\n", "\\n"),  # escaped newlines
        lambda pem: pem.replace("\n", " "),  # newlines collapsed by an env store
        lambda pem: "".join(pem.splitlines()[1:-1]),  # bare base64 body
    ],
    ids=["verbatim", "escaped", "collapsed", "body-only"],
)
def test_key_accepted_in_env_shapes(keypair, shape):
    pem, public = keypair
    assert normalize_pem(shape(pem)) == pem
    decode(DeveloperToken("T", "K", shape(pem)).get(), public)


def test_bad_config_fails_at_construction(keypair):
    pem, _ = keypair
    with pytest.raises(ValueError):
        DeveloperToken("T", "K", "")
    with pytest.raises(ValueError):
        DeveloperToken("", "K", pem)
    with pytest.raises(ValueError):
        DeveloperToken("T", "K", pem, ttl_seconds=100, renew_before_seconds=100)
    with pytest.raises(Exception):  # not a key at all
        DeveloperToken("T", "K", "bm90IGEga2V5")
