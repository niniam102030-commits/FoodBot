import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ErrorEvent, InputFile
from aiohttp import FormData

from config import settings
from database import init_db
from handlers import router
from scheduler import create_scheduler
from services import admin_notice_text
from storage import SQLiteStorage

# ─── لاگگیری ──────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        # چرخش لاگ: حداکثر ۵ مگابایت در هر فایل و ۳ نسخه قدیمی نگه داشته می‌شود
        RotatingFileHandler(
            "logs/bot.log", encoding="utf-8",
            maxBytes=5 * 1024 * 1024, backupCount=3,
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)
_instance_lock_file = None


def _acquire_instance_lock():
    global _instance_lock_file

    lock_path = os.path.join(os.path.dirname(__file__), ".bot.lock")
    lock_file = open(lock_path, "a+")
    lock_file.seek(0)

    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        lock_file.close()
        raise RuntimeError("A FoodBot instance is already running")

    _instance_lock_file = lock_file


# ─── Session سفارشی برای بله ──────────────────────────────────────────────────
class BaleAiohttpSession(AiohttpSession):
    def build_form_data(self, bot, method):
        form = FormData(quote_fields=False)
        files = {}
        for key, value in method.model_dump(warnings=False).items():
            if isinstance(value, InputFile):
                form.add_field(key, value.read(bot), filename=value.filename or key)
                continue
            value = self.prepare_value(value, bot=bot, files=files)
            if value:
                form.add_field(key, value)
        for key, value in files.items():
            form.add_field(key, value.read(bot), filename=value.filename or key)
        return form


async def notify_admins(bot: Bot, text: str):
    from services import get_all_admin_ids
    for admin_id in get_all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Admin notify failed for {admin_id}: {e}")


async def main():
    try:
        _acquire_instance_lock()
    except RuntimeError as exc:
        logger.error(exc)
        return

    init_db()

    session = BaleAiohttpSession(
        api=TelegramAPIServer.from_base(settings.API_URL),
    )
    bot = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=SQLiteStorage())
    dp.include_router(router)

    # مدیریت خطای سراسری: خطای هر handler به کاربر پیام موقت می‌دهد و کامل لاگ می‌شود
    async def global_error_handler(event: ErrorEvent):
        logger.error(
            "Unhandled error while processing update: %s",
            event.update.update_id,
            exc_info=event.exception,
        )
        try:
            update = event.update
            if update.callback_query:
                await update.callback_query.answer(
                    "⚠️ خطای موقتی رخ داد. لطفاً دوباره تلاش کنید.",
                    show_alert=True,
                )
            elif update.message:
                await update.message.answer(
                    "⚠️ خطای موقتی رخ داد. لطفاً دوباره تلاش کنید."
                )
        except Exception:
            logger.exception("Could not deliver error message to user")

    dp.errors.register(global_error_handler)

    scheduler = create_scheduler(bot)
    scheduler.start()

    await notify_admins(bot, admin_notice_text("✅ ربات فعال شد."))
    logger.info("MFIH Food Bot is running...")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
