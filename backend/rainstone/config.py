from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAINSTONE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rainstone:rainstone@localhost:5432/rainstone"
    auth_mode: str = "development"
    demo_data: bool = False
    static_dir: Path = Path("static")
    root_path: str = ""

    @model_validator(mode="after")
    def reject_development_identity_in_production(self) -> "Settings":
        if self.auth_mode == "development" and not self.demo_data:
            raise ValueError("development identity requires RAINSTONE_DEMO_DATA=true")
        if self.auth_mode not in {"development", "trusted-proxy"}:
            raise ValueError("auth_mode must be development or trusted-proxy")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
