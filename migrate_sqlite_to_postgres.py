"""مهاجرت یک‌بارهٔ داده‌ها از SQLite به PostgreSQL.

روش استفاده:
    1) DATABASE_URL را در محیط برابر آدرس PostgreSQL بگذارید، مثلاً:
         export DATABASE_URL="postgresql+psycopg2://foodbot:foodbot@localhost:5432/foodbot"
    2) اسکریپت را اجرا کنید:
         python migrate_sqlite_to_postgres.py
       و اگر فایل SQLite در مسیر دیگری است:
         python migrate_sqlite_to_postgres.py --source sqlite:///./data/foodbot.db
    3) سپس ربات را به‌صورت عادی اجرا کنید؛ مکانیزم seed خودش پیش‌فرض‌های
       جاافتاده را می‌سازد.

اسکریپت فقط می‌خواند از SQLite و می‌نویسد روی PostgreSQL. اگر جدولی در مقصد
از قبل داده داشته باشد، بدون --force آن جدول رد می‌شود تا داده‌ای پاک نشود.
"""

import argparse
import sys

from sqlalchemy import MetaData, Table, create_engine, delete, func, inspect, select, text

from config import settings
from models import Base


def _sqlite_source_engine(source_url: str):
    return create_engine(source_url, connect_args={"check_same_thread": False})


def migrate(source_url: str, force: bool = False) -> int:
    if settings.is_sqlite:
        print("❌ DATABASE_URL مقصد هنوز SQLite است. آن را روی PostgreSQL تنظیم کنید.")
        return 2

    source_engine = _sqlite_source_engine(source_url)
    target_engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

    if not inspect(source_engine).has_table("users"):
        print(f"❌ در مبدأ ({source_url}) جدول users پیدا نشد. مسیر SQLite درست است؟")
        return 2

    # فقط اسکیمای مقصد ساخته می‌شود؛ seed پیش‌فرض‌ها عمداً اجرا نمی‌شود تا داده‌های
    # مهاجرت‌شده با مقادیر پیش‌فرض قاطی نشوند (خود ربات بعداً seed می‌کند).
    Base.metadata.create_all(bind=target_engine)

    source_meta = MetaData()
    source_meta.reflect(bind=source_engine)

    total = 0
    with source_engine.connect() as src, target_engine.begin() as dst:
        for table_name, target_table in Base.metadata.tables.items():
            if table_name not in source_meta.tables:
                print(f"• {table_name}: در مبدأ وجود ندارد — رد شد")
                continue
            source_table = source_meta.tables[table_name]
            common_columns = [
                column.name for column in target_table.columns
                if column.name in source_table.columns
            ]
            if not common_columns:
                print(f"• {table_name}: ستون مشترکی ندارد — رد شد")
                continue

            rows = [
                {column: row[column] for column in common_columns}
                for row in src.execute(
                    select(*[source_table.c[column] for column in common_columns])
                ).mappings()
            ]

            existing = dst.execute(
                select(func.count()).select_from(target_table)
            ).scalar() or 0
            if existing and not force:
                print(f"• {table_name}: مقصد {existing} رکورد دارد — رد شد (برای جای‌گزینی --force بدهید)")
                continue
            if existing and force:
                dst.execute(delete(target_table))

            if rows:
                dst.execute(target_table.insert(), rows)
            total += len(rows)
            print(f"✅ {table_name}: {len(rows)} رکورد منتقل شد")

    _reset_postgres_sequences(target_engine)
    print(f"\n🎉 مهاجرت کامل شد — مجموع {total} رکورد.")
    return 0


def _reset_postgres_sequences(target_engine):
    """سریال‌های PostgreSQL را بعد از درج id صریح هم‌تراز می‌کند."""
    if target_engine.dialect.name != "postgresql":
        return
    for table_name, table in Base.metadata.tables.items():
        pk_columns = [column.name for column in table.primary_key.columns]
        if len(pk_columns) != 1:
            continue
        pk = pk_columns[0]
        with target_engine.begin() as connection:
            sequence = connection.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table_name, "c": pk},
            ).scalar()
            if not sequence:
                continue
            connection.execute(
                text(
                    f"SELECT setval(:seq, COALESCE((SELECT MAX({pk}) FROM {table_name}), 1))"
                ),
                {"seq": sequence},
            )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="مهاجرت داده از SQLite به PostgreSQL")
    parser.add_argument(
        "--source",
        default="sqlite:///./data/foodbot.db",
        help="آدرس SQLite مبدأ (پیش‌فرض: sqlite:///./data/foodbot.db)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="اجازهٔ جای‌گزینی جدول‌هایی که در مقصد داده دارند",
    )
    args = parser.parse_args(argv)
    return migrate(args.source, args.force)


if __name__ == "__main__":
    sys.exit(main())
