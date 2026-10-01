"""The service-credential headers an API test must send, defined once.

`Authorization: Bearer svc.<secret>` — **the scheme is not optional.**

`security/auth.py` reads the credential through FastAPI's `HTTPBearer`, which returns
`None` for an `Authorization` header with no `Bearer` scheme, and `authenticate` then
answers 403 `missing_credentials` before it looks at the value at all.

`test_task_api_and_ui.py` sent the bare `svc.<secret>` for a long time and its 21 tests
passed, because its only HTTP calls are to `GET /api/v1/ui`, which is unauthenticated,
and its `_create` helper -- the one that would have caught it -- was defined and never
called. A fixture that cannot authenticate anything, in a green suite, is a fixture
that has only ever been trusted. F123.

The `client` fixture itself lives in `tests/integration/conftest.py`.
"""

from __future__ import annotations

from tests.integration.conftest import TEST_SECRET


def auth_headers(organization_id: str) -> dict[str, str]:
    """Headers for a service call as `authenticate` expects them."""
    return {
        "Authorization": f"Bearer svc.{TEST_SECRET}",
        "x-organization-id": organization_id,
        "Content-Type": "application/json",
    }


__all__ = ["TEST_SECRET", "auth_headers"]
