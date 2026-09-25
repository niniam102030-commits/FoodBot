from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton,
)

from models import PERSIAN_MONTHS, to_persian_digits


def get_start_menu() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def get_main_menu(is_admin: bool = False, lunch_mark: str = "", dinner_mark: str = "") -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=f"🍽️ ثبت / ویرایش غذای ناهار{lunch_mark}")],
        [KeyboardButton(text=f"🌙 ثبت / ویرایش غذای شام{dinner_mark}")],
        [KeyboardButton(text="📋 منوی هفتگی و قیمت‌ها")],
        [KeyboardButton(text="📋 مشاهده ثبت‌های من")],
        [KeyboardButton(text="👤 ویرایش نام")],
        [KeyboardButton(text="💡 راهنما")],
        [KeyboardButton(text="🗑️ حذف حساب کاربری")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ منوی مدیریت ادمین")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="back_to_menu"),
    ]])


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """دکمه انصراف برای جریان‌های چندمرحله‌ای که منتظر ورودی متنی‌اند."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_flow"),
    ]])


def get_weekly_calendar_keyboard(
    user_reserved_days: list, all_days: list, week_index: int, meal_type: str = "lunch"
) -> InlineKeyboardMarkup:
    weeks = [all_days[i:i + 7] for i in range(0, len(all_days), 7)]
    week_days = weeks[week_index] if 0 <= week_index < len(weeks) else []
    buttons = []

    for day_info in week_days:
        if day_info["is_holiday"]:
            continue
        # شام پنج‌شنبه نداریم
        if meal_type == "dinner" and day_info.get("weekday_num") == 5:
            continue
        selected = "✅ " if day_info["day"] in user_reserved_days else ""
        label = f"{selected}{day_info['day']} / {day_info['weekday_name']}"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"day_{meal_type}_{week_index}_{day_info['day']}",
            )
        )

    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    navigation = []
    if week_index > 0:
        navigation.append(InlineKeyboardButton(text="➡️ هفته قبل", callback_data=f"week_{meal_type}_prev_{week_index}"))
    if week_index < len(weeks) - 1:
        navigation.append(InlineKeyboardButton(text="هفته بعد ⬅️", callback_data=f"week_{meal_type}_next_{week_index}"))
    else:
        confirm_label = "✅ ثبت و مرحله بعد"
        if user_reserved_days:
            confirm_label = f"✅ ثبت و مرحله بعد ({to_persian_digits(len(user_reserved_days))} روز)"
        navigation.append(InlineKeyboardButton(text=confirm_label, callback_data=f"confirm_days_{meal_type}"))
    rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_day_edit_keyboard(foods: list, meal_type: str = "lunch") -> InlineKeyboardMarkup:
    rows = []
    for day_name, food_name, day_num in foods:
        rows.append([
            InlineKeyboardButton(
                text=f"{day_name} / {food_name}",
                callback_data=f"edit_food_{meal_type}_{day_num}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_archive_month_keyboard(months: list) -> InlineKeyboardMarkup:
    rows = []
    for year, month in months:
        month_name = PERSIAN_MONTHS[month - 1] if 1 <= month <= 12 else ""
        year_text = to_persian_digits(year)
        month_text = to_persian_digits(f"{month:02d}")
        label = f"{year_text} / {month_text} ({month_name})" if month_name else f"{year_text} / {month_text}"
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"delete_archive_{year}_{month}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_confirmation_keyboard(
    confirm_data: str,
    cancel_data: str = "cancel_action",
    confirm_text: str = "✅ بله، حذف شود",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=confirm_text, callback_data=confirm_data),
        InlineKeyboardButton(text="❌ انصراف", callback_data=cancel_data),
    ]])


def get_delete_reg_keyboard(meal_type: str = "lunch") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ حذف ثبت قبلی و شروع مجدد", callback_data=f"delete_my_reg_{meal_type}")]
    ])


def get_meal_choice_keyboard(
    prefix: str,
    lunch_label: str = "🍽️ ناهار",
    dinner_label: str = "🌙 شام",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=lunch_label, callback_data=f"{prefix}_lunch"),
        InlineKeyboardButton(text=dinner_label, callback_data=f"{prefix}_dinner"),
    ]])


def get_delete_user_keyboard(target_uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ حذف اطلاعات کاربر", callback_data=f"deluser_{target_uid}")]
    ])


def get_delete_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="❌ حذف کامل حساب من",
            callback_data="delete_my_account",
        )
    ]])


def get_zero_next_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="صفر، مرحله بعدی", callback_data="zero_next")
    ]])


def get_admin_user_days_keyboard(
    user_reserved_days: list, all_days: list, week_index: int, meal_type: str = "lunch"
) -> InlineKeyboardMarkup:
    """تقویم انتخاب روزهای یک کاربر توسط ادمین (به‌همراه دکمه پایان و ذخیره)."""
    weeks = [all_days[i:i + 7] for i in range(0, len(all_days), 7)]
    week_days = weeks[week_index] if 0 <= week_index < len(weeks) else []
    buttons = []

    for day_info in week_days:
        if day_info["is_holiday"]:
            continue
        # شام پنج‌شنبه نداریم
        if meal_type == "dinner" and day_info.get("weekday_num") == 5:
            continue
        selected = "✅ " if day_info["day"] in user_reserved_days else ""
        label = f"{selected}{day_info['day']} / {day_info['weekday_name']}"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"aday_{meal_type}_{week_index}_{day_info['day']}",
            )
        )

    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    navigation = []
    if week_index > 0:
        navigation.append(InlineKeyboardButton(text="➡️ هفته قبل", callback_data=f"aweek_{meal_type}_prev_{week_index}"))
    if week_index < len(weeks) - 1:
        navigation.append(InlineKeyboardButton(text="هفته بعد ⬅️", callback_data=f"aweek_{meal_type}_next_{week_index}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="✅ پایان و ذخیره", callback_data="afinish")])
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_flow")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 شروع دوباره", callback_data="restart_bot"),
    ]])


def get_user_list_keyboard(users: list, meal_type: str = "lunch") -> InlineKeyboardMarkup:
    """users: لیست (user_id, full_name) — هر کاربر یک دکمه شیشه‌ای."""
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {full_name or 'بدون نام'}",
            callback_data=f"user_card_{meal_type}_{user_id}",
        )]
        for user_id, full_name in users
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="user_list_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_user_card_keyboard(user_id: str, meal_type: str = "lunch") -> InlineKeyboardMarkup:
    """گزینه‌های کارت هر کاربر: ویرایش، حذف و بازگشت به فهرست."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ ویرایش اطلاعات کاربر", callback_data=f"admin_edit_user_{user_id}_{meal_type}")],
        [InlineKeyboardButton(text="❌ حذف اطلاعات کاربر", callback_data=f"deluser_{user_id}")],
        [InlineKeyboardButton(text="🔙 بازگشت به فهرست", callback_data=f"user_list_{meal_type}")],
    ])


