import logging
import os
import json
import jdatetime
from aiogram.types import BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import backup_database, get_db
from models import Archive, Reservation, DinnerReservation, FreeOrder, UserContainer, User
from services import (
    build_excel_report, build_kitchen_report, get_all_admin_ids, get_next_period,
    get_registration_period, get_registration_window, get_report_snapshot,
    is_registration_last_day, is_registration_open, is_registration_window_day,
    registration_deadline_text, registration_period_text, set_setting, get_setting,
    today_key,
)
from config import settings

logger = logging.getLogger(__name__)


async def _notify_admins(bot, text: str):
    for admin_id in get_all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Admin notify failed for {admin_id}: {e}")


async def job_registration_reminder(bot, only_last_day: bool = False):
    """یادآوری ثبت غذا برای کاربرانی که وارد ربات شده‌اند.

    - از ابتدای بازهٔ ثبت‌نام و سپس هر روز یک‌بار ارسال می‌شود.
    - در آخرین روز بازه، این کار هر ۸ ساعت تکرار می‌شود (only_last_day=True).
    - قاعدهٔ مهم: اگر کاربر ناهار را ثبت کرده باشد، هیچ یادآوری‌ای — حتی برای
      شام — ارسال نمی‌شود.
    """
    if get_setting("registration_status") != "active" or not is_registration_open():
        return
    if only_last_day and not is_registration_last_day():
        return

    admins = set(get_all_admin_ids())
    # داده‌ها داخل نشست به تاپل ساده تبدیل می‌شوند تا پس از بسته شدن session،
    # نمونه‌های ORM منقضی نشوند (DetachedInstanceError).
    with get_db() as db:
        targets = [
            (user.user_id, user.dinner_status)
            for user in db.query(User).filter(
                User.full_name != "",
                User.lunch_status != "completed",
            ).all()
        ]

    for user_id, dinner_status in targets:
        if user_id in admins:
            continue
        pending_meals = ["ناهار"]
        if dinner_status != "completed":
            pending_meals.append("شام")
        meals_text = " و ".join(pending_meals)
        text = (
            "⏰ یادآوری ثبت غذا\n\n"
            f"ثبت {meals_text} شما هنوز انجام نشده است.\n"
            f"لطفاً ثبت غذای خود را انجام دهید. مهلت ثبت‌نام تا {registration_deadline_text()} است."
        )
        try:
            await bot.send_message(user_id, text)
        except Exception as exc:
            logger.error("Registration reminder failed for %s: %s", user_id, exc)


