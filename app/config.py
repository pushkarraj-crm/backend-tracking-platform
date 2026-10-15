from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal
from urllib.parse import urlsplit
from cryptography.fernet import Fernet


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: str
    jwt_secret: str = Field(min_length=32)
    token_minutes: int = Field(default=30, ge=1, le=120)
    public_base_url: str = "http://localhost:8000"
    frontend_base_url: str = "http://localhost:3000"
    terms_url: str = ""
    privacy_url: str = ""
    legal_version: str = "1"
    cors_origins: list[str] = []
    environment: Literal["development", "test", "production"] = "development"
    auto_create_tables: bool = False
    rate_limit_per_minute: int = Field(default=120, ge=1)
    auth_rate_per_minute: int = Field(default=10, ge=1, le=100)
    tracking_rate_per_minute: int = Field(default=10000, ge=1)
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout: int = Field(default=10, ge=1, le=120)
    db_statement_timeout_ms: int = Field(default=10000, ge=1000, le=60000)
    db_schema: str = Field(default="", pattern=r"^[a-z0-9_]*$", max_length=63)
    redis_url: str = ""
    encryption_key: str = ""
    metrics_token: str = ""
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    max_request_bytes: int = Field(default=65536, ge=1024, le=1048576)
    refresh_days: int = Field(default=7, ge=1, le=30)
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    require_email_verification: bool = False
    jwt_previous_secret: str = ""
    allow_registration: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    outbound_allowed_hosts: list[str] = []
    worker_poll_seconds: int = Field(default=2, ge=1, le=30)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_plans: dict[str, str] = {}
    geoip_database_path: str = ""
    tracking_cname: str = ""
    domain_certificate_token: str = ""
    trusted_gateway_ip: str = "172.30.0.2"
    billing_grace_hours: int = Field(default=72, ge=0, le=168)
    signup_trial_days: Literal[15] = 15
    plan_limits: dict[str, dict[str, int]] = {
        "trial": {"users": 10, "offers": 25},
        "starter": {"users": 100, "offers": 500},
        "business": {"users": 1000, "offers": 5000},
    }

    @model_validator(mode="after")
    def production_config(self):
        if self.encryption_key:
            try:
                Fernet(self.encryption_key.encode())
            except (ValueError, TypeError):
                raise ValueError("ENCRYPTION_KEY must be a valid Fernet key")
        if self.cookie_samesite == "none" and self.environment != "production":
            raise ValueError("Cross-site cookies require production HTTPS")
        if self.db_schema and self.environment != "test":
            raise ValueError("Isolated schema overrides are for test mode only")
        if not self.database_url.startswith("postgresql+psycopg://"):
            if not (self.environment == "test" and self.database_url.startswith("sqlite")):
                raise ValueError("DATABASE_URL must use postgresql+psycopg://")
        if self.environment != "test" and self.auto_create_tables:
            raise ValueError("Use Alembic migrations instead of AUTO_CREATE_TABLES")
        if self.environment == "production":
            if not self.public_base_url.startswith("https://"):
                raise ValueError("Production public_base_url requires HTTPS")
            if not self.frontend_base_url.startswith("https://"):
                raise ValueError("Production frontend_base_url requires HTTPS")
            if self.allow_registration and (
                not self.terms_url.startswith("https://")
                or not self.privacy_url.startswith("https://")
            ):
                raise ValueError("Public signup requires HTTPS terms and privacy URLs")
            if self.database_url.startswith("sqlite") or self.auto_create_tables:
                raise ValueError("Production requires a server database and migrations")
            if "*" in self.cors_origins:
                raise ValueError("Production CORS requires explicit origins")
            if not self.redis_url or not self.encryption_key or len(self.metrics_token) < 32:
                raise ValueError(
                    "Production requires Redis, encryption key and a strong metrics token"
                )
            if not self.require_email_verification or not self.smtp_host or not self.smtp_from:
                raise ValueError("Production requires verified email and SMTP")
            if "*" in self.allowed_hosts or not self.allowed_hosts:
                raise ValueError("Production requires explicit allowed hosts")
            if "replace-" in self.jwt_secret or len(self.jwt_secret) < 48:
                raise ValueError("Production JWT secret must be random and at least 48 characters")
            if urlsplit(self.public_base_url).hostname not in self.allowed_hosts:
                raise ValueError("Public URL host must be allowed")
            if (
                not self.razorpay_key_id.startswith("rzp_")
                or not self.razorpay_key_secret
                or not self.razorpay_webhook_secret
                or not self.razorpay_plans
            ):
                raise ValueError(
                    "Production requires Razorpay credentials, webhook secret and plan IDs"
                )
        return self


@lru_cache
def get_settings():
    return Settings()
