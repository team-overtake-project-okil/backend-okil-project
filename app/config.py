from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DB_URL: str 

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