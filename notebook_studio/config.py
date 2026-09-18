from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    password: str
    modal_enabled: bool
    modal_app_name: str
    modal_volume_name: str
    monthly_budget_usd: float
    safety_buffer_usd: float
    max_session_hours: float
    default_idle_timeout_minutes: int
    upload_limit_bytes: int
    database_path: Path
    app_public: bool
    allowed_origins: tuple[str, ...] = ()
    multi_user_mode: bool = False
    credential_encryption_key: str = ""
    admin_provisioning_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        upload_gib = max(0.01, float(os.getenv("UPLOAD_LIMIT_GIB", "4")))
        return cls(
            password=os.getenv("APP_PASSWORD", ""),
            modal_enabled=_bool("MODAL_ENABLED"),
            modal_app_name=os.getenv("MODAL_APP_NAME", "personal-notebook-studio"),
            modal_volume_name=os.getenv(
                "MODAL_VOLUME_NAME", "personal-notebook-studio-workspace"
            ),
            monthly_budget_usd=max(0.0, float(os.getenv("MONTHLY_COMPUTE_BUDGET_USD", "30"))),
            safety_buffer_usd=max(0.0, float(os.getenv("BUDGET_SAFETY_BUFFER_USD", "1"))),
            max_session_hours=min(24.0, max(0.1, float(os.getenv("MAX_SESSION_HOURS", "8")))),
            default_idle_timeout_minutes=min(
                1440, max(1, int(os.getenv("DEFAULT_IDLE_TIMEOUT_MINUTES", "15")))
            ),
            upload_limit_bytes=int(upload_gib * 1024**3),
            database_path=Path(os.getenv("DATABASE_PATH", "./data/studio.sqlite3")).expanduser(),
            app_public=_bool("APP_PUBLIC"),
            allowed_origins=tuple(
                origin.strip().rstrip("/")
                for origin in os.getenv("APP_ALLOWED_ORIGINS", "").split(",")
                if origin.strip()
            ),
            multi_user_mode=_bool("MULTI_USER_MODE"),
            credential_encryption_key=os.getenv("MODAL_CREDENTIAL_ENCRYPTION_KEY", ""),
            admin_provisioning_key=os.getenv("ADMIN_PROVISIONING_KEY", ""),
        )
