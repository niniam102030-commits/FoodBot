import logging
import os
import json
import jdatetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import backup_database, get_db
from models import Archive, Reservation, DinnerReservation, FreeOrder, UserContainer, User
from services import (
    build_excel_report, get_all_admin_ids, get_next_period, get_registration_period,
    get_registration_window, get_report_snapshot, get_setting,
    is_registration_window_day, registration_deadline_text, registration_period_text,
    set_setting,
)
from config import settings

logger = logging.getLogger(__name__)


async def _notify_admins(bot, text: str):
    for admin_id in get_all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Admin notify failed for {admin_id}: {e}")


async def job_registration_reminder(bot):
    _, end_day = get_registration_window()
    if get_setting("registration_status") != "active" or not is_registration_window_day():
        return

    with get_db() as db:
        users = db.query(User).filter(
            User.full_name != "",
            (User.lunch_status != "completed") | (User.dinner_status != "completed"),
        ).all()

    for user in users:
        pending_meals = []
        if user.lunch_status != "completed":
            pending_meals.append("ناهار")
        if user.dinner_status != "completed":
            pending_meals.append("شام")
        meals_text = " و ".join(pending_meals) if len(pending_meals) == 2 else (pending_meals[0] if pending_meals else "")
        text = (
            "⏰ یادآوری ثبت غذا\n\n"
            f"ثبت {meals_text} شما هنوز انجام نشده است.\n"
            f"لطفاً ثبت غذای خود را انجام دهید. مهلت ثبت‌نام تا {registration_deadline_text()} است."
        )
        try:
            await bot.send_message(user.user_id, text)
        except Exception as exc:
            logger.error("Registration reminder failed for %s: %s", user.user_id, exc)


async def job_daily_backup(bot=None):
    """بکاپ روزانه دیتابیس؛ ۷ نسخه اخیر نگه داشته می‌شود."""
    try:
        backup_path = backup_database()
        logger.info("Daily backup created: %s", backup_path)
    except Exception as exc:
        logger.error("Daily backup failed: %s", exc)
        return
    backup_dir = os.path.dirname(backup_path)
    try:
        backups = sorted(
            (
                os.path.join(backup_dir, name)
                for name in os.listdir(backup_dir)
                if name.startswith("foodbot_") and name.endswith(".db")
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        for old_backup in backups[7:]:
            os.remove(old_backup)
    except OSError as exc:
        logger.error("Could not prune old backups: %s", exc)


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
                    await bot.send_document(admin_id, f, caption="📊 فایل گزارش نهایی تفکیکشده ماه جاری.")
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

    scheduler.add_job(
        job_registration_reminder,
        CronTrigger(hour=9, minute=0, timezone="Asia/Tehran"),
        args=[bot],
        id="registration_reminder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ریست ماهانه: آخرین روز ماه ساعت ۲۳
    # از آنجا که آخرین روز ماه متغیر است، هر شب ساعت ۲۳ چک میکنیم
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
