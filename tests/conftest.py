"""
Shared test fixtures and configuration.
"""

import pytest
import os


@pytest.fixture(autouse=True)
def no_real_api_calls(monkeypatch):
    """Prevent any test from accidentally hitting real APIs."""
    import urllib.request
    def blocked(*args, **kwargs):
        raise RuntimeError(
            "Real API calls are blocked in tests. Use mock fixtures instead."
        )
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


@pytest.fixture
def sample_conversation_id():
    return "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


@pytest.fixture
def sample_genesys_env():
    return {
        "GENESYS_CLIENT_ID": "test-client-id",
        "GENESYS_CLIENT_SECRET": "test-client-secret",
        "GENESYS_ENVIRONMENT": "mypurecloud.com",
    }
