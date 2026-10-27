"""GitHub App and token auth."""

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from swarm.sources.github import auth


@pytest.fixture(scope="module")
def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


def test_app_jwt_claims(key_pair):
    pem, public = key_pair
    app = auth.AppAuth("12345", pem, "4242", now=lambda: 1_800_000_000)
    claims = jwt.decode(app.app_jwt(), public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iss": "12345", "iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540}


def test_installation_token_is_cached_until_near_expiry(key_pair):
    pem, public = key_pair
    calls = []

    def handler(request):
        calls.append(request)
        jwt.decode(request.headers["authorization"].split()[1], public, algorithms=["RS256"],
                   options={"verify_exp": False, "verify_iat": False})
        return httpx.Response(201, json={"token": f"inst-{len(calls)}", "expires_at": "2027-01-15T09:00:00Z"})

    clock = [1_800_000_000.0]          # 2027-01-15T08:00:00Z
    app = auth.AppAuth("12345", pem, "4242", transport=httpx.MockTransport(handler), now=lambda: clock[0])
    assert app.headers() == {"Authorization": "Bearer inst-1"}
    clock[0] += 1800
    assert app.headers() == {"Authorization": "Bearer inst-1"}
    clock[0] += 1800 - 30              # within a minute of expiry: refresh
    assert app.headers() == {"Authorization": "Bearer inst-2"}
    assert [c.url.path for c in calls] == ["/app/installations/4242/access_tokens"] * 2


def test_from_env_picks_the_mode(tmp_path, key_pair):
    key_file = tmp_path / "app.pem"
    key_file.write_text(key_pair[0])
    app = auth.from_env({"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY_PATH": str(key_file),
                         "GITHUB_INSTALLATION_ID": "2"})
    assert app.mode == "app"
    assert auth.from_env({"GITHUB_TOKEN": "t"}).headers() == {"Authorization": "Bearer t"}
    with pytest.raises(SystemExit):
        auth.from_env({})
