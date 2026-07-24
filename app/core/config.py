"""
Centralized, typed configuration.

Everything tunable about the auditor's behavior lives here so it's not
scattered as magic numbers through the codebase, and so it can be overridden
by environment variables in different deploy environments without code changes.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Page Pulse"
    app_version: str = "1.0.0"

    # Networking behavior for outbound fetches.
    fetch_timeout_seconds: float = 8.0
    max_redirects: int = 5
    max_content_bytes: int = 5 * 1024 * 1024  # 5 MB cap, avoids huge/streamed pages
    user_agent: str = "PagePulse/1.0 (+https://digitalheroesco.com; audit bot)"

    # CORS: "*" is fine for this tool since it has no auth/secrets and is
    # meant to be called from any static frontend origin.
    cors_allow_origins: list[str] = ["*"]

    class Config:
        env_prefix = "PAGEPULSE_"


settings = Settings()