def get_blocked_days_keyboard(
    selected_days: list, all_days: list, week_index: int = 0
) -> InlineKeyboardMarkup:
    """تقویم انتخاب روزهای بدون غذا توسط ادمین (با دکمه پایان و ذخیره)."""
    weeks = [all_days[i:i + 7] for i in range(0, len(all_days), 7)]
    week_days = weeks[week_index] if 0 <= week_index < len(weeks) else []
    buttons = []

    for day_info in week_days:
        if day_info["is_holiday"]:
            continue
        selected = "✅ " if day_info["day"] in selected_days else ""
        label = f"{selected}{day_info['day']} / {day_info['weekday_name']}"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"bday_{week_index}_{day_info['day']}",
            )
        )

    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    navigation = []
    if week_index > 0:
        navigation.append(InlineKeyboardButton(text="➡️ هفته قبل", callback_data=f"bweek_prev_{week_index}"))
    if week_index < len(weeks) - 1:
        navigation.append(InlineKeyboardButton(text="هفته بعد ⬅️", callback_data=f"bweek_next_{week_index}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="✅ پایان و ذخیره", callback_data="bfinish")])
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_flow")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_management_keyboard() -> InlineKeyboardMarkup:
    """دکمه‌های مدیریت ادمین‌ها (فقط برای ادمین اصلی نمایش داده می‌شود)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ اضافه کردن ادمین", callback_data="admin_mgmt_add")],
        [InlineKeyboardButton(text="➖ حذف ادمین", callback_data="admin_mgmt_remove")],
        [InlineKeyboardButton(text="❌ بستن", callback_data="admin_mgmt_cancel")],
    ])


def get_extra_admins_keyboard(extra_admins: list) -> InlineKeyboardMarkup:
    """فهرست ادمین‌های اضافه برای انتخاب جهت حذف؛ هر آیتم (id, name) است.
    name می‌تواند None باشد؛ در این صورت فقط آیدی نمایش داده می‌شود."""
    rows = []
    for admin_id, name in extra_admins:
        label = f"❌ {name} ({admin_id})" if name else f"❌ {admin_id}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"admin_mgmt_del_{admin_id}")])
    rows.append([InlineKeyboardButton(text="❌ بستن", callback_data="admin_mgmt_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_food_list_keyboard(foods: list, meal_type: str = "lunch") -> InlineKeyboardMarkup:
    """foods: list of (id, day_name, food_name, normal_price, free_price)"""
    rows = []
    for fid, day_name, food_name, norm, free in foods:
        rows.append([InlineKeyboardButton(
            text=f"{day_name} - {food_name}",
            callback_data=f"setprice_{meal_type}_{fid}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)
