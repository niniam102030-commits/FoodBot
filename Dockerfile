# اجرای پایدار ربات غذا روی سرور (polling ۲۴ ساعته)
FROM python:3.12-slim

# جلوگیری از بافر شدن لاگ‌ها در docker logs + منطقه زمانی شمسی برای jdatetime و زمان‌بند
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tehran

WORKDIR /app

# وابستگی‌ها در لایه جدا تا تغییر کد، نصب پکیج‌ها را دوباره اجرا نکند
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# فقط فایل‌های کد منبع؛ .env و دیتابیس وارد ایمیج نمی‌شوند (از طریق compose تزریق می‌شوند)
COPY *.py ./

RUN mkdir -p data logs backups

# ═══ «آزمون ورود» در زمان build ═══
# اگر نسخه کتابخانه‌ها با کد ناسازگار باشد (مثل ModuleNotFoundError که
# قبلاً تجربه شد)، build همینجا شکست می‌خورد و ایمیج معیوب ساخته نمی‌شود.
RUN pip check \
    && BOT_TOKEN=test:dummy DATABASE_URL=sqlite:///./data/foodbot.db python -c "import main" \
    && echo 'Self-test passed: all imports OK'

# سلامت‌سنجی: هر ۳۰ ثانیه چک می‌کند پروسه ربات زنده باشد؛
# اگر هنگ کرده باشد به compose اجازه restart می‌دهد (با autoheal یا restart دستی)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python main.py" > /dev/null || exit 1

CMD ["python", "main.py"]
