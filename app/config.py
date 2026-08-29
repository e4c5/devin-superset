"""Environment configuration. Fails fast on missing required values."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Config:
    devin_token: str
    devin_org_id: str
    devin_repository_id: str
    devin_api_base: str

    github_webhook_secret: str
    github_token: str
    target_repository: str

    max_acu_limit: int
    autofix_label: str
    bypass_approval: bool

    database_path: str

    @property
    def sessions_url(self) -> str:
        return f"{self.devin_api_base}/v3/organizations/{self.devin_org_id}/sessions"

    @classmethod
    def from_env(cls) -> "Config":
        try:
            max_acu = int(_optional("MAX_ACU_LIMIT", "10"))
        except ValueError as exc:
            raise ConfigError("MAX_ACU_LIMIT must be an integer") from exc

        cfg = cls(
            devin_token=_require("DEVIN_SERVICE_USER_TOKEN"),
            devin_org_id=_require("DEVIN_ORG_ID"),
            devin_repository_id=_require("DEVIN_REPOSITORY_ID"),
            devin_api_base=_optional("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/"),
            github_webhook_secret=_require("GITHUB_WEBHOOK_SECRET"),
            github_token=_require("GITHUB_TOKEN"),
            target_repository=_require("TARGET_REPOSITORY"),
            max_acu_limit=max_acu,
            autofix_label=_optional("DEVIN_AUTOFIX_LABEL", "devin-autofix"),
            bypass_approval=_optional("BYPASS_APPROVAL", "true").lower() == "true",
            database_path=_optional("DATABASE_PATH", "/app/data/ops_guard.db"),
        )
        return cfg


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config
