from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "Farstar"
    bot_token: str = ""
    admin_ids: list[int] = Field(default_factory=list)
    admin_username: str = "admin"
    admin_password: str = ""
    secret_key: str = ""
    database_path: Path = Path("data/farstar.db")
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = "http://localhost:8080"
    cookie_secure: bool = False
    demo: bool = False
    currency: str = "XTR"
    manual_payments: bool = False
    card_number: str = ""
    card_holder: str = ""
    required_channel: str = ""
    channel_url: str = ""
    support_username: str = ""
    terms_text: str = ""
    referral_percent: int = Field(default=10, ge=0, le=50)
    trial_enabled: bool = True
    trial_gb: int = Field(default=1, ge=1, le=10)
    trial_hours: int = Field(default=24, ge=1, le=72)
    worker_interval: int = Field(default=10, ge=2)
    backup_hours: int = Field(default=24, ge=1)

    @field_validator("currency")
    @classmethod
    def currency_valid(cls, value):
        if value not in {"XTR", "IRT"}:
            raise ValueError("CURRENCY must be XTR or IRT")
        return value

    @model_validator(mode="after")
    def validate_secrets(self):
        if len(self.secret_key) < 32 or len(self.admin_password) < 12:
            raise ValueError("Run python -m farstar setup to generate SECRET_KEY and ADMIN_PASSWORD")
        if self.demo and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Demo must listen on loopback")
        if not self.demo and self.bot_token and not self.admin_ids:
            raise ValueError("ADMIN_IDS is required when the bot is enabled")
        return self
