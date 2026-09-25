import asyncio
import json
import logging
import os
import re
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove

from config import settings
from database import get_db
from models import (
    AuditLog, User, Food, Reservation, DinnerReservation, FreeOrder, UserContainer, Archive,
    WEEKDAYS_MAP,
)
from keyboards import (
    get_start_menu, get_main_menu, get_weekly_calendar_keyboard,
    get_delete_reg_keyboard, get_delete_user_keyboard,
    get_food_list_keyboard, get_day_edit_keyboard, get_archive_month_keyboard,
    get_confirmation_keyboard, get_delete_account_keyboard, get_zero_next_keyboard,
    get_meal_choice_keyboard, get_admin_user_days_keyboard, get_restart_keyboard,
    get_user_scope_keyboard,
    get_admin_management_keyboard, get_extra_admins_keyboard,
    get_user_list_keyboard, get_user_card_keyboard, get_blocked_days_keyboard,
    get_back_to_menu_keyboard, get_cancel_keyboard,
)
from services import (
    PERSIAN_MONTHS, add_extra_admin, admin_notice_text, calculate_user_total,
    build_excel_report, get_all_admin_ids, get_menu_text, build_invoice_text, format_price,
    get_days_details, get_next_period, get_registration_period,
    get_registration_window, get_registration_window_config, get_setting,
    is_registration_open, is_registration_window_day, remove_extra_admin,
    registration_deadline_text, registration_period_text, registration_window_text,
    registration_deadline_notice,
    set_setting, shift_period, to_int, to_persian_digits,
)
from scheduler import job_monthly_reset
import jdatetime

logger = logging.getLogger(__name__)
router = Router()

_calendar_locks: dict[str, asyncio.Lock] = {}


class NameEditState(StatesGroup):
    waiting_for_name = State()


class PriceEditState(StatesGroup):
    waiting_for_normal = State()
    waiting_for_free = State()


class ContainerPriceState(StatesGroup):
    waiting_for_price = State()


class FoodEditState(StatesGroup):
    waiting_for_name = State()


class UserReservationState(StatesGroup):
    waiting_for_days = State()


class RegistrationWindowState(StatesGroup):
    waiting_for_start = State()
    waiting_for_end = State()
    waiting_for_start_offset = State()
    waiting_for_end_offset = State()


class BlockedDaysState(StatesGroup):
    waiting_for_days = State()


class AccessCodeState(StatesGroup):
    waiting_for_code = State()


class AdminManagementState(StatesGroup):
    waiting_for_new_admin_id = State()
    waiting_for_confirm = State()


def _is_admin_command(text: str) -> bool:
    command = _get_command(text)
    return command in {
        "ex", "us", "user", "users", "یوزر", "یوزرز", "rm", "dt", "pr",
        "mn", "menu", "منو", "تغییر", "تغییرنام", "تغییرنامغذا", "changename", "sc", "ho", "bd",
        "st", "stats", "stat", "آمار", "edituser", "eu", "ویرایشکاربر", "audit", "logs", "لاگ",
        "lg", "dl", "deadline", "مهلت", "بازهثبتنام", "cn", "cancelnext", "لغوماه", "لغوشروعماه",
        "vc", "cc", "ma",
        "بستن", "close", "closemonth", "close_month", "cm", "reset", "شروعماه", "rg",
    }


def _get_command(text: str) -> str:
    if not text:
        return ""
    if text.strip() == "⚙️ منوی مدیریت ادمین":
        return "menu"
    return text.strip().split()[0].lower().lstrip("/").split("@", 1)[0]


def is_admin(user_id: str) -> bool:
    return user_id in get_all_admin_ids()


def is_primary_admin(user_id: str) -> bool:
    """فقط ادمین اصلی (آیدی موجود در .env) — تنها او می‌تواند ادمین اضافه/حذف کند."""
    return user_id in settings.admin_ids_list


# دکمه‌هایی که ممکن است نشانگر وضعیت (⏳/✅) بگیرند
_MEAL_MENU_BUTTONS = {
    "🍽️ ثبت / ویرایش غذای ناهار",
    "🌙 ثبت / ویرایش غذای شام",
}


def _main_menu_markup(user_id: str):
    """منوی اصلی با نشانگر وضعیت هر وعده (⏳ در حال ثبت / ✅ نهایی‌شده)."""
    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        step = user.step if user else None
        lunch_status = user.lunch_status if user else None
        dinner_status = user.dinner_status if user else None
    lunch_mark = ""
    dinner_mark = ""
    if lunch_status == "completed":
        lunch_mark = " ✅"
    elif step and step.startswith("lunch_") and step != "lunch_done":
        lunch_mark = " ⏳"
    if dinner_status == "completed":
        dinner_mark = " ✅"
    elif step and step.startswith("dinner_") and step != "dinner_done":
        dinner_mark = " ⏳"
    return get_main_menu(is_admin(user_id), lunch_mark, dinner_mark)


def _normalize_access_code(text: str) -> str:
    return text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def _is_valid_access_code(text: str) -> bool:
    return bool(re.fullmatch(r"\d{4}", _normalize_access_code(text)))


def _clear_user_data(user_id: str):
    with get_db() as db:
        db.query(Reservation).filter(Reservation.user_id == user_id).delete()
        db.query(DinnerReservation).filter(DinnerReservation.user_id == user_id).delete()
        db.query(FreeOrder).filter(FreeOrder.user_id == user_id).delete()
        db.query(UserContainer).filter(UserContainer.user_id == user_id).delete()


def _clear_meal_data(user_id: str, meal_type: str):
    with get_db() as db:
        model = _reservation_model(meal_type)
        db.query(model).filter(model.user_id == user_id).delete()
        food_ids = [food.id for food in db.query(Food).filter(Food.meal_type == meal_type).all()]
        if food_ids:
            db.query(FreeOrder).filter(
                FreeOrder.user_id == user_id,
                FreeOrder.food_id.in_(food_ids),
            ).delete(synchronize_session=False)
        if meal_type == "lunch":
            db.query(UserContainer).filter(UserContainer.user_id == user_id).delete()


def _audit(actor_id: str, action: str, target_id: str | None = None, details: str = ""):
    with get_db() as db:
        db.add(AuditLog(actor_id=actor_id, action=action, target_id=target_id, details=details))


def _meal_label(meal_type: str) -> str:
    if meal_type == "lunch":
        return "ناهار"
    if meal_type == "dinner":
        return "شام"
    return "کل کاربران"


def _window_label(start_day: int, start_offset: int, end_day: int, end_offset: int) -> str:
    target_year, target_month = get_registration_period()
    start_year, start_month = shift_period(target_year, target_month, start_offset)
    end_year, end_month = shift_period(target_year, target_month, end_offset)
    if (start_year, start_month) == (end_year, end_month):
        return f"{PERSIAN_MONTHS[start_month - 1]}، روز {to_persian_digits(start_day)} تا {to_persian_digits(end_day)}"
    return (
        f"روز {to_persian_digits(start_day)} {PERSIAN_MONTHS[start_month - 1]} "
        f"تا روز {to_persian_digits(end_day)} {PERSIAN_MONTHS[end_month - 1]}"
    )


def _holidays_label(days_str: str) -> str:
    """فهرست روزهای تعطیل را با نام روز هفته نمایش می‌دهد؛ در خطا متن خام برمی‌گردد."""
    parts = [item.strip() for item in days_str.replace("،", ",").split(",") if item.strip()]
    if not parts:
        return "(خالی)"
    try:
        year, month = get_registration_period()
        labels = []
        for item in parts:
            day = int(item)
            weekday = jdatetime.date(year, month, day).weekday()
            labels.append(f"{day} ({WEEKDAYS_MAP[weekday]['name']})")
        return "، ".join(labels)
    except (ValueError, KeyError):
        return days_str


def _status_field(meal_type: str) -> str:
    return "lunch_status" if meal_type == "lunch" else "dinner_status"


def _reservation_model(meal_type: str):
    return Reservation if meal_type == "lunch" else DinnerReservation


async def _notify_admins(bot, text: str):
    for admin_id in get_all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.error("Admin notification failed for %s: %s", admin_id, exc)


# ─── /start ───────────────────────────────────────────────────────────────────

async def _send_start_flow(message: Message, user_id: str):
    if not is_registration_open() and not is_admin(user_id):
        await message.answer(
            f"⏳ بازه ثبت‌نام تمام شده است. مهلت ثبت‌نام تا {registration_deadline_text()} بود.\n"
            f"🗓️ بازه ثبت‌نام: {registration_window_text()}"
        )
        return

    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        is_new = not user
        onboarding_step = user.step if user else None
        if is_new:
            db.add(User(user_id=user_id, full_name="", step="get_access_code", status="pending"))

    if is_new or onboarding_step == "get_access_code":
        await message.answer(
            ("سلام! به ربات ثبت غذای موسسه امام حسین ع خوش آمدید.\n" if is_new else "")
            + "کد ورود را وارد کنید.",
            reply_markup=get_start_menu(),
        )
    else:
        await message.answer(get_menu_text(), reply_markup=_main_menu_markup(user_id))


@router.message(Command("start"))
async def cmd_start(message: Message):
    await _send_start_flow(message, str(message.from_user.id))


# ─── ثبت وعده‌های غذایی ────────────────────────────────────────────────────────

@router.message(F.text == "📋 منوی هفتگی و قیمت‌ها")
async def handle_weekly_menu(message: Message):
    await message.answer(get_menu_text(), reply_markup=_main_menu_markup(str(message.from_user.id)))


HELP_TEXT = (
    "💡 راهنمای استفاده از ربات\n\n"
    "1️⃣ از منوی پایین صفحه وعده مورد نظر را انتخاب کنید (ناهار/شام).\n"
    "2️⃣ روزهای دلخواه را در تقویم بزنید و «✅ ثبت و مرحله بعد» را بزنید.\n"
    "3️⃣ تعداد غذاهای آزاد و ظروف را مشخص کنید؛ در پایان فاکتور نمایش داده می‌شود.\n\n"
    "📋 «مشاهده ثبت‌های من» فاکتور فعلی‌تان را نشان می‌دهد.\n"
    "✏️ با «ویرایش نام» می‌توانید نامتان را عوض کنید.\n"
    "🗑️ «حذف حساب کاربری» همه اطلاعات شما را برای همیشه پاک می‌کند.\n\n"
    "❓ سؤال یا مشکل؟ با معاونت اجرایی موسسه در میان بگذارید."
)


