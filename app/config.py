from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DB_URL: str 
    DB_INIT_ON_STARTUP: bool = True
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    CHAT_SESSION_TTL_SECONDS: PositiveInt = 1800
    CHAT_PRELOAD_AI: bool = False
    SERPAPI_KEYS: str = ""
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-20b"
    @property
    def DATABASE_URL_asyncpg(self):
        url = self.DB_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            
        if "?" in url:
            url = url.split("?")[0]
            
        return url

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
