from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    BOT_TOKEN: str
    API_URL: str = "https://tapi.bale.ai"
    ADMIN_IDS: str = ""  # کاما جدا شده، مثال: 123,456
    # پیش‌فرض توسعه/تست روی SQLite است. روی سرور، در docker-compose مقدار
    # postgresql+psycopg2://foodbot:foodbot@db:5432/foodbot تنظیم می‌شود.
    DATABASE_URL: str = "sqlite:///./data/foodbot.db"
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": ".env"}

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def admin_ids_list(self) -> List[str]:
        raw_ids = self.ADMIN_IDS.replace("\n", ",").replace(";", ",")
        return [admin_id.strip() for admin_id in raw_ids.split(",") if admin_id.strip()]


settings = Settings()
