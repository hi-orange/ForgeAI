from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "ForgeAI"
    debug: bool = True

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str
    mysql_database: str

    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080

    cors_origins: str = "http://localhost:5173"

    llm_provider: str = "deepseek"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    upload_root: str = "uploads"
    max_resume_size_mb: int = 10

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env.development",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def database_url(self) -> str:
        password = quote_plus(self.mysql_password)
        return (
            f"mysql+pymysql://"
            f"{self.mysql_user}:"
            f"{password}@"
            f"{self.mysql_host}:"
            f"{self.mysql_port}/"
            f"{self.mysql_database}"
            f"?charset=utf8mb4"
        )


settings = Settings()