@router.message(F.text == "💡 راهنما")
async def handle_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=get_back_to_menu_keyboard())


@router.message(F.text == "📋 مشاهده ثبت‌های من")
async def handle_my_registrations(message: Message):
    await message.answer(
        "کدام وعده را می‌خواهید ببینید؟",
        reply_markup=get_meal_choice_keyboard("view_reg"),
    )


@router.callback_query(F.data.in_({"view_reg_lunch", "view_reg_dinner"}))
async def cb_view_my_registration(callback: CallbackQuery):
    meal_type = callback.data.removeprefix("view_reg_")
    await callback.answer()
    await callback.message.edit_text(
        build_invoice_text(str(callback.from_user.id), meal_type),
        reply_markup=get_back_to_menu_keyboard(),
    )


@router.message(F.text == "🍽️ ثبت / ویرایش غذای ناهار")
async def handle_lunch_start(message: Message, state: FSMContext):
    await _start_meal_registration(message, "lunch", state)


@router.message(F.text == "🌙 ثبت / ویرایش غذای شام")
async def handle_dinner_start(message: Message, state: FSMContext):
    await _start_meal_registration(message, "dinner", state)


async def _start_meal_registration(message: Message, meal_type: str, state: FSMContext):
    user_id = str(message.from_user.id)

    if not is_registration_open() and not is_admin(user_id):
        await message.answer(f"⏳ مهلت ثبت‌نام تا {registration_deadline_text()} است.")
        return

    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        user_step = user.step if user else None
        meal_status = getattr(user, _status_field(meal_type), None) if user else None

    if not user or user_step == "get_name":
        with get_db() as db:
            if not db.query(User).filter(User.user_id == user_id).first():
                db.add(User(user_id=user_id, full_name="", step="get_name", status="pending"))
        await state.set_state(NameEditState.waiting_for_name)
        await message.answer("لطفاً ابتدا نام و نام خانوادگی خود را ارسال کنید:")
        return

    if meal_status == "completed":
        meal_name = _meal_label(meal_type)
        await message.answer(
            f"⚠️ ثبت {meal_name} شما قبلاً نهایی شده است.\nاگر می‌خواهید، ثبت {meal_name} قبلی را حذف و دوباره وارد کنید:",
            reply_markup=get_delete_reg_keyboard(meal_type),
        )
        return

    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        user.step = f"{meal_type}_choosing_days"

    reserved = _get_user_reserved_days(user_id, meal_type)
    days = get_days_details()
    await message.answer(
        _calendar_week_text(days, 0, meal_type),
        reply_markup=get_weekly_calendar_keyboard(reserved, days, 0, meal_type),
    )