async def job_daily_backup(bot=None):
    """بکاپ دیتابیس؛ ۷ نسخه اخیر نگه داشته می‌شود."""
    try:
        backup_path = backup_database()
        logger.info("Daily backup created: %s", backup_path)
    except Exception as exc:
        logger.error("Daily backup failed: %s", exc)
        return
    backup_dir = os.path.dirname(backup_path)
    try:
        extensions = (".db", ".sql")
        backups = sorted(
            (
                os.path.join(backup_dir, name)
                for name in os.listdir(backup_dir)
                if name.startswith("foodbot_") and name.endswith(extensions)
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        for old_backup in backups[7:]:
            os.remove(old_backup)
    except OSError as exc:
        logger.error("Could not prune old backups: %s", exc)


async def job_registration_backup(bot):
    """ارسال بکاپ آمار ماه (اکسل) و گزارش آشپزخانه به ادمین‌ها.

    - در ایام بازهٔ ثبت‌نام، هر روز یک‌بار ارسال می‌شود.
    - خارج از بازه، فقط اگر ادمین همان روز ثبت‌نام را دستی فعال کرده باشد.
    - گزارش آشپزخانه شامل ظرف یکبار مصرف و غذای آزاد نمی‌شود.
    """
    key = today_key()
    if get_setting("last_registration_backup_at") == key:
        return

    window_day = get_setting("registration_status") == "active" and is_registration_window_day()
    override = get_setting("registration_manual_override") == "true"
    activated_today = get_setting("registration_manual_activated_at") == key
    if not window_day and not (override and activated_today):
        return

    try:
        stats_file = build_excel_report(file_suffix="_backup")
        kitchen_file = build_kitchen_report(file_suffix="_kitchen")
    except Exception:
        logger.exception("Registration backup build failed")
        return

    for admin_id in get_all_admin_ids():
        try:
            with open(stats_file, "rb") as handle:
                await bot.send_document(
                    admin_id,
                    BufferedInputFile(handle.read(), filename=os.path.basename(stats_file)),
                    caption=f"📊 بکاپ آمار ماه {registration_period_text()} (اکسل)",
                )
            with open(kitchen_file, "rb") as handle:
                await bot.send_document(
                    admin_id,
                    BufferedInputFile(handle.read(), filename=os.path.basename(kitchen_file)),
                    caption="🍳 گزارش آشپزخانه (بدون ظرف یکبار مصرف و غذای آزاد)",
                )
        except Exception as exc:
            logger.error("Send registration backup to %s failed: %s", admin_id, exc)

    for path in (stats_file, kitchen_file):
        try:
            os.remove(path)
        except OSError:
            pass
    set_setting("last_registration_backup_at", key)


async def job_send_report(bot):
    now = jdatetime.datetime.now()
    key = f"{now.year}_{now.month}_25"
    with get_db() as db:
        if db.query(Archive).filter(Archive.date_str == key).first():
            return
        snapshot = get_report_snapshot()
        db.add(Archive(date_str=key, payload=json.dumps(snapshot, ensure_ascii=False), year=now.year, month=now.month))

    try:
        file_name = build_excel_report()
        for admin_id in get_all_admin_ids():
            try:
                with open(file_name, "rb") as f:
                    await bot.send_document(admin_id, f, caption="📊 فایل گزارش نهایی تفکیک‌شده ماه جاری.")
            except Exception as e:
                logger.error(f"Send report to {admin_id} failed: {e}")
        os.remove(file_name)
    except Exception as e:
        logger.error(f"job_send_report error: {e}")


async def job_monthly_reset(bot):
    year, month = get_registration_period()
    key = f"{year}_{month}_reset"
    with get_db() as db:
        if db.query(Archive).filter(Archive.date_str == key).first():
            return
    backup_path = backup_database()
    with get_db() as db:
        snapshot = get_report_snapshot()
        db.add(Archive(date_str=key, payload=json.dumps(snapshot, ensure_ascii=False), year=year, month=month))
        db.query(Reservation).delete()
        db.query(DinnerReservation).delete()
        db.query(FreeOrder).delete()
        db.query(UserContainer).delete()
        db.query(User).update({
            "step": "main_menu",
            "status": "pending",
            "lunch_status": "pending",
            "dinner_status": "pending",
        })

    next_year, next_month = get_next_period(year, month)
    set_setting("registration_year", str(next_year))
    set_setting("registration_month", str(next_month))
    set_setting("registration_status", "active")
    set_setting(
        "registration_manual_override",
        "false" if is_registration_window_day() else "true",
    )

    from services import admin_notice_text
    await _notify_admins(
        bot,
        admin_notice_text(
            f"📢 ثبت‌نام ماه {registration_period_text()} فعال شد ✅\n"
            "🔄 پرونده ماه جاری بسته شد و ثبت‌نام ماه بعد فعال شد.",
            note=f"💾 پشتیبان: {os.path.basename(backup_path)}",
        ),
    )


def _get_last_day_of_month() -> int:
    now = jdatetime.datetime.now()
    if now.month <= 6:
        return 31
    elif now.month <= 11:
        return 30
    return 30 if jdatetime.date(now.year, 12, 1).is_leap_year() else 29


def create_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Tehran")

    scheduler.add_job(
        job_send_report,
        CronTrigger(day=25, hour=9, minute=0, timezone="Asia/Tehran"),
        args=[bot],
        id="send_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # یادآوری روزانه ثبت غذا (از ابتدای بازه و سپس هر روز)
    scheduler.add_job(
        job_registration_reminder,
        CronTrigger(hour=9, minute=0, timezone="Asia/Tehran"),
        args=[bot],
        id="registration_reminder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # در آخرین روز بازه، یادآوری هر ۸ ساعت (۱، ۹ و ۱۷)
    scheduler.add_job(
        job_registration_reminder,
        CronTrigger(hour=1, minute=0, timezone="Asia/Tehran"),
        args=[bot, True],
        id="registration_reminder_last_day_1",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_registration_reminder,
        CronTrigger(hour=17, minute=0, timezone="Asia/Tehran"),
        args=[bot, True],
        id="registration_reminder_last_day_2",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # بکاپ آمار ماه + گزارش آشپزخانه برای ادمین‌ها؛ هر شب ۲۲ چک می‌شود
    scheduler.add_job(
        job_registration_backup,
        CronTrigger(hour=22, minute=0, timezone="Asia/Tehran"),
        args=[bot],
        id="registration_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ریست ماهانه: هر شب ساعت ۲۳ بررسی می‌شود
    scheduler.add_job(
        _check_monthly_reset,
        CronTrigger(hour=23, minute=0, timezone="Asia/Tehran"),
        args=[bot],
        id="monthly_reset_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # بکاپ روزانه دیتابیس ساعت ۳ بامداد
    scheduler.add_job(
        job_daily_backup,
        CronTrigger(hour=3, minute=0, timezone="Asia/Tehran"),
        id="daily_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler


async def _check_monthly_reset(bot):
    now = jdatetime.datetime.now()
    active_year, active_month = get_registration_period()
    if (active_year, active_month) == (now.year, now.month) and now.day == _get_last_day_of_month():
        await job_monthly_reset(bot)
