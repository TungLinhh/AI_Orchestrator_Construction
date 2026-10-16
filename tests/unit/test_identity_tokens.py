"""Malformed signed credentials must be refused before tenant binding."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from ai_orchestrator.config.settings import Settings
from ai_orchestrator.domain.errors import AuthorizationError
from ai_orchestrator.security.auth import ALGORITHM, AUDIENCE, ISSUER, principal_from_token

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("org", None),
        ("org", ""),
        ("org", "  "),
        ("org", 123),
        ("org", {"id": "org_example"}),
        ("org", "x" * 41),
        ("org", "org_short"),
        ("org", "x" * 30),
        ("sub", ""),
        ("sub", 123),
        ("tv", None),
        ("tv", True),
        ("tv", "1"),
        ("tv", 0),
        ("tv", -1),
        ("tv", 1.5),
    ],
)
def test_malformed_identity_claim_is_a_credential_refusal(claim, value):
    settings = Settings(jwt_secret="synthetic-unit-test-key-never-deploy")
    now = datetime.now(UTC)
    claims = {
        "sub": "usr_synthetic",
        "org": "org_synthetic",
        "tv": 1,
        "iat": now,
        "exp": now + timedelta(minutes=1),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    if value is None:
        claims.pop(claim)
    else:
        claims[claim] = value
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    with pytest.raises(AuthorizationError):
        principal_from_token(token, settings)
