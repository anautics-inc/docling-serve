"""Auth behavior for the shared ``APIKeyAuth`` dependency (Article VI.1).

Covers the two regimes that matter for fail-closed/fail-open semantics:
  * no key configured  -> the comparison is permissive (auth effectively open;
    the fail-closed guard lives in settings, not here);
  * key configured      -> only an exact match validates.

These exercise the pure ``_validate_api_key`` coroutine (no network, no FastAPI
request plumbing). They run as pytest-asyncio tests (``asyncio_mode = auto``) so
the event loop is managed per-test and not closed globally — calling
``asyncio.run`` here would unset the loop and break session-scoped loop fixtures
in other test modules.
"""

import pytest

from docling_serve.auth import APIKeyAuth


@pytest.mark.asyncio
async def test_unset_key_accepts_any_provided_header():
    auth = APIKeyAuth("")

    assert (await auth._validate_api_key("anything")).valid is True
    assert (await auth._validate_api_key("")).valid is True
    # A missing header still reports invalid, but __call__ does not raise when no
    # key is configured, so the endpoint stays open (upstream behavior).
    assert (await auth._validate_api_key(None)).valid is False


@pytest.mark.asyncio
async def test_set_key_requires_exact_match():
    auth = APIKeyAuth("s3cr3t")

    assert (await auth._validate_api_key("s3cr3t")).valid is True
    # Surrounding whitespace is stripped before comparison.
    assert (await auth._validate_api_key("  s3cr3t  ")).valid is True

    assert (await auth._validate_api_key("wrong")).valid is False
    assert (await auth._validate_api_key(None)).valid is False
