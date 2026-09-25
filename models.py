from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

# مقادیر پیش‌فرض اولیه ربات؛ ادمین هر زمان از داخل ربات می‌تواند تغییرشان دهد
WEEKDAYS_MAP = {
    0: {"name": "شنبه",      "food": "خورشت قیمه",         "norm": 20000, "free": 80000},
    1: {"name": "یکشنبه",    "food": "استانبولی",          "norm": 20000, "free": 80000},
    2: {"name": "دوشنبه",    "food": "مرغ",                "norm": 20000, "free": 80000},
    3: {"name": "سه‌شنبه",   "food": "عدس پلو/باقالی پلو", "norm": 20000, "free": 80000},
    4: {"name": "چهارشنبه",  "food": "قورمه",              "norm": 20000, "free": 80000},
    5: {"name": "پنج‌شنبه",   "food": "خوراک مرغ",          "norm": 20000, "free": 80000},
    6: {"name": "جمعه",      "food": "جمعه",               "norm": 0,     "free": 0},
}

PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_digits(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


class Setting(Base):
    __tablename__ = "settings"
    key   = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)


class Food(Base):
    __tablename__ = "foods"
    id           = Column(Integer, primary_key=True)
    day_num      = Column(Integer, nullable=False)
    day_name     = Column(String(50), nullable=False)
    food_name    = Column(String(100), nullable=False)
    normal_price = Column(Integer, default=0)
    free_price   = Column(Integer, default=0)
    meal_type    = Column(String(20), nullable=False, default="lunch")


class User(Base):
    __tablename__ = "users"
    user_id   = Column(String, primary_key=True)
    full_name = Column(String(255), nullable=False, default="")
    step      = Column(String(50), default="get_name")
    status    = Column(String(50), default="pending")
    lunch_status = Column(String(50), default="pending")
    dinner_status = Column(String(50), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)


class Reservation(Base):
    __tablename__ = "reservations"
    user_id = Column(String, primary_key=True)
    day     = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DinnerReservation(Base):
    __tablename__ = "dinner_reservations"
    user_id = Column(String, primary_key=True)
    day     = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FreeOrder(Base):
    __tablename__ = "free_orders"
    user_id = Column(String, primary_key=True)
    food_id = Column(Integer, primary_key=True)
    count   = Column(Integer, default=0)


class UserContainer(Base):
    __tablename__ = "user_containers"
    user_id = Column(String, primary_key=True)
    count   = Column(Integer, default=0)


class Archive(Base):
    __tablename__ = "archive"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    date_str = Column(String(50))
    payload  = Column(Text)
    year     = Column(Integer, nullable=True)
    month    = Column(Integer, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    actor_id   = Column(String, nullable=False)
    action     = Column(String(100), nullable=False)
    target_id  = Column(String, nullable=True)
    details    = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class FSMState(Base):
    __tablename__ = "fsm_states"
    key        = Column(String(255), primary_key=True)
    state      = Column(String(255), nullable=True)
    data       = Column(Text, nullable=True)