@router.message(F.text == "👤 ویرایش نام")
async def handle_name_edit_start(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        current_name = user.full_name if user else None
    if not user:
        await message.answer("لطفاً ابتدا /start را ارسال کنید.")
        return
    await state.set_state(NameEditState.waiting_for_name)
    await message.answer(
        f"👤 نام فعلی شما: {current_name or 'بدون نام'}\n\n"
        "✏️ نام و نام‌خانوادگی جدید خود را ارسال کنید:",
        reply_markup=get_cancel_keyboard(),
    )


def _get_user_reserved_days(user_id: str, meal_type: str = "lunch") -> list:
    with get_db() as db:
        model = _reservation_model(meal_type)
        return [r.day for r in db.query(model).filter(model.user_id == user_id).all()]


def _get_calendar_lock(user_id: str) -> asyncio.Lock:
    return _calendar_locks.setdefault(user_id, asyncio.Lock())


def _calendar_week_text(all_days: list, week_index: int, meal_type: str = "lunch") -> str:
    total_weeks = (len(all_days) + 6) // 7
    return (
        f"📅 انتخاب روزهای {_meal_label(meal_type)} - هفته {week_index + 1} از {total_weeks}\n\n"
        "روزهای موردنظر این هفته را انتخاب کنید. نام روز هفته کنار تاریخ نمایش داده شده است.\n"
        "پس از انتخاب، با دکمه «هفته بعد» ادامه دهید."
    )


def _blocked_calendar_days() -> list:
    """روزهای تقویم برای انتخاب «بدون غذا»؛ روزهای قبلاً مسدودشده هم قابل انتخاب می‌مانند."""
    blocked = {
        int(item.strip())
        for item in get_setting("blocked_days").split(",")
        if item.strip().isdigit()
    }
    holidays = {
        int(item.strip())
        for item in get_setting("holidays").split(",")
        if item.strip().isdigit()
    }
    days = get_days_details()
    for day_info in days:
        # فقط جمعه و تعطیلات رسمی غیرقابل انتخاب‌اند؛ مسدودی‌های قبلی دوباره باز می‌شوند
        day_info["is_holiday"] = day_info["weekday_num"] == 6 or day_info["day"] in holidays
    return days


def _blocked_week_text(all_days: list, week_index: int, selected_days: list | None = None) -> str:
    total_weeks = (len(all_days) + 6) // 7
    selected_text = ""
    if selected_days:
        selected_text = (
            f"\n✅ انتخاب‌شده: {'، '.join(to_persian_digits(day) for day in sorted(selected_days))}"
            f" ({to_persian_digits(len(selected_days))} روز)"
        )
    return (
        f"📅 انتخاب روزهای بدون غذا - هفته {to_persian_digits(week_index + 1)} از {to_persian_digits(total_weeks)}"
        f"{selected_text}\n\n"
        "روزهایی که غذا سرو نمی‌شود را انتخاب کنید؛ انتخاب‌شده‌ها با ✅ مشخص می‌شوند.\n"
        "پس از اتمام، «✅ پایان و ذخیره» را بزنید."
    )


# ─── Callback ها ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("day_"))
async def cb_toggle_day(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    parts = callback.data.split("_")
    if len(parts) == 4:
        _, meal_type, week_index_str, day_str = parts
    else:
        _, week_index_str, day_str = parts
        meal_type = "lunch"
    week_index, day = int(week_index_str), int(day_str)
    await callback.answer()

    async with _get_calendar_lock(user_id):
        days = get_days_details()
        week_days = days[week_index * 7:(week_index + 1) * 7]
        valid_days = {
            item["day"] for item in week_days
            if not item["is_holiday"]
            and not (meal_type == "dinner" and item["weekday_num"] == 5)
        }
        if day not in valid_days:
            logger.warning("Ignoring invalid calendar day %s for user %s", day, user_id)
            return

        with get_db() as db:
            model = _reservation_model(meal_type)
            existing = db.query(model).filter(
                model.user_id == user_id, model.day == day
            ).first()
            if existing:
                db.delete(existing)
            else:
                db.add(model(user_id=user_id, day=day))
            db.flush()
            reserved = [
                reservation.day
                for reservation in db.query(model).filter(
                    model.user_id == user_id
                ).all()
            ]

        kb = get_weekly_calendar_keyboard(reserved, days, week_index, meal_type)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.exception("Could not refresh calendar keyboard for user %s", user_id)


@router.callback_query(F.data.startswith("week_"))
async def cb_change_calendar_week(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    parts = callback.data.split("_")
    if len(parts) == 4:
        _, meal_type, direction, current_week_str = parts
    else:
        _, direction, current_week_str = parts
        meal_type = "lunch"
    current_week = int(current_week_str)
    await callback.answer()

    async with _get_calendar_lock(user_id):
        days = get_days_details()
        total_weeks = (len(days) + 6) // 7
        step = 1 if direction == "next" else -1
        target_week = max(0, min(current_week + step, total_weeks - 1))
        reserved = _get_user_reserved_days(user_id, meal_type)
        keyboard = get_weekly_calendar_keyboard(reserved, days, target_week, meal_type)
        try:
            await callback.message.edit_text(
                _calendar_week_text(days, target_week, meal_type),
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Could not change calendar week for user %s", user_id)


@router.callback_query(F.data.startswith("confirm_days"))
async def cb_confirm_days(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    parts = callback.data.split("_")
    meal_type = parts[2] if len(parts) > 2 else "lunch"
    await callback.answer()

    async with _get_calendar_lock(user_id):
        with get_db() as db:
            user = db.query(User).filter(User.user_id == user_id).first()
            if not user or user.step != f"{meal_type}_choosing_days":
                return
            first_food = db.query(Food).filter(
                Food.day_num.between(0, 5), Food.meal_type == meal_type
            ).order_by(Food.day_num).first()
            if not first_food:
                logger.error("No food found while confirming days for user %s", user_id)
                return
            user.step = f"{meal_type}_free_{first_food.id}"
            food_data = {"id": first_food.id, "day_name": first_food.day_name, "food_name": first_food.food_name}

        await callback.message.edit_text(
            f"🍗 تعداد غذای آزاد { _meal_label(meal_type) } را مشخص کنید:\n\nروز: {food_data['day_name']}\nغذا: {food_data['food_name']}\n\nتعداد را به‌صورت عدد وارد کنید:",
            reply_markup=get_zero_next_keyboard(),
        )


@router.callback_query(F.data == "zero_next")
async def cb_zero_next(callback: CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        user_step = user.step if user else None
    if user_step and ("_free_" in user_step or user_step == "lunch_containers"):
        await callback.answer()
        await _handle_count_input(callback.message, user_id, "0", user_step)
        return
    await state.clear()
    await callback.answer("به منوی اصلی بازگشتید.")
    await callback.message.answer(get_menu_text(), reply_markup=_main_menu_markup(user_id))


# ─── مدیریت ادمین‌ها (فقط ادمین اصلی) ─────────────────────────────────────────

@router.callback_query(F.data == "admin_mgmt_add")
async def cb_admin_mgmt_add(callback: CallbackQuery, state: FSMContext):
    if not is_primary_admin(str(callback.from_user.id)):
        await callback.answer("فقط ادمین اصلی به این بخش دسترسی دارد.", show_alert=True)
        return
    await state.set_state(AdminManagementState.waiting_for_new_admin_id)
    await callback.message.edit_text(
        "➕ شناسه عددی ادمین جدید را ارسال کنید:",
        reply_markup=get_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_mgmt_remove")
async def cb_admin_mgmt_remove(callback: CallbackQuery):
    if not is_primary_admin(str(callback.from_user.id)):
        await callback.answer("فقط ادمین اصلی به این بخش دسترسی دارد.", show_alert=True)
        return
    primary = settings.admin_ids_list
    extra_ids = [item for item in get_all_admin_ids() if item not in primary]
    if not extra_ids:
        await callback.answer("ادمین اضافه‌ای برای حذف وجود ندارد.", show_alert=True)
        return
    with get_db() as db:
        name_map = {
            user.user_id: user.full_name
            for user in db.query(User).filter(User.user_id.in_(extra_ids)).all()
        }
    extra = [(admin_id, name_map.get(admin_id)) for admin_id in extra_ids]
    await callback.message.edit_text(
        "➖ ادمینی که می‌خواهید حذف کنید انتخاب کنید:",
        reply_markup=get_extra_admins_keyboard(extra),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_mgmt_del_"))
async def cb_admin_mgmt_delete(callback: CallbackQuery, state: FSMContext):
    if not is_primary_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    target_id = callback.data.removeprefix("admin_mgmt_del_")
    if remove_extra_admin(target_id):
        _audit(str(callback.from_user.id), "remove_admin", target_id)
        await callback.message.edit_text(f"✅ ادمین {target_id} حذف شد.")
    else:
        await callback.message.edit_text("ℹ️ این کاربر در فهرست ادمین‌های اضافه نیست.")
    await callback.answer()


@router.callback_query(F.data == "admin_mgmt_cancel")
async def cb_admin_mgmt_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ عملیات لغو شد.")
    await callback.answer()


@router.callback_query(F.data == "admin_mgmt_cancel_text")
async def cb_admin_mgmt_cancel_text(callback: CallbackQuery, state: FSMContext):
    """انصراف از تأیید دو مرحله‌ای افزودن ادمین (پیام متنی، نه ادیت پیام)."""
    await state.clear()
    await callback.message.answer("❌ عملیات لغو شد.")
    await callback.answer()


@router.callback_query(F.data == "confirm_add_admin")
async def cb_confirm_add_admin(callback: CallbackQuery, state: FSMContext):
    if not is_primary_admin(str(callback.from_user.id)):
        await callback.answer("فقط ادمین اصلی به این بخش دسترسی دارد.", show_alert=True)
        return
    data = await state.get_data()
    target_id = data.get("pending_admin_id")
    await state.clear()
    if not target_id or not add_extra_admin(target_id):
        await callback.message.edit_text("ℹ️ این کاربر قبلاً ادمین است یا شناسه معتبر نیست.")
        await callback.answer()
        return
    _audit(str(callback.from_user.id), "add_admin", target_id)
    await callback.message.edit_text(f"✅ کاربر {target_id} به‌عنوان ادمین اضافه شد.")
    await callback.answer()


@router.callback_query(F.data.startswith("delete_my_reg"))
async def cb_delete_my_reg(callback: CallbackQuery):
    parts = callback.data.split("_")
    meal_type = parts[3] if len(parts) > 3 else "lunch"
    details = "روزها، غذاهای آزاد و ظروف" if meal_type == "lunch" else "روزها و غذاهای آزاد"
    await callback.message.edit_text(
        f"⚠️ با حذف ثبت {_meal_label(meal_type)}، تمام اطلاعات قبلی شامل {details} پاک می‌شود.\n"
        "آیا ادامه می‌دهید؟",
        reply_markup=get_confirmation_keyboard(f"confirm_delete_my_reg_{meal_type}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_my_reg_"))
async def cb_confirm_delete_my_reg(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    meal_type = callback.data.removeprefix("confirm_delete_my_reg_")
    _clear_meal_data(user_id, meal_type)

    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        user.step = f"{meal_type}_choosing_days"
        setattr(user, _status_field(meal_type), "pending")
        user.status = "pending"

    reserved = _get_user_reserved_days(user_id, meal_type)
    days = get_days_details()
    await callback.message.edit_text(
        _calendar_week_text(days, 0, meal_type) + f"\n\n🔄 اطلاعات { _meal_label(meal_type) } قبلی پاک شد.",
        reply_markup=get_weekly_calendar_keyboard(reserved, days, 0, meal_type),
    )
    await callback.answer()


@router.message(F.text == "🗑️ حذف حساب کاربری")
async def handle_delete_account_start(message: Message):
    with get_db() as db:
        user_exists = db.query(User).filter(
            User.user_id == str(message.from_user.id)
        ).first() is not None
    if not user_exists:
        await message.answer("حساب کاربری فعالی برای حذف وجود ندارد.")
        return
    await message.answer(
        "⚠️ با حذف حساب، نام، ثبت‌های غذایی، اطلاعات غذای آزاد و ظروف شما برای همیشه پاک می‌شود.\n"
        "آیا مطمئن هستید؟",
        reply_markup=get_delete_account_keyboard(),
    )


@router.callback_query(F.data == "delete_my_account")
async def cb_delete_my_account(callback: CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        "⚠️ این عملیات غیرقابل بازگشت است. حذف کامل حساب را تأیید می‌کنید؟",
        reply_markup=get_confirmation_keyboard(
            f"confirm_delete_my_account_{user_id}",
            confirm_text="✅ حذف شود",
        ),
    )


@router.callback_query(F.data.startswith("confirm_delete_my_account_"))
async def cb_confirm_delete_my_account(callback: CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    target_id = callback.data.removeprefix("confirm_delete_my_account_")
    if target_id != user_id:
        await callback.answer("این درخواست برای حساب شما نیست.", show_alert=True)
        return

    _clear_user_data(user_id)
    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            db.delete(user)
    await state.clear()
    _audit(user_id, "delete_own_account", user_id)
    await callback.message.edit_text(
        "✅ حساب کاربری و تمام اطلاعات ثبت‌شده شما حذف شد.\n"
        "برای شروع دوباره، دکمه زیر را بزنید یا هر زمان /start را ارسال کنید.",
        reply_markup=get_restart_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "restart_bot")
async def cb_restart_bot(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _send_start_flow(callback.message, str(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(callback: CallbackQuery, state: FSMContext):
    """بازگشت از هر پیام طولانی (راهنما، فاکتور و...) به منوی اصلی."""
    await state.clear()
    user_id = str(callback.from_user.id)
    await callback.answer()
    try:
        await callback.message.edit_text(
            get_menu_text(), reply_markup=None
        )
        # کیبورد reply فقط با answer قابل ارسال است؛ پیام جدید با منوی اصلی
        await callback.message.answer(
            "منوی اصلی 👇", reply_markup=_main_menu_markup(user_id)
        )
    except Exception:
        await callback.message.answer(
            get_menu_text(), reply_markup=_main_menu_markup(user_id)
        )


@router.callback_query(F.data == "cancel_flow")
async def cb_cancel_flow(callback: CallbackQuery, state: FSMContext):
    """انصراف از هر جریان چندمرحله‌ای و بازگشت به منوی اصلی."""
    await state.clear()
    user_id = str(callback.from_user.id)
    await callback.answer("عملیات لغو شد ❌")
    try:
        await callback.message.edit_text("❌ عملیات لغو شد.")
    except Exception:
        logger.debug("Could not edit message while cancelling flow")
    await callback.message.answer(
        get_menu_text(), reply_markup=_main_menu_markup(user_id)
    )


@router.callback_query(F.data.startswith("deluser_"))
async def cb_delete_user(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    target_uid = callback.data.split("_")[1]
    await callback.message.edit_text(
        "⚠️ حذف کامل اطلاعات این کاربر قطعی است. آیا ادامه می‌دهید؟",
        reply_markup=get_confirmation_keyboard(f"confirm_deluser_{target_uid}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_deluser_"))
async def cb_confirm_delete_user(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    target_uid = callback.data.removeprefix("confirm_deluser_")
    _clear_user_data(target_uid)
    with get_db() as db:
        user = db.query(User).filter(User.user_id == target_uid).first()
        if user:
            db.delete(user)
    _audit(str(callback.from_user.id), "delete_user", target_uid)
    await callback.message.edit_text("✅ کاربر و تمامی اطلاعات مالی مربوطه با موفقیت حذف شدند.")
    await callback.answer()


@router.callback_query(F.data == "confirm_month_close")
async def cb_confirm_month_close(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    closed_year, closed_month = get_registration_period()
    opened_year, opened_month = get_next_period(closed_year, closed_month)
    await job_monthly_reset(callback.bot)
    _audit(str(callback.from_user.id), "monthly_reset")
    await callback.message.edit_text(
        f"✅ ماه {PERSIAN_MONTHS[closed_month - 1]} بایگانی شد و ثبت‌نام ماه "
        f"{PERSIAN_MONTHS[opened_month - 1]} {to_persian_digits(opened_year)} آغاز شد."
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_action")
async def cb_cancel_action(callback: CallbackQuery):
    await callback.message.edit_text("❌ عملیات لغو شد.")
    await callback.answer()


# ─── جریان تغییر قیمت ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("setprice_"))
async def cb_setprice_select(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    parts = callback.data.split("_")
    if len(parts) == 3:
        _, meal_type, food_id_text = parts
    else:
        _, food_id_text = parts
        meal_type = "lunch"
    food_id = int(food_id_text)
    await state.set_state(PriceEditState.waiting_for_normal)
    with get_db() as db:
        food = db.query(Food).filter(Food.id == food_id).first()
        name = food.food_name if food else "؟"
        normal_price = food.normal_price if food else 0
        free_price = food.free_price if food else 0
    await state.update_data(food_id=food_id, meal_type=meal_type, old_normal_price=normal_price, old_free_price=free_price)
    await callback.message.edit_text(
        f"✏️ {_meal_label(meal_type)}: {name}\n\n"
        f"قیمت عادی (تومان) را ارسال کنید:\n"
        f"قیمت فعلی عادی: {format_price(normal_price)} | آزاد: {format_price(free_price)}",
        reply_markup=get_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_food_"))
async def cb_edit_food_day(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    parts = callback.data.split("_")
    if len(parts) == 4:
        _, _, meal_type, day_num_text = parts
    else:
        _, _, day_num_text = parts
        meal_type = "lunch"
    day_num = int(day_num_text)
    await state.set_state(FoodEditState.waiting_for_name)
    await state.update_data(day_num=day_num)
    await state.update_data(meal_type=meal_type)
    with get_db() as db:
        food = db.query(Food).filter(
            Food.day_num == day_num, Food.meal_type == meal_type
        ).first()
        day_name = food.day_name if food else "روز انتخابی"
        current_food_name = food.food_name if food else ""
    await callback.message.edit_text(
        f"📝 نام جدید برای غذای {_meal_label(meal_type)} در {day_name} را وارد کنید:\n"
        f"نام فعلی: {current_food_name or 'نامشخص'}",
        reply_markup=get_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_archive_"))
async def cb_delete_archive(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    _, _, year_raw, month_raw = callback.data.split("_")
    year = int(year_raw)
    month = int(month_raw)
    with get_db() as db:
        archive_rows = db.query(Archive).filter(Archive.year == year, Archive.month == month).all()
        total_size = sum(len(row.payload or "") for row in archive_rows)
    size_text = f"{total_size / 1024:.1f} کیلوبایت" if total_size >= 1024 else f"{total_size} بایت"
    month_name = PERSIAN_MONTHS[month - 1] if 1 <= month <= 12 else str(month)
    await callback.message.edit_text(
        f"⚠️ آرشیو {year}/{month:02d} ({month_name}) حذف شود؟ این عملیات قابل بازگشت نیست.\n"
        f"تعداد رکوردها: {len(archive_rows)} | حجم تقریبی: {size_text}",
        reply_markup=get_confirmation_keyboard(f"confirm_delete_archive_{year}_{month}"),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"admin_users_lunch", "admin_users_dinner", "admin_users_all"}))
async def cb_admin_users_meal(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    meal_type = callback.data.removeprefix("admin_users_")
    await _show_user_list(callback, state, meal_type)


@router.callback_query(F.data.startswith("user_list_"))
async def cb_user_list(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    meal_type = callback.data.removeprefix("user_list_")
    if meal_type == "back":
        with get_db() as db:
            lunch_count = db.query(User).filter(User.lunch_status == "completed").count()
            dinner_count = db.query(User).filter(User.dinner_status == "completed").count()
            total_count = db.query(User).count()
        await callback.message.edit_text(
            "کاربران کدام بخش نمایش داده شوند؟",
            reply_markup=_users_scope_kb(lunch_count, dinner_count, total_count),
        )
        await callback.answer()
        return
    if meal_type not in {"lunch", "dinner", "all"}:
        await callback.answer()
        return
    await _show_user_list(callback, state, meal_type)


async def _show_user_list(callback: CallbackQuery, state: FSMContext, meal_type: str):
    """نام همه کاربران را به‌صورت دکمه‌های شیشه‌ای نمایش می‌دهد."""
    query = (await state.get_data()).get("admin_user_query", "")
    pending_only = query in {"ناتمام", "pending", "ناتمام‌ها"}
    with get_db() as db:
        users = db.query(User).order_by(User.full_name).all()
        if pending_only:
            users = [user for user in users if getattr(user, _status_field(meal_type)) != "completed"]
        elif query:
            normalized_query = query.casefold()
            users = [
                user for user in users
                if normalized_query in user.user_id.casefold()
                or normalized_query in (user.full_name or "").casefold()
            ]
        user_list = [(user.user_id, user.full_name) for user in users]
    await callback.answer()
    if not user_list:
        await callback.message.edit_text(
            "هیچ کاربری یافت نشد.\n"
            "🔍 جست‌وجو با «us [نام]» و فیلتر ناتمام‌ها با «us ناتمام» انجام می‌شود."
        )
        return
    scope_label = "کل کاربران" if meal_type == "all" else f"کاربران {_meal_label(meal_type)}"
    title = (
        f"⏳ {scope_label} با ثبت ناتمام ({to_persian_digits(len(user_list))} نفر):"
        if pending_only
        else f"👥 {scope_label} ({to_persian_digits(len(user_list))} نفر):"
    )
    await callback.message.edit_text(
        f"{title}\nروی نام کاربر مورد نظر بزنید:",
        reply_markup=get_user_list_keyboard(user_list, meal_type),
    )


@router.callback_query(F.data.startswith("user_card_"))
async def cb_user_card(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    parts = callback.data.split("_")
    if len(parts) != 4:
        await callback.answer()
        return
    _, meal_type, uid = parts[1], parts[2], parts[3]
    with get_db() as db:
        user = db.query(User).filter(User.user_id == uid).first()
        full_name = user.full_name if user else None
        status = getattr(user, _status_field(meal_type), None) if user else None
    if not user:
        await callback.message.edit_text("❌ کاربری با این مشخصات پیدا نشد.")
        await callback.answer()
        return
    if meal_type == "all":
        # کارت ترکیبی: وضعیت و هزینه هر دو وعده
        lunch_total = calculate_user_total(uid, "lunch")
        dinner_total = calculate_user_total(uid, "dinner")
        with get_db() as db:
            u = db.query(User).filter(User.user_id == uid).first()
            lunch_status = u.lunch_status if u else None
            dinner_status = u.dinner_status if u else None
        await callback.message.edit_text(
            f"👤 نام: {full_name}\n"
            f"🆔 شناسه: {uid}\n\n"
            f"🍽️ ناهار — وضعیت: {lunch_status} | هزینه: {format_price(lunch_total)} تومان\n"
            f"🌙 شام — وضعیت: {dinner_status} | هزینه: {format_price(dinner_total)} تومان\n\n"
            "یکی از گزینه‌ها را انتخاب کنید:",
            reply_markup=get_user_card_keyboard(uid, "all"),
        )
        await callback.answer()
        return
    total = calculate_user_total(uid, meal_type)
    await callback.message.edit_text(
        f"👤 نام: {full_name}\n"
        f"🆔 شناسه: {uid}\n"
        f"🍽️ وعده: {_meal_label(meal_type)}\n"
        f"📌 وضعیت: {status}\n"
        f"💰 هزینه: {format_price(total)} تومان\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=get_user_card_keyboard(uid, meal_type),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"admin_price_lunch", "admin_price_dinner"}))
async def cb_admin_price_meal(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    meal_type = callback.data.removeprefix("admin_price_")
    # جمعه ناهار ندارد و پنج‌شنبه شام ندارد؛ از لیست حذف می‌شوند
    excluded = ~(
        ((Food.meal_type == "lunch") & (Food.day_num == 6))
        | ((Food.meal_type == "dinner") & (Food.day_num == 5))
    )
    with get_db() as db:
        foods = db.query(Food).filter(Food.meal_type == meal_type, excluded).order_by(Food.day_num).all()
        food_list = [(food.id, food.day_name, food.food_name, food.normal_price, food.free_price) for food in foods]
    await callback.message.edit_text(
        f"✏️ غذای {_meal_label(meal_type)} را برای تغییر قیمت انتخاب کنید:",
        reply_markup=get_food_list_keyboard(food_list, meal_type),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"admin_name_lunch", "admin_name_dinner"}))
async def cb_admin_name_meal(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    meal_type = callback.data.removeprefix("admin_name_")
    # جمعه ناهار ندارد و پنج‌شنبه شام ندارد؛ از لیست حذف می‌شوند
    excluded = ~(
        ((Food.meal_type == "lunch") & (Food.day_num == 6))
        | ((Food.meal_type == "dinner") & (Food.day_num == 5))
    )
    with get_db() as db:
        foods = [
            (food.day_name, food.food_name, food.day_num)
            for food in db.query(Food).filter(Food.meal_type == meal_type, excluded).order_by(Food.day_num).all()
        ]
    await callback.message.edit_text(
        f"📅 غذای {_meal_label(meal_type)} را برای تغییر نام انتخاب کنید:",
        reply_markup=get_day_edit_keyboard(foods, meal_type),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_edit_user_"))
async def cb_admin_edit_user_meal(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    _, _, _, target_uid, meal_type = callback.data.split("_")
    if meal_type == "all":
        # ویرایش ترکیبی معنا ندارد؛ ادمین باید وعده مشخص انتخاب کند
        await callback.answer("برای ویرایش، ابتدا وعده (ناهار یا شام) را از منوی کاربر انتخاب کنید.", show_alert=True)
        return
    await state.set_state(UserReservationState.waiting_for_days)
    await state.update_data(target_uid=target_uid, meal_type=meal_type)
    reserved = _get_user_reserved_days(target_uid, meal_type)
    days = get_days_details()
    await callback.message.edit_text(
        f"✏️ ویرایش روزهای ثبت {_meal_label(meal_type)} کاربر:\n\n"
        "با دکمه‌های تقویم روزها را انتخاب کنید، یا روزها را به‌صورت متن با کاما جدا بفرستید؛ مثال: ۳،۷،۱۵\n"
        "برای حذف همه، عدد ۰ بفرستید.\n\n"
        "پس از انتخاب، «✅ پایان و ذخیره» را بزنید.",
        reply_markup=get_admin_user_days_keyboard(reserved, days, 0, meal_type),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("aday_"))
async def cb_admin_toggle_day(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    parts = callback.data.split("_")
    _, meal_type, week_index_str, day_str = parts
    week_index, day = int(week_index_str), int(day_str)
    target_uid = (await state.get_data()).get("target_uid")
    if not target_uid:
        await callback.answer("این مرحله دیگر فعال نیست.", show_alert=True)
        return
    await callback.answer()

    async with _get_calendar_lock(target_uid):
        days = get_days_details()
        week_days = days[week_index * 7:(week_index + 1) * 7]
        valid_days = {
            item["day"] for item in week_days
            if not item["is_holiday"]
            and not (meal_type == "dinner" and item["weekday_num"] == 5)
        }
        if day not in valid_days:
            return

        with get_db() as db:
            model = _reservation_model(meal_type)
            existing = db.query(model).filter(
                model.user_id == target_uid, model.day == day
            ).first()
            if existing:
                db.delete(existing)
            else:
                db.add(model(user_id=target_uid, day=day))

        reserved = _get_user_reserved_days(target_uid, meal_type)
        kb = get_admin_user_days_keyboard(reserved, days, week_index, meal_type)
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            logger.exception("Could not refresh admin calendar keyboard")


@router.callback_query(F.data.startswith("aweek_"))
async def cb_admin_change_week(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    parts = callback.data.split("_")
    _, meal_type, direction, current_week_str = parts
    current_week = int(current_week_str)
    target_uid = (await state.get_data()).get("target_uid")
    if not target_uid:
        await callback.answer("این مرحله دیگر فعال نیست.", show_alert=True)
        return
    await callback.answer()

    days = get_days_details()
    total_weeks = (len(days) + 6) // 7
    step = 1 if direction == "next" else -1
    target_week = max(0, min(current_week + step, total_weeks - 1))
    reserved = _get_user_reserved_days(target_uid, meal_type)
    keyboard = get_admin_user_days_keyboard(reserved, days, target_week, meal_type)
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        logger.exception("Could not change admin calendar week")


# ─── تقویم روزهای بدون غذا (ادمین) ───────────────────────────────────────────

async def _refresh_blocked_calendar(callback: CallbackQuery, state: FSMContext, week_index: int):
    data = await state.get_data()
    selected = data.get("days", [])
    days = _blocked_calendar_days()
    try:
        await callback.message.edit_text(
            _blocked_week_text(days, week_index, selected),
            reply_markup=get_blocked_days_keyboard(selected, days, week_index),
        )
    except Exception:
        # متن یا کیبورد تغییری نکرده (مثلاً دو کلیک پشت‌سرهم) — فقط کیبورد تازه می‌شود
        try:
            await callback.message.edit_reply_markup(
                reply_markup=get_blocked_days_keyboard(selected, days, week_index)
            )
        except Exception:
            logger.debug("Could not refresh blocked-days calendar")


@router.callback_query(F.data.startswith("bday_"))
async def cb_blocked_day_toggle(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    if await state.get_state() != BlockedDaysState.waiting_for_days.state:
        await callback.answer("این مرحله دیگر فعال نیست؛ دوباره bd را بزنید.", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer()
        return
    week_index, day = int(parts[1]), int(parts[2])
    data = await state.get_data()
    selected = set(data.get("days", []))
    if day in selected:
        selected.discard(day)
    else:
        selected.add(day)
    await state.update_data(days=sorted(selected))
    await callback.answer()
    await _refresh_blocked_calendar(callback, state, week_index)


@router.callback_query(F.data.startswith("bweek_"))
async def cb_blocked_change_week(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    if await state.get_state() != BlockedDaysState.waiting_for_days.state:
        await callback.answer("این مرحله دیگر فعال نیست؛ دوباره bd را بزنید.", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer()
        return
    _, direction, current_week_str = parts
    current_week = int(current_week_str)
    days = _blocked_calendar_days()
    total_weeks = (len(days) + 6) // 7
    step = 1 if direction == "next" else -1
    target_week = max(0, min(current_week + step, total_weeks - 1))
    await callback.answer()
    await _refresh_blocked_calendar(callback, state, target_week)


@router.callback_query(F.data == "bfinish")
async def cb_blocked_finish(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    if await state.get_state() != BlockedDaysState.waiting_for_days.state:
        await callback.answer("این مرحله دیگر فعال نیست؛ دوباره bd را بزنید.", show_alert=True)
        return
    data = await state.get_data()
    days = sorted(set(data.get("days", [])))
    value = ",".join(map(str, days))
    set_setting("blocked_days", value)
    with get_db() as db:
        db.query(Reservation).filter(Reservation.day.in_(days)).delete(synchronize_session=False)
        db.query(DinnerReservation).filter(DinnerReservation.day.in_(days)).delete(synchronize_session=False)
    await state.clear()
    _audit(str(callback.from_user.id), "set_blocked_days", details=value)
    if days:
        days_text = "، ".join(to_persian_digits(day) for day in days)
        await callback.message.edit_text(
            f"✅ روزهای بدون غذا ذخیره شد ({to_persian_digits(len(days))} روز):\n"
            f"📅 {days_text}"
        )
    else:
        await callback.message.edit_text("✅ هیچ روز بدون غذایی ثبت نشد؛ فهرست قبلی پاک شد.")
    await callback.answer("ذخیره شد ✅")


@router.callback_query(F.data == "afinish")
async def cb_admin_finish_days(callback: CallbackQuery, state: FSMContext):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    data = await state.get_data()
    target_uid = data.get("target_uid")
    meal_type = data.get("meal_type", "lunch")
    if not target_uid:
        await callback.answer("این مرحله دیگر فعال نیست.", show_alert=True)
        return
    days = sorted(set(_get_user_reserved_days(target_uid, meal_type)))
    with get_db() as db:
        model = _reservation_model(meal_type)
        db.query(model).filter(model.user_id == target_uid).delete()
        db.add_all(model(user_id=target_uid, day=day) for day in days)
    await state.clear()
    _audit(str(callback.from_user.id), "edit_user_meal", target_uid, f"{meal_type}:{','.join(map(str, days))}")
    total = calculate_user_total(target_uid, meal_type)
    days_text = "، ".join(to_persian_digits(d) for d in days) if days else "هیچ روزی"
    await callback.message.edit_text(
        f"✅ ثبت‌های {_meal_label(meal_type)} کاربر با موفقیت به‌روزرسانی شد.\n"
        f"📅 روزهای ثبت‌شده: {days_text}\n"
        f"💰 مبلغ جدید: {format_price(total)} تومان"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_archive_"))
async def cb_confirm_delete_archive(callback: CallbackQuery):
    if not is_admin(str(callback.from_user.id)):
        await callback.answer()
        return
    _, _, _, year_raw, month_raw = callback.data.split("_")
    year = int(year_raw)
    month = int(month_raw)
    with get_db() as db:
        deleted = db.query(Archive).filter(Archive.year == year, Archive.month == month).delete()
    if deleted:
        month_name = PERSIAN_MONTHS[month - 1] if 1 <= month <= 12 else str(month)
        await callback.message.edit_text(
            f"✅ {to_persian_digits(deleted)} آرشیو ماه {month_name} {to_persian_digits(year)} حذف شد."
        )
    else:
        await callback.message.edit_text("ℹ️ آرشیوی برای این ماه یافت نشد.")
    await callback.answer()


# ─── پیام‌های متنی ─────────────────────────────────────────────────────────────

async def _handle_count_input(message: Message, user_id: str, text: str, user_step: str):
    try:
        count = to_int(text)
    except (TypeError, ValueError):
        await message.answer("❌ لطفاً صفر یا یک عدد صحیح مثبت وارد کنید:")
        return

    if "_free_" in user_step:
        meal_type, _, food_id_text = user_step.partition("_free_")
        food_id = int(food_id_text)
        with get_db() as db:
            user = db.query(User).filter(User.user_id == user_id).first()
            if not user or user.step != user_step:
                return
            order = db.query(FreeOrder).filter(
                FreeOrder.user_id == user_id, FreeOrder.food_id == food_id
            ).first()
            if order:
                order.count = count
            else:
                db.add(FreeOrder(user_id=user_id, food_id=food_id, count=count))
            current_food = db.query(Food).filter(Food.id == food_id).first()
            next_food = None
            if current_food:
                next_food = db.query(Food).filter(
                    Food.day_num > current_food.day_num,
                    Food.day_num <= 5,
                    Food.meal_type == meal_type,
                ).order_by(Food.day_num).first()
            if next_food:
                user.step = f"{meal_type}_free_{next_food.id}"
                next_text = f"🍗 تعداد غذای آزاد {_meal_label(meal_type)} را مشخص کنید:\n\nروز: {next_food.day_name}\nغذا: {next_food.food_name}\n\nتعداد را به‌صورت عدد وارد کنید:"
                next_markup = get_zero_next_keyboard()
            elif meal_type == "lunch":
                user.step = "lunch_containers"
                try:
                    container_price = int(get_setting("container_price") or 0)
                except ValueError:
                    container_price = 0
                next_text = (
                    "📦 تعداد ظروف یک‌بار مصرف استفاده‌شده برای ناهار را وارد کنید:\n\n"
                    f"💰 قیمت هر ظرف: {format_price(container_price)} تومان\n\n"
                    "تعداد را به‌صورت عدد وارد کنید:"
                )
                next_markup = get_zero_next_keyboard()
            else:
                user.step = "dinner_done"
                setattr(user, _status_field(meal_type), "completed")
                next_text = None
                next_markup = None
        if next_text is None:
            await _send_final_invoice(message, user_id, meal_type)
        else:
            await message.answer(next_text, reply_markup=next_markup)
        return

    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        if not user or user.step != "lunch_containers":
            return
        container = db.query(UserContainer).filter(UserContainer.user_id == user_id).first()
        if container:
            container.count = count
        else:
            db.add(UserContainer(user_id=user_id, count=count))
        user.step = "lunch_done"
        user.lunch_status = "completed"
        user.status = "completed"
    await _send_final_invoice(message, user_id, "lunch")


async def _send_final_invoice(message: Message, user_id: str, meal_type: str):
    """فاکتور نهایی + جمع‌بندی کوتاه + دکمه بازگشت به منو."""
    meal_name = _meal_label(meal_type)
    await message.answer(build_invoice_text(user_id, meal_type))
    await message.answer(
        f"✅ {meal_name} شما نهایی شد — {registration_deadline_notice()}",
        reply_markup=get_back_to_menu_keyboard(),
    )


@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    text = message.text.strip()

    # نشانگرهای وضعیت (⏳/✅) انتهای دکمه‌های وعده را حذف کن تا متن خام مقایسه شود
    if text not in _MEAL_MENU_BUTTONS:
        for mark in (" ⏳", " ✅"):
            if text.endswith(mark) and text[: -len(mark)] in _MEAL_MENU_BUTTONS:
                text = text[: -len(mark)]
                break

    if text in {
        "🍽️ ثبت / ویرایش غذای ناهار",
        "🍽️ ثبت / ویرایش رزرو غذا",
    }:
        await _start_meal_registration(message, "lunch", state)
        return
    if text == "🌙 ثبت / ویرایش غذای شام":
        await _start_meal_registration(message, "dinner", state)
        return

    with get_db() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        user_step = user.step if user else None
    admin_command = is_admin(user_id) and _is_admin_command(text)
    current_state = await state.get_state()

    if admin_command:
        await state.clear()
        current_state = None

    if is_admin(user_id) and current_state == AccessCodeState.waiting_for_code.state:
        code = _normalize_access_code(text)
        if not _is_valid_access_code(text):
            await message.answer("❌ کد معتبر نیست. دوباره وارد کنید:")
            return
        set_setting("registration_access_code", code)
        await state.clear()
        _audit(user_id, "change_registration_access_code")
        await message.answer("✅ کد ورود ثبت‌نام با موفقیت تغییر کرد.")
        return

    if is_primary_admin(user_id) and current_state == AdminManagementState.waiting_for_new_admin_id.state:
        target_id = text.strip()
        if not target_id.isdigit():
            await message.answer("❌ شناسه باید عدد باشد. دوباره ارسال کنید:")
            return
        if is_admin(target_id):
            await state.clear()
            await message.answer("ℹ️ این کاربر قبلاً ادمین است.")
            return
        with get_db() as db:
            target_user = db.query(User).filter(User.user_id == target_id).first()
            target_name = target_user.full_name if target_user else None
        if not target_user:
            await state.clear()
            await message.answer(
                f"❌ کاربری با شناسه {target_id} در ربات ثبت‌نام نکرده است.\n"
                "ابتدا از او بخواهید ربات را استارت کند و ثبت‌نام شود، سپس دوباره تلاش کنید."
            )
            return
        # تأیید دو مرحله‌ای: نمایش نام کاربر و گرفتن تأیید قبل از اعمال
        await state.set_state(AdminManagementState.waiting_for_confirm)
        await state.update_data(pending_admin_id=target_id)
        await message.answer(
            f"⚠️ تأیید نهایی:\n\n"
            f"👤 نام: {target_name or 'بدون نام'}\n"
            f"🆔 شناسه: {target_id}\n\n"
            "این کاربر به‌عنوان ادمین اضافه شود؟",
            reply_markup=get_confirmation_keyboard(
                "confirm_add_admin",
                cancel_data="admin_mgmt_cancel_text",
                confirm_text="✅ بله، ادمین شود",
            ),
        )
        return

    if current_state == NameEditState.waiting_for_name.state and not admin_command:
        if not text:
            await message.answer("❌ نام نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return
        with get_db() as db:
            user = db.query(User).filter(User.user_id == user_id).first()
            if not user:
                await state.clear()
                await message.answer("لطفاً ابتدا /start را ارسال کنید.")
                return
            is_first_registration = user.step == "get_name"
            user.full_name = text
            if user.step == "get_name":
                user.step = "main_menu"
        await state.clear()
        success_text = "✅ نام شما با موفقیت ثبت شد." if is_first_registration else "✅ نام شما با موفقیت ویرایش شد."
        await message.answer(success_text, reply_markup=_main_menu_markup(user_id))
        return

    # ثبت‌نام اولیه
    if user_step == "get_access_code" and not admin_command:
        if not _is_valid_access_code(text):
            fails = (await state.get_data()).get("access_code_fails", 0) + 1
            await state.update_data(access_code_fails=fails)
            help_line = ""
            if fails >= 3:
                help_line = "\n⚠️ برای دریافت کد ورود به معاونت اجرایی موسسه مراجعه کنید."
            await message.answer("❌ کد ورود نادرست است. دوباره وارد کنید:" + help_line)
            return
        with get_db() as db:
            current_user = db.query(User).filter(User.user_id == user_id).first()
            if not current_user:
                await message.answer("لطفاً ابتدا /start را ارسال کنید.")
                return
            current_user.step = "get_name"
        await state.clear()
        await message.answer(
            "✅ کد صحیح است. لطفاً نام و نام خانوادگی خود را وارد کنید:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if (not user or user_step == "get_name") and not admin_command:
        is_first_registration = False
        if text == "🚀 شروع ثبتنام":
            await message.answer(
                "لطفاً ابتدا نام و نام‌خانوادگی خود را ارسال کنید:",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        if not text:
            await message.answer("نام معتبر نیست، دوباره وارد کنید:")
            return
        with get_db() as db:
            u = db.query(User).filter(User.user_id == user_id).first()
            if u:
                u.full_name = text
                u.step = "main_menu"
            else:
                db.add(User(user_id=user_id, full_name=text, step="main_menu", status="pending"))
        await message.answer("✅ نام شما با موفقیت ثبت شد.")
        await message.answer(get_menu_text(), reply_markup=_main_menu_markup(user_id))
        # اطلاع‌رسانی تکمیل ثبت‌نام به ادمین‌ها — فقط یک پیام شامل نام و شناسه
        if not is_admin(user_id) and is_first_registration:
            try:
                from main import notify_admins
                import asyncio
                asyncio.create_task(notify_admins(
                    message.bot,
                    f"🆕 ثبت‌نام جدید:\n👤 نام: {text}\n🆔 شناسه: {user_id}",
                ))
            except Exception:
                logger.exception("Could not notify admins about new registration")
        return

    if is_admin(user_id) and current_state == RegistrationWindowState.waiting_for_start.state:
        try:
            start_day = to_int(text)
        except ValueError:
            await message.answer("❌ عدد معتبر نیست. روز شروع را دوباره بفرستید:")
            return
        if not 1 <= start_day <= 31:
            await message.answer("❌ روز شروع باید بین ۱ تا ۳۱ باشد:")
            return
        await state.update_data(start_day=start_day)
        await state.set_state(RegistrationWindowState.waiting_for_end)
        await message.answer("روز پایان بازه را به‌صورت عدد بفرستید:")
        return

    if is_admin(user_id) and current_state == RegistrationWindowState.waiting_for_end.state:
        try:
            end_day = to_int(text)
        except ValueError:
            await message.answer("❌ عدد معتبر نیست. روز پایان را دوباره بفرستید:")
            return
        start_day = (await state.get_data()).get("start_day")
        if not start_day or not start_day <= end_day <= 31:
            await message.answer("❌ روز پایان باید از شروع بازه بزرگ‌تر یا مساوی و حداکثر ۳۱ باشد:")
            return
        await state.update_data(end_day=end_day)
        await state.set_state(RegistrationWindowState.waiting_for_start_offset)
        await message.answer(
            "📅 فاصله ماه شروع را بفرستید:\n"
            "برای ماه جاری عدد ۰، ماه قبل عدد ۱- و ماه بعد عدد ۱ — برای بازه معمولی عدد ۰:"
        )
        return

    if is_admin(user_id) and current_state == RegistrationWindowState.waiting_for_start_offset.state:
        try:
            start_offset = int(text)
        except ValueError:
            await message.answer("❌ فاصله ماه باید عدد صحیح باشد (۰ = ماه جاری، ۱- = ماه قبل، ۱ = ماه بعد):")
            return
        await state.update_data(start_offset=start_offset)
        await state.set_state(RegistrationWindowState.waiting_for_end_offset)
        await message.answer(
            "📅 فاصله ماه پایان را بفرستید:\n"
            "برای ماه جاری عدد ۰، ماه قبل عدد ۱- و ماه بعد عدد ۱ — برای بازه معمولی عدد ۰:"
        )
        return

    if is_admin(user_id) and current_state == RegistrationWindowState.waiting_for_end_offset.state:
        try:
            end_offset = int(text)
        except ValueError:
            await message.answer("❌ فاصله ماه باید عدد صحیح باشد (۰ = ماه جاری، ۱- = ماه قبل، ۱ = ماه بعد):")
            return
        data = await state.get_data()
        start_day, end_day = data.get("start_day"), data.get("end_day")
        start_offset = data.get("start_offset", 0)
        if not -12 <= start_offset <= end_offset <= 12:
            await message.answer("❌ فاصله ماه‌ها باید بین ۱۲- تا ۱۲ و به‌ترتیب باشد:")
            return
        set_setting("registration_start_day", str(start_day))
        set_setting("registration_end_day", str(end_day))
        set_setting("registration_start_month_offset", str(start_offset))
        set_setting("registration_end_month_offset", str(end_offset))
        await state.clear()
        _audit(user_id, "set_registration_window", details=f"{start_day}/{start_offset}-{end_day}/{end_offset}")
        window_text = _window_label(start_day, start_offset, end_day, end_offset)
        await message.answer(f"✅ بازه ثبت‌نام از روز {start_day} تا روز {end_day} ({window_text}) تنظیم شد.")
        return

    if is_admin(user_id) and current_state == BlockedDaysState.waiting_for_days.state:
        if text.strip().casefold() in {"پایان", "تمام", "done", "end"}:
            days = sorted((await state.get_data()).get("days", []))
            value = ",".join(map(str, days))
            set_setting("blocked_days", value)
            with get_db() as db:
                db.query(Reservation).filter(Reservation.day.in_(days)).delete(synchronize_session=False)
                db.query(DinnerReservation).filter(DinnerReservation.day.in_(days)).delete(synchronize_session=False)
            await state.clear()
            _audit(user_id, "set_blocked_days", details=value)
            await message.answer("✅ فهرست روزهای بدون غذا ذخیره شد.")
            return
        if "،" in text or "," in text or " " in text.strip():
            await message.answer("❌ هر تاریخ را جداگانه و در یک خط بفرستید:")
            return
        try:
            day = to_int(text)
        except ValueError:
            await message.answer("❌ تاریخ معتبر نیست. هر تاریخ را در یک خط بفرستید:")
            return
        if not 1 <= day <= 31:
            await message.answer("❌ تاریخ باید بین ۱ تا ۳۱ باشد:")
            return
        days = set((await state.get_data()).get("days", []))
        days.add(day)
        await state.update_data(days=sorted(days))
        await message.answer("✅ ثبت شد. تاریخ بعدی را در یک خط بفرستید یا برای پایان «پایان» را ارسال کنید:")
        return

    if user_step and (("_free_" in user_step) or user_step == "lunch_containers") and not admin_command:
        async with _get_calendar_lock(user_id):
            with get_db() as db:
                current_user = db.query(User).filter(User.user_id == user_id).first()
                current_step = current_user.step if current_user else None
            if current_step == user_step:
                await _handle_count_input(message, user_id, text, current_step)
        return

    # جریان تغییر قیمت (ادمین در حال وارد کردن عدد)
    if is_admin(user_id) and current_state in {
        PriceEditState.waiting_for_normal.state,
        PriceEditState.waiting_for_free.state,
    }:
        edit_data = await state.get_data()
        try:
            value = to_int(text)
            if value < 0:
                raise ValueError
        except ValueError:
            await state.clear()
            await message.answer("❌ لطفاً صفر یا عدد مثبت وارد کنید.")
            return
        if current_state == PriceEditState.waiting_for_normal.state:
            await state.update_data(normal_price=value)
            await state.set_state(PriceEditState.waiting_for_free)
            await message.answer("قیمت آزاد (تومان) را ارسال کنید:", reply_markup=get_cancel_keyboard())
            return
        elif current_state == PriceEditState.waiting_for_free.state:
            food_id = edit_data["food_id"]
            normal_price = edit_data["normal_price"]
            free_price = value
            with get_db() as db:
                food = db.query(Food).filter(Food.id == food_id).first()
                if food:
                    food.normal_price = normal_price
                    food.free_price = free_price
            await state.clear()
            _audit(user_id, "edit_food_price", str(food_id), f"normal={normal_price},free={value}")
            await message.answer(
                "✅ قیمت غذا بروزرسانی شد.\n"
                f"💵 عادی: {format_price(edit_data.get('old_normal_price', 0))} → {format_price(normal_price)} تومان\n"
                f"💵 آزاد: {format_price(edit_data.get('old_free_price', 0))} → {format_price(free_price)} تومان"
            )
            return

    if is_admin(user_id) and current_state == ContainerPriceState.waiting_for_price.state:
        try:
            value = to_int(text)
            if value < 0:
                raise ValueError
        except ValueError:
            await state.clear()
            await message.answer("❌ لطفاً صفر یا عدد مثبت وارد کنید.")
            return
        try:
            current_price = to_int(get_setting("container_price") or 0)
        except ValueError:
            current_price = 0
        set_setting("container_price", str(value))
        await state.clear()
        _audit(user_id, "edit_container_price", details=str(value))
        await message.answer(
            f"✅ قیمت ظرف یک‌بار مصرف از {format_price(current_price)} به {format_price(value)} تومان بروزرسانی شد."
        )
        return

    if is_admin(user_id) and current_state == FoodEditState.waiting_for_name.state:
        edit_data = await state.get_data()
        day_num = edit_data["day_num"]
        meal_type = edit_data.get("meal_type", "lunch")
        if not text.strip():
            await message.answer("❌ نام غذا نمی‌تواند خالی باشد.")
            return
        with get_db() as db:
            food = db.query(Food).filter(
                Food.day_num == day_num, Food.meal_type == meal_type
            ).first()
            if food:
                food.food_name = text.strip()
        await state.clear()
        _audit(user_id, "edit_food_name", str(day_num), text.strip())
        await message.answer(
            f"✅ نام غذای {_meal_label(meal_type)} در روز انتخابی به «{text.strip()}» تغییر کرد."
        )
        return

    if is_admin(user_id) and current_state == UserReservationState.waiting_for_days.state:
        edit_data = await state.get_data()
        target_uid = edit_data.get("target_uid")
        meal_type = edit_data.get("meal_type", "lunch")
        try:
            if text == "0":
                days = []
            else:
                days = sorted({to_int(item) for item in text.replace("،", ",").split(",") if item.strip()})
                valid_days = {
                    item["day"] for item in get_days_details()
                    if not item["is_holiday"]
                    and not (meal_type == "dinner" and item["weekday_num"] == 5)
                }
                if any(day not in valid_days for day in days):
                    raise ValueError
        except ValueError:
            await message.answer("❌ روزها را به‌صورت عددهای معتبر و جداشده با کاما وارد کنید؛ مثال: ۳،۷،۱۵")
            return
        with get_db() as db:
            model = _reservation_model(meal_type)
            db.query(model).filter(model.user_id == target_uid).delete()
            db.add_all(model(user_id=target_uid, day=day) for day in days)
        await state.clear()
        _audit(user_id, "edit_user_meal", target_uid, f"{meal_type}:{','.join(map(str, days))}")
        total = calculate_user_total(target_uid, meal_type)
        days_text = "، ".join(to_persian_digits(d) for d in days) if days else "هیچ روزی"
        await message.answer(
            f"✅ ثبت‌های {_meal_label(meal_type)} کاربر با موفقیت به‌روزرسانی شد.\n"
            f"📅 روزهای ثبت‌شده: {days_text}\n"
            f"💰 مبلغ جدید: {format_price(total)} تومان"
        )
        return

    # منوی ادمین
    if text == "⚙️ منوی مدیریت ادمین" and is_admin(user_id):
        await message.answer(
            "🛠️ راهنمای دستورات ادمین:\n\n"
            "🔹 ex : خروجی اکسل جاری و آرشیوها\n"
            "🔹 us : نمایش کاربران؛ ابتدا وعده را انتخاب کنید\n"
            "🔹 eu : ویرایش ثبت‌های یک کاربر\n"
            "🔹 st : آمار دوره فعال\n"
            "🔹 lg : آخرین لاگ‌های ادمین\n"
            "🔹 rm : حذف آرشیو\n"
            "🔹 dt : نمایش تاریخ شمسی\n"
            "🔹 pr : تغییر قیمت غذا\n"
            "🔹 mn : تغییر نام غذا\n"
            "🔹 ho : تنظیم تعطیلات رسمی\n"
            "🔹 bd : انتخاب روزهای بدون غذا از روی تقویم؛ سپس «پایان و ذخیره»\n"
            "🔹 sc : تغییر قیمت ظرف ناهار\n"
            "🔹 cm : پایان ماه جاری و شروع ماه بعد\n"
            "🔹 cn : لغو شروع ثبت‌نام ماه بعد\n"
            "🔹 rg : فعال/غیرفعال‌کردن ثبت‌نام\n"
            "🔹 dl : تغییر بازه ثبت‌نام؛ روز شروع، روز پایان و فاصله ماه‌ها\n"
            "🔹 vc : مشاهده کد ورود فعلی\n"
            "🔹 ma : مدیریت ادمین‌ها (فقط ادمین اصلی)\n"
            "🔹 cc : تغییر کد ورود؛ سپس کد جدید را ارسال کنید\n"
            "🔹 ⚙️ منوی مدیریت ادمین : نمایش همین راهنما",
            reply_markup=get_back_to_menu_keyboard(),
        )
        return

    if not is_admin(user_id):
        await message.answer(
            "🤔 متوجه نشدم؛ لطفاً یکی از گزینه‌های منو را انتخاب کنید.",
            reply_markup=_main_menu_markup(user_id),
        )
        return

    cmd = _get_command(text)

    if cmd == "ex":
        await _send_excel(message)

    elif cmd == "vc":
        await message.answer(f"🔐 کد ورود فعلی ثبت‌نام: {get_setting('registration_access_code')}")

    elif cmd == "ma":
        if not is_primary_admin(user_id):
            await message.answer("❌ فقط ادمین اصلی به مدیریت ادمین‌ها دسترسی دارد.")
            return
        primary = settings.admin_ids_list
        extra = [item for item in get_all_admin_ids() if item not in primary]
        lines = ["👥 مدیریت ادمین‌ها\n"]
        lines += [f"⭐ {item} (اصلی)" for item in primary]
        lines += [f"• {item}" for item in extra] or ["• ادمین اضافه‌ای ثبت نشده."]
        await message.answer(
            "\n".join(lines),
            reply_markup=get_admin_management_keyboard(),
        )

    elif cmd == "cc":
        await state.set_state(AccessCodeState.waiting_for_code)
        await message.answer("کد ورود جدید را ارسال کنید:", reply_markup=get_cancel_keyboard())

    elif cmd in {"us", "user", "users", "یوزر", "یوزرز"}:
        query = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        await state.update_data(admin_user_query=query)
        with get_db() as db:
            lunch_count = db.query(User).filter(User.lunch_status == "completed").count()
            dinner_count = db.query(User).filter(User.dinner_status == "completed").count()
            total_count = db.query(User).count()
        await message.answer(
            "کاربران کدام بخش نمایش داده شوند؟",
            reply_markup=_users_scope_kb(lunch_count, dinner_count, total_count),
        )

    elif cmd in {"edituser", "eu", "ویرایشکاربر"}:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await message.answer(
                "فرمت صحیح: edituser [شناسه کاربر]\n"
                "مثال: edituser 123456789\n"
                "معادل فارسی: ویرایشکاربر 123456789"
            )
            return
        target_uid = parts[1].strip()
        with get_db() as db:
            target = db.query(User).filter(User.user_id == target_uid).first()
        if not target:
            await message.answer(f"❌ کاربری با شناسه {target_uid} پیدا نشد.")
            return
        await message.answer(
            f"ویرایش ثبت‌های {target.full_name} برای کدام وعده انجام شود؟",
            reply_markup=get_meal_choice_keyboard(f"admin_edit_user_{target_uid}"),
        )

    elif cmd in {"st", "stats", "stat", "آمار"}:
        await _send_stats(message)

    elif cmd in {"lg", "audit", "logs", "لاگ"}:
        await _send_audit_logs(message)

    elif cmd == "rm":
        with get_db() as db:
            months = [
                (year, month)
                for year, month in db.query(Archive.year, Archive.month)
                .filter(Archive.year.isnot(None), Archive.month.isnot(None))
                .group_by(Archive.year, Archive.month)
                .order_by(Archive.year, Archive.month)
                .all()
            ]
        if not months:
            await message.answer("هیچ آرشیوی برای حذف وجود ندارد.")
            return
        await message.answer(
            "📁 ماه مورد نظر برای حذف آرشیو را انتخاب کنید:\n"
            "⚠️ حذف آرشیو فقط گزارش ذخیره‌شده را پاک می‌کند و به ثبت‌نام جاری آسیبی نمی‌زند.",
            reply_markup=get_archive_month_keyboard(months),
        )

    elif cmd == "rg":
        current = get_setting("registration_status")
        new_status = "inactive" if current == "active" else "active"
        set_setting("registration_status", new_status)
        if new_status == "inactive":
            set_setting("registration_manual_override", "false")
        else:
            set_setting(
                "registration_manual_override",
                "false" if is_registration_window_day() else "true",
            )
        _audit(user_id, "toggle_registration_status", details=new_status)
        label = "بسته ❌" if new_status == "inactive" else "باز ✅"
        await message.answer(f"ثبت‌نام کاربران اکنون {label} است.")
        await _notify_admins(
            message.bot,
            admin_notice_text(
                f"📢 ثبت‌نام ماه {registration_period_text()} {('فعال شد ✅' if new_status == 'active' else 'غیرفعال شد ❌')}",
                user_id,
                f"🗓️ بازه ثبت‌نام: {registration_window_text()}",
            ),
        )

    elif cmd in {"cn", "cancelnext", "لغوماه", "لغوشروعماه"}:
        set_setting("registration_status", "inactive")
        set_setting("registration_manual_override", "false")
        _audit(user_id, "cancel_next_registration")
        await message.answer(
            f"✅ فعال‌سازی ثبت‌نام ماه بعد ({registration_period_text()}) لغو شد و ثبت‌نام اکنون غیرفعال است."
        )
        await _notify_admins(
            message.bot,
            admin_notice_text(
                f"📢 ثبت‌نام ماه {registration_period_text()} غیرفعال شد ❌ و فعال‌سازی ماه بعد لغو گردید.",
                user_id,
            ),
        )

    elif cmd in {"dl", "deadline", "مهلت", "بازهثبتنام"}:
        start_day, end_day, start_offset, end_offset = get_registration_window_config()
        await state.set_state(RegistrationWindowState.waiting_for_start)
        await state.update_data(start_day=None)
        await message.answer(
            f"بازه فعلی: {_window_label(start_day, start_offset, end_day, end_offset)}.\n"
            "عدد روز شروع بازه را بفرستید:",
            reply_markup=get_cancel_keyboard(),
        )

    elif cmd == "dt":
        now = jdatetime.datetime.now()
        await message.answer(
            f"🧪 تست تاریخ شمسی\n\n"
            f"📅 تاریخ: {now.year}/{now.month:02d}/{now.day:02d}\n"
            f"🕒 ساعت: {now.hour:02d}:{now.minute:02d}:{now.second:02d}"
        )

    elif cmd == "pr":
        await message.answer(
            "قیمت کدام وعده تغییر کند؟",
            reply_markup=get_meal_choice_keyboard("admin_price"),
        )

    elif cmd in {"mn", "menu", "منو", "تغییر", "تغییرنام", "تغییرنامغذا", "changename"}:
        await message.answer(
            "نام غذای کدام وعده تغییر کند؟",
            reply_markup=get_meal_choice_keyboard("admin_name"),
        )

    elif cmd == "sc":
        parts = text.split()
        if len(parts) == 2:
            try:
                value = to_int(parts[1])
                if value < 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ لطفاً صفر یا عدد مثبت وارد کنید.")
                return
            old_price = get_setting("container_price")
            set_setting("container_price", str(value))
            try:
                old_price_num = to_int(old_price)
            except (TypeError, ValueError):
                old_price_num = 0
            await message.answer(
                f"✅ قیمت ظرف یک‌بار مصرف از {format_price(old_price_num)} به {format_price(value)} تومان بروزرسانی شد."
            )
            return
        current_price = to_int(get_setting("container_price") or 0)
        await state.set_state(ContainerPriceState.waiting_for_price)
        await message.answer(
            f"💰 قیمت جدید ظرف یک‌بار مصرف را به‌صورت عدد ارسال کنید:\n"
            f"قیمت فعلی: {format_price(current_price)} تومان",
            reply_markup=get_cancel_keyboard(),
        )

    elif cmd == "ho":
        parts = text.split(maxsplit=1)
        days_str = parts[1] if len(parts) > 1 else ""
        set_setting("holidays", days_str)
        _audit(user_id, "set_holidays", details=days_str)
        await message.answer(f"✅ روزهای تعطیل رسمی به این لیست تغییر یافت:\n{_holidays_label(days_str)}")

    elif cmd == "bd":
        await state.set_state(BlockedDaysState.waiting_for_days)
        await state.update_data(days=[])
        days = _blocked_calendar_days()
        await message.answer(
            _blocked_week_text(days, 0),
            reply_markup=get_blocked_days_keyboard([], days, 0),
        )

    elif cmd in {"بستن", "close", "closemonth", "close_month", "cm", "reset", "شروعماه"}:
        year, month = get_registration_period()
        next_year, next_month = get_next_period(year, month)
        await message.answer(
            f"⚠️ پرونده ماه {PERSIAN_MONTHS[month - 1]} در آرشیو ذخیره می‌شود و ثبت‌نام ماه "
            f"{PERSIAN_MONTHS[next_month - 1]} باز می‌شود.\n"
            "آمار ماه جاری در آرشیو باقی می‌ماند و برای ثبت‌نام بعدی جدا می‌شود.\n"
            "آیا مطمئن هستید؟",
            reply_markup=get_confirmation_keyboard(
                "confirm_month_close",
                confirm_text="✅ بله، ماه را ببند",
            ),
        )



async def _send_excel(message: Message):
    try:
        year, month = get_registration_period()
        current_caption = f"📊 گزارش ماه {PERSIAN_MONTHS[month - 1]}"
        reports = [(build_excel_report(), current_caption)]
        with get_db() as db:
            archives = db.query(Archive).filter(Archive.payload.isnot(None)).order_by(Archive.year, Archive.month).all()
            archive_data = [(archive.year, archive.month, archive.payload) for archive in archives if archive.year and archive.month]
        for year, month, payload in archive_data:
            try:
                caption = f"📊 آرشیو {PERSIAN_MONTHS[month - 1]} {to_persian_digits(year)}"
                reports.append((build_excel_report(json.loads(payload), file_suffix=f"_archive_{year}_{month:02d}"), caption))
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Skipping invalid archive %s-%s", year, month)
        from aiogram.types import BufferedInputFile
        for file_name, caption in reports:
            with open(file_name, "rb") as f:
                await message.answer_document(BufferedInputFile(f.read(), filename=file_name), caption=caption)
            os.remove(file_name)
    except Exception as e:
        logger.error(f"Excel report error: {e}")
        await message.answer("❌ خطا در ساخت فایل گزارش.")


async def _send_stats(message: Message):
    with get_db() as db:
        users = db.query(User).all()
        reservations = db.query(Reservation).count() + db.query(DinnerReservation).count()
        free_orders = db.query(FreeOrder).all()
        containers = sum((item.count or 0) for item in db.query(UserContainer).all())
        lunch_completed = sum(user.lunch_status == "completed" for user in users)
        dinner_completed = sum(user.dinner_status == "completed" for user in users)
        food_counts = {}
        for order in free_orders:
            food = db.query(Food).filter(Food.id == order.food_id).first()
            if food:
                key = f"{_meal_label(food.meal_type)}: {food.food_name}"
                food_counts[key] = food_counts.get(key, 0) + order.count
    top_foods = "\n".join(
        f"• {name}: {count} غذای آزاد" for name, count in sorted(food_counts.items(), key=lambda item: -item[1])[:5]
    ) or "• موردی ثبت نشده است"
    total_amount = sum(calculate_user_total(user.user_id) for user in users)
    completion_rate = 0
    if users:
        completion_rate = round(
            100 * sum(
                user.lunch_status == "completed" and user.dinner_status == "completed"
                for user in users
            ) / len(users)
        )
    await message.answer(
        "📊 آمار دوره فعال\n\n"
        f"👥 کل کاربران: {len(users)}\n"
        f"✅ ناهار تکمیل‌شده: {lunch_completed}\n"
        f"✅ شام تکمیل‌شده: {dinner_completed}\n"
        f"📈 درصد تکمیل ثبت‌نام (هر دو وعده): {to_persian_digits(completion_rate)}٪\n"
        f"📅 مجموع روزهای ثبت‌شده: {reservations}\n"
        f"📦 مجموع ظروف: {containers}\n"
        f"💰 مبلغ کل دوره: {format_price(total_amount)} تومان\n\n"
        f"🍽️ بیشترین غذاهای آزاد:\n{top_foods}"
    )


async def _send_audit_logs(message: Message):
    with get_db() as db:
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(20).all()
    if not logs:
        await message.answer("لاگی ثبت نشده است.")
        return
    lines = [
        f"{item.created_at:%Y-%m-%d %H:%M} | {item.actor_id} | {item.action}"
        + (f" | {item.target_id}" if item.target_id else "")
        for item in logs
    ]
    await message.answer("🧾 آخرین عملیات ادمین:\n" + "\n".join(lines))
