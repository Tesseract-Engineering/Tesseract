"""Drive service-account credentials: the signed request, token caching."""

import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from swarm.sources.gdrive.auth import SCOPE, TOKEN_URI, ServiceAccountAuth, from_env


@pytest.fixture(scope="module")
def key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return {"type": "service_account", "client_email": "reader@acme-swarm.iam.gserviceaccount.com",
            "private_key": pem, "token_uri": TOKEN_URI}, private.public_key()


def test_the_assertion_asks_for_read_only_drive(key):
    info, public = key
    auth = ServiceAccountAuth(info, now=lambda: 1_000_000.0)
    claims = jwt.decode(auth.assertion(), public, algorithms=["RS256"], audience=TOKEN_URI,
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iss": info["client_email"], "scope": SCOPE, "aud": TOKEN_URI,
                      "iat": 1_000_000, "exp": 1_003_600}
    assert SCOPE.endswith("/drive.readonly")


def test_tokens_are_cached_until_a_minute_before_expiry(key):
    info, _ = key
    clock = [1_000_000.0]
    posts = []

    def handler(request):
        posts.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": f"token-{len(posts)}", "expires_in": 3599,
                                         "token_type": "Bearer"})

    auth = ServiceAccountAuth(info, transport=httpx.MockTransport(handler), now=lambda: clock[0])
    assert auth.headers() == {"Authorization": "Bearer token-1"}
    clock[0] += 3000
    assert auth.headers() == {"Authorization": "Bearer token-1"}
    clock[0] += 600
    assert auth.headers() == {"Authorization": "Bearer token-2"}
    assert posts[0]["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]


def test_from_env_reads_a_key_file_or_inline_json(key, tmp_path):
    info, _ = key
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps(info))
    assert from_env({"GDRIVE_SERVICE_ACCOUNT_KEY_PATH": str(path)}).email == info["client_email"]
    assert from_env({"GDRIVE_SERVICE_ACCOUNT_KEY": json.dumps(info)}).email == info["client_email"]
    with pytest.raises(SystemExit, match="GDRIVE_SERVICE_ACCOUNT_KEY_PATH"):
        from_env({})
    with pytest.raises(SystemExit, match="credentials missing"):
        from_env({"GDRIVE_SERVICE_ACCOUNT_KEY_PATH": str(tmp_path / "not-there.json")})
