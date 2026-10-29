import base64
import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from scripts.render_start import prepare_environment


def staging(**overrides):
    values = dict(
        _env_file=None,
        environment="staging",
        database_url="postgresql+psycopg://user:pass@localhost/preview",
        jwt_secret=secrets.token_urlsafe(32),
        encryption_key=base64.b64encode(secrets.token_bytes(32)).decode(),
        metrics_token=secrets.token_urlsafe(32),
        redis_url="redis://localhost:6379",
        public_base_url="https://api.example.com",
        frontend_base_url="https://app.example.com",
        allowed_hosts=["api.example.com"],
        cors_origins=["https://app.example.com"],
    )
    values.update(overrides)
    return Settings(**values)


def test_staging_requires_real_database_https_and_secret_configuration():
    assert staging().environment == "staging"
    for overrides in [
        dict(redis_url=""),
        dict(encryption_key=""),
        dict(public_base_url="http://api.example.com"),
        dict(cors_origins=["*"]),
        dict(database_url="sqlite:///preview.db"),
    ]:
        with pytest.raises(ValidationError):
            staging(**overrides)


def test_staging_rejects_live_billing_keys_and_production_still_requires_smtp():
    with pytest.raises(ValidationError):
        staging(razorpay_key_id="rzp_live_example")
    assert staging(razorpay_key_id="rzp_test_example").razorpay_key_id.startswith("rzp_test_")
    with pytest.raises(ValidationError):
        staging(environment="production", allow_registration=False)


def test_render_environment_maps_provider_urls_without_changing_secret_values():
    env = {
        "DATABASE_URL": "postgresql://user:password@db/preview",
        "RENDER_EXTERNAL_URL": "https://api.example.com",
        "FRONTEND_BASE_URL": "https://app.example.com",
        "JWT_SECRET": "unchanged-secret",
    }
    prepare_environment(env)
    assert env["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert env["JWT_SECRET"] == "unchanged-secret"
    assert env["ALLOWED_HOSTS"] == '["api.example.com"]'
    assert env["CORS_ORIGINS"] == '["https://app.example.com"]'
