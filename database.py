import logging
import os
import sqlite3
import subprocess
from datetime import datetime

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from contextlib import contextmanager
from config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

# تنظیمات موتور بر اساس نوع دیتابیس: SQLite برای توسعه/تست و PostgreSQL روی سرور
_engine_kwargs: dict = {}
if settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # pool_pre_ping اتصال‌های مرده را قبل از استفاده بازیابی می‌کند و
    # pool_recycle از قطع شدن اتصال‌های طولانی توسط فایروال جلوگیری می‌کند
    _engine_kwargs.update(pool_pre_ping=True, pool_recycle=1800)

engine = create_engine(settings.DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    from models import Base as ModelsBase, Setting, Food, WEEKDAYS_MAP
    ModelsBase.metadata.create_all(bind=engine)
    _migrate()
    _seed_defaults()


def backup_database() -> str:
    """Create a timestamped backup of the database and return its path.

    برای SQLite یک کپی امن از فایل ساخته می‌شود و برای PostgreSQL از pg_dump
    یک فایل SQL فشرده گرفته می‌شود. اگر pg_dump در دسترس نباشد، خطا بالا می‌رود.
    """
    backup_dir = os.path.join(os.path.dirname(__file__), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if settings.is_sqlite:
        source_path = settings.DATABASE_URL.removeprefix("sqlite:///")
        if not os.path.isabs(source_path):
            source_path = os.path.abspath(source_path)
        if not os.path.exists(source_path):
            raise FileNotFoundError(source_path)
        backup_path = os.path.join(backup_dir, f"foodbot_{stamp}.db")
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return backup_path

    # PostgreSQL: pg_dump یک فایل SQL قابل بازیابی می‌سازد
    backup_path = os.path.join(backup_dir, f"foodbot_{stamp}.sql")
    try:
        with open(backup_path, "w", encoding="utf-8") as dump_file:
            subprocess.run(
                ["pg_dump", "--no-owner", "--no-privileges", settings.DATABASE_URL],
                stdout=dump_file,
                stderr=subprocess.PIPE,
                check=True,
            )
    except FileNotFoundError as exc:
        raise RuntimeError("pg_dump not installed; cannot back up PostgreSQL") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pg_dump failed: {exc.stderr.decode(errors='ignore')}") from exc
    return backup_path


def _migrate():
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    # نوع ستون تاریخ‌زمان در SQLite و PostgreSQL متفاوت است
    datetime_type = "DATETIME" if settings.is_sqlite else "TIMESTAMP"
    with engine.begin() as connection:
        if "archive" in tables:
            columns = {column["name"] for column in inspector.get_columns("archive")}
            if "year" not in columns:
                connection.execute(text("ALTER TABLE archive ADD COLUMN year INTEGER"))
            if "month" not in columns:
                connection.execute(text("ALTER TABLE archive ADD COLUMN month INTEGER"))
        for table_name in ("reservations", "dinner_reservations"):
            if table_name in tables:
                res_columns = {column["name"] for column in inspector.get_columns(table_name)}
                if "created_at" not in res_columns:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN created_at {datetime_type}"))
                if "updated_at" not in res_columns:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN updated_at {datetime_type}"))
        if "fsm_states" in tables:
            # stateهای ذخیره‌شده با فرمت خراب "<State '...'>" از نسخه‌های قبلی؛ قابل بازیابی نیستند و پاک می‌شوند
            connection.execute(text(
                "DELETE FROM fsm_states WHERE state LIKE '<State%'"
            ))
        if "foods" in tables:
            columns = {column["name"] for column in inspector.get_columns("foods")}
            if "meal_type" not in columns:
                connection.execute(text("ALTER TABLE foods ADD COLUMN meal_type VARCHAR(20) DEFAULT 'lunch' NOT NULL"))
        if "users" in tables:
            columns = {column["name"] for column in inspector.get_columns("users")}
            if "lunch_status" not in columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN lunch_status VARCHAR(50) DEFAULT 'pending'"))
            if "dinner_status" not in columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN dinner_status VARCHAR(50) DEFAULT 'pending'"))
            connection.execute(text(
                "UPDATE users SET lunch_status = CASE WHEN status = 'completed' THEN 'completed' ELSE 'pending' END "
                "WHERE lunch_status IS NULL"
            ))
            connection.execute(text("UPDATE users SET dinner_status = 'pending' WHERE dinner_status IS NULL"))
            connection.execute(text(
                "UPDATE users SET step = 'get_access_code' "
                "WHERE COALESCE(full_name, '') = '' AND step = 'get_name'"
            ))


def _seed_defaults():
    from models import Setting, Food, WEEKDAYS_MAP
    with get_db() as db:
        defaults = [
            ("bot_status", "active"),
            ("registration_status", "active"),
            ("registration_start_day", "1"),
            ("registration_end_day", "7"),
            ("registration_start_month_offset", "0"),
            ("registration_end_month_offset", "0"),
            ("registration_manual_override", "false"),
            ("container_price", "6000"),
            ("holidays", ""),
            ("blocked_days", ""),
            ("registration_access_code", "1234"),
            ("meal_prices_initialized_v2", "false"),
            # تاریخ (شمسی) فعال‌سازی دستی ثبت‌نام؛ همان شب بکاپ ماهانه ارسال می‌شود
            ("registration_manual_activated_at", ""),
            # آخرین تاریخی که بکاپ/گزارش آشپزخانه برای ادمین‌ها ارسال شده (ضد ارسال تکراری)
            ("last_registration_backup_at", ""),
        ]
        for key, value in defaults:
            if not db.query(Setting).filter(Setting.key == key).first():
                db.add(Setting(key=key, value=value))
        db.flush()

        for day_num, info in WEEKDAYS_MAP.items():
            if day_num == 6:
                continue
            if not db.query(Food).filter(Food.id == day_num).first():
                db.add(Food(
                    id=day_num,
                    day_num=day_num,
                    day_name=info["name"],
                    food_name=info["food"],
                    normal_price=info["norm"],
                    free_price=info["free"],
                    meal_type="lunch",
                ))

        for day_num in range(6):
            dinner_id = 100 + day_num
            if not db.query(Food).filter(Food.id == dinner_id).first():
                info = WEEKDAYS_MAP[day_num]
                db.add(Food(
                    id=dinner_id,
                    day_num=day_num,
                    day_name=info["name"],
                    food_name=f"شام {day_num + 1}",
                    # قیمت پیش‌فرض شام هم از همان جدول روزها گرفته می‌شود تا
                    # ربات تازه‌نصب با قیمت‌های صفر بالا نیاید
                    normal_price=info["norm"],
                    free_price=info["free"],
                    meal_type="dinner",
                ))

        price_marker = db.query(Setting).filter(Setting.key == "meal_prices_initialized_v2").first()
        if price_marker and price_marker.value != "true":
            db.query(Food).update({"normal_price": 20000, "free_price": 80000})
            price_marker.value = "true"

        legacy_food = db.query(Food).filter(Food.id == 0, Food.day_name == "دوشنبه").first()
        if legacy_food:
            for day_num, info in WEEKDAYS_MAP.items():
                food = db.query(Food).filter(Food.id == day_num).first()
                if not food:
                    food = Food(id=day_num)
                    db.add(food)
                food.day_num = day_num
                food.day_name = info["name"]
                food.food_name = info["food"]
                food.normal_price = info["norm"]
                food.free_price = info["free"]
