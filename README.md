# شکارچی آگهی خودرو - دیوار مشهد

ربات آگهی‌های جدید خودرو مشهد رو از MCP دیوار می‌گیره، با قیمت بازار مقایسه می‌کنه، متن و عکس رو با هوش مصنوعی تحلیل می‌کنه و فقط اگه شکار بود، یک پیام برات توی تلگرام می‌فرسته.

**هیچ ارتباطی با فروشنده گرفته نمیشه.** MCP دیوار فقط خواندنیه و شماره تلفن برنمی‌گردونه؛ تنها کاری که کد بیرون انجام میده، فرستادن پیام به chat id خودته.

## راه‌اندازی

```bash
cd divar-hunter
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env        # کلیدهای جدیدت رو بذار
```

> سرور بهتره **خارج از ایران** باشه: OpenRouter و تلگرام از ایران معمولاً بسته‌ست. سرور MCP هم روی Cloudflare هست و از بیرون مشکلی نداره.
> اگه از داخل ایران با پروکسی/VPN اجرا می‌کنی، متغیر `HTTPS_PROXY` رو ست کن (httpx خودش می‌خونه).

## ترتیب تست

```bash
python bot.py --test-telegram           # ۱) پیام تست به تلگرام
python bot.py --once --dry-run --debug  # ۲) یک دور، با نمایش خروجی خام، بدون ارسال
python bot.py --once                    # ۳) یک دور واقعی
python bot.py                           # ۴) اجرای دائمی (هر ۱۰ دقیقه)
```

اگه تو مرحله ۲ خطایی دیدی یا کارت‌ها پیدا نشدن، خروجی `RAW search_ads` رو برام بفرست تا کد رو با ساختار واقعی تنظیم کنم.

## اجرا به‌صورت سرویس (systemd)

`/etc/systemd/system/divar-hunter.service`:

```ini
[Unit]
Description=Divar Hunter
After=network-online.target

[Service]
WorkingDirectory=/home/USER/divar-hunter
ExecStart=/home/USER/divar-hunter/venv/bin/python bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now divar-hunter
journalctl -u divar-hunter -f
```

## تنظیم دقت

- `MIN_DISCOUNT_PERCENT`: فیلتر اول (قبل از خرج AI). پایین‌ترش کنی آگهی بیشتر بررسی میشه.
- `MIN_PROFIT_TOMAN` و `MIN_CONFIDENCE`: هر چی بالاتر، پیام کمتر و تمیزتر.
- `repair_costs.md`: جدول هزینه تعمیرات؛ اعداد نمونه‌ست، با قیمت واقعی خودت عوض کن.
- `seen.db`: آگهی‌های دیده‌شده. حذفش کنی همه‌چی از اول بررسی میشه.

## محدودیت‌ها

- سرور MCP رایگانه و محدودیت ۶۰ درخواست در دقیقه برای هر IP داره؛ کد با فاصله بین درخواست‌ها کار می‌کنه و سقف تحلیل هر دور (`MAX_ANALYZE_PER_CYCLE`) داره.
- «میانه بازار» میانه آگهی‌های همون لحظه‌ست، نه ارزیابی کارشناسی. سال ساخت، کارکرد و وضعیت بدنه بین آگهی‌ها فرق داره. خروجی ربات یه فیلتر اولیه‌ست، نه جایگزین دیدن ماشین.
