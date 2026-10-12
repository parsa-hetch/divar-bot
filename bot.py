#!/usr/bin/env python3
"""
Divar Hunter - شکارچی آگهی خودرو در دیوار مشهد (دریافت خودکار مدل‌های رایگان فعال)

جریان کار:
  1) دریافت آگهی‌های جدید خودرو مشهد از MCP دیوار
  2) فیلتر سریع بر اساس میانه قیمت بازار و لیست سیاه (تصادفی/تاکسی/سال ساخت)
  3) استعلام آنلاین مدل‌های رایگان و فعال OpenRouter
  4) تحلیل سریع متنی آگهی با مدل‌های رایگان و پرامپت سخت‌گیرانه کارشناسی
  5) ارسال کاندیداهای برتر به تلگرام
"""

import argparse
import asyncio
import html
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

# ----------------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "auto_free")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

raw_chat_ids = os.getenv("TELEGRAM_CHAT_ID", "")
CHAT_IDS = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]


MCP_URL = os.getenv("DIVAR_MCP_URL", "https://divar-mcp.mmdju2.workers.dev/mcp")
CITY = os.getenv("CITY", "mashhad")
CATEGORY = os.getenv("CATEGORY", "light")
SELLER_TYPE = os.getenv("SELLER_TYPE", "personal")

MIN_PRICE_TOMAN = env_int("MIN_PRICE_TOMAN", 100_000_000)
MAX_PRICE_TOMAN = env_int("MAX_PRICE_TOMAN", 1_500_000_000)

MIN_DISCOUNT_PERCENT = env_float("MIN_DISCOUNT_PERCENT", 7)
MIN_PROFIT_TOMAN = env_int("MIN_PROFIT_TOMAN", 20_000_000)
MIN_PROFIT_SAMAND_TOMAN = env_int("MIN_PROFIT_SAMAND_TOMAN", 35_000_000) # حد سود سخت‌گیرانه‌تر برای سمند
MIN_CONFIDENCE = env_int("MIN_CONFIDENCE", 40)
OVERHEAD_TOMAN = env_int("OVERHEAD_TOMAN", 3_000_000)

INTERVAL_SECONDS = env_int("INTERVAL_SECONDS", 600)
MAX_ANALYZE_PER_CYCLE = env_int("MAX_ANALYZE_PER_CYCLE", 15)
PACE_SECONDS = env_float("PACE_SECONDS", 1.5)

DB_PATH = BASE_DIR / "seen.db"
CACHED_FREE_MODELS = []

# لیست سیاه کلمات جهت حذف سریع در فیلتر اولیه
BLACKLIST_KEYWORDS = [
    "تصادفی", "چپی", "اوراقی", "شاسی خورده", "شاسی جلو خورده", "شاسی عقب خورده",
    "تاکسی", "پیکان وانت", "وانت پیکان", "آریسان", "اریسان", "تعویض اتاق", "موتور تعویض", "دور رنگ", "کامل رنگ"
]

# خودروهای خوش‌خرید بازار
PREFERRED_CARS = ["پراید", "206", "۲۰۶", "207", "۲۰۷", "کوئیک", "کوییک", "ساینا", "تیبا", "پارس"]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ----------------------------------------------------------------------------
# دیتابیس آگهی‌های دیده‌شده
# ----------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS seen (
               token TEXT PRIMARY KEY,
               status TEXT,
               price INTEGER,
               profit INTEGER,
               ts INTEGER
           )"""
    )
    db.commit()
    return db


def is_seen(db, token: str) -> bool:
    return db.execute("SELECT 1 FROM seen WHERE token=?", (token,)).fetchone() is not None


def mark_seen(db, token: str, status: str, price=None, profit=None):
    db.execute(
        "INSERT OR REPLACE INTO seen(token,status,price,profit,ts) VALUES (?,?,?,?,?)",
        (token, status, price, profit, int(time.time())),
    )
    db.commit()


# ----------------------------------------------------------------------------
# توابع کمکی و فیلترهای اولیه
# ----------------------------------------------------------------------------
def extract_year(text: str) -> int:
    """استخراج سال ساخت شمسی از عنوان آگهی"""
    for word in text.split():
        if word.isdigit() and 1370 <= int(word) <= 1403:
            return int(word)
    return 0


def is_valid_car_prefilter(title: str, description: str) -> bool:
    """فیلتر سریع قبل از ارسال به هوش مصنوعی (حذف تصادفی، تاکسی، وانت، زیر ۹۰)"""
    full_text = f"{title} {description}".lower()
    
    # ۱. کلمات کلیدی ممنوعه
    if any(keyword in full_text for keyword in BLACKLIST_KEYWORDS):
        return False
        
    # ۲. بررسی سال ساخت (زیر ۱۳۹۰ رد می‌شود مگر استثنایی و کلکسیونی باشد)
    year = extract_year(title)
    if 0 < year < 1390:
        pristine_keywords = ["استثنایی", "کلکسیونی", "کم کار واقعی", "عتیقه", "بدون رنگ واقعی"]
        if not any(k in full_text for k in pristine_keywords):
            return False
            
    return True


async def call_tool(session: ClientSession, name: str, args: dict):
    res = await session.call_tool(name, args)
    text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    if res.isError:
        raise RuntimeError(f"MCP tool {name} failed: {text[:300]}")
    structured = getattr(res, "structuredContent", None)
    if structured:
        return structured
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def deep_get(obj, key):
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        for v in obj.values():
            r = deep_get(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_get(v, key)
            if r is not None:
                return r
    return None


def find_cards(data):
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data) and any(("token" in x or "url" in x) for x in data):
            return data
        for x in data:
            r = find_cards(x)
            if r:
                return r
    elif isinstance(data, dict):
        for v in data.values():
            r = find_cards(v)
            if r:
                return r
    return []


def token_of(card: dict):
    if card.get("token"):
        return str(card["token"])
    m = re.search(r"/v/(?:[^/]+/)?([A-Za-z0-9_-]{6,})/?(?:\?.*)?$", str(card.get("url", "")))
    return m.group(1) if m else None


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def million(n) -> str:
    return f"{n / 1_000_000:,.0f} میلیون"


# ----------------------------------------------------------------------------
# دریافت هوشمند لیست مدل‌های رایگان فعال از OpenRouter
# ----------------------------------------------------------------------------
async def get_active_free_models(http: httpx.AsyncClient) -> list:
    global CACHED_FREE_MODELS
    if CACHED_FREE_MODELS:
        return CACHED_FREE_MODELS

    env_val = OPENROUTER_MODEL.strip()
    # اگر در تنظیمات مدل خاصی تعریف شده باشد
    if env_val.lower() not in ("auto_free", "auto", "free", ""):
        return [m.strip() for m in env_val.split(",") if m.strip()]

    # لیست VIP گلچین‌شده از بهترین و پایدارترین مدل‌های رایگان با سقف مجاز بالا
    best_free_models = [
        "google/gemini-2.0-flash-lite-001:free",
        "google/gemini-2.0-pro-exp-02-05:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-coder-32b-instruct:free",
        "deepseek/deepseek-r1:free"
    ]
    
    log("استفاده از لیست گلچین‌شده مدل‌های پایدار و رایگان OpenRouter")
    CACHED_FREE_MODELS = best_free_models
    return best_free_models



# ----------------------------------------------------------------------------
# تحلیل هوش مصنوعی (مستر پرامپت کارشناسی)
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a top-tier expert used-car inspector and dealer in Mashhad, Iran.
Your ONLY job is to skim the car ad text and identify genuine bargain leads ("shekar") for resale profit (at least 10-15% below realistic market value).

Strict Screening Rules:
1. Hard Dealbreakers (MUST set is_candidate = false):
   - Accidents, chassis damage (شاسی خورده/ضربه‌خورده), rollover/salvage (چپی/اوراقی), body frame replacement (تعویض اتاق), engine swap (موتور تعویض), heavy body damage/severe sun fade (آفتاب‌سوختگی شدید).
   - Taxis, Paykan Pickups, Arisan, low-demand old Chinese cars.
   - Legal/document issues (سند مشکل‌دار/وکالتی بدون پلاک).
2. Car Condition & Liquidity:
   - Priority given to high-liquidity models: Pride, 206, 207, Quick, Saina, Tiba, Pars.
   - High value for "No Paint" (بی‌رنگ) and low mileage (<150,000 km).
   - Samand or low-demand cars require a significantly larger profit margin to be considered a candidate.
3. Pricing & Resale:
   - Provide a realistic resale value in Toman based on actual market value (do NOT blindly accept inflated median prices if the ad condition is poor).
   - Estimate repair costs realistically (0 if clean/minor).

Respond ONLY with a valid JSON object matching this exact format:
{
  "is_candidate": boolean,
  "confidence": integer 0-100,
  "estimated_repair_cost_toman": integer,
  "estimated_resale_toman": integer,
  "red_flags": [string, ...],
  "positives": [string, ...],
  "summary_fa": string
}"""


def parse_llm_json(text: str) -> dict:
    if not text or not isinstance(text, str):
        raise ValueError("پاسخ مدل خالی یا نامعتبر بود")
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


async def analyze_with_llm(http: httpx.AsyncClient, ad: dict, market: dict) -> dict:
    title = ad.get("title") or deep_get(ad, "title") or ""
    description = deep_get(ad, "description") or deep_get(ad, "text") or ""

    minimal_ad_text = (
        f"عنوان آگهی: {title}\n"
        f"توضیحات آگهی: {description[:1200]}\n"
        f"قیمت فروشنده: {market.get('price')} تومان\n"
        f"میانه بازار: {market.get('median')} تومان (تخفیف: {market.get('discount'):.1f}٪)"
    )

    models_list = await get_active_free_models(http)

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/mmdju/divar-mcp",
        "X-Title": "Divar Car Hunter",
    }

    last_err = ""
    for model_name in models_list[:8]:
        payload = {
            "model": model_name,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": minimal_ad_text},
            ],
        }

        try:
            r = await http.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )

            if r.status_code == 200:
                res_json = r.json()
                choices = res_json.get("choices", [])
                if choices and isinstance(choices, list):
                    msg = choices[0].get("message", {}).get("content")
                    if msg:
                        log(f"پاسخ هوش مصنوعی دریافت شد (مدل: {model_name})")
                        return parse_llm_json(msg)

            last_err = f"{model_name} (Code {r.status_code})"
            log(f"مدل {model_name} پاسخ معتبر نداد ({r.status_code}) -> مدل بعدی...")

        except Exception as e:
            log(f"خطا در فراخوانی {model_name}: {e}")

    raise RuntimeError(f"هیچ مدل رایگانی پاسخ نداد. آخرین خطا: {last_err}")


# ----------------------------------------------------------------------------
# تلگرام
# ----------------------------------------------------------------------------
async def telegram_send(http: httpx.AsyncClient, text: str):
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    for chat_id in CHAT_IDS:
        try:
            r = await http.post(
                f"{base}/sendMessage",
                data={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"},
            )
            if r.status_code != 200:
                log(f"خطا در ارسال پیام به Chat ID {chat_id}: {r.status_code}")
        except Exception as e:
            log(f"خطا در ارسال پیام به {chat_id}: {e}")


def build_message(title, url, price, market, analysis, profit) -> str:
    e = html.escape
    lines = [
        f"🎯 <b>کاندیدای جدید: {e(title or 'آگهی خودرو')}</b>",
        "",
        f"💰 قیمت آگهی: <b>{million(price)}</b> تومان",
        f"📊 میانه بازار: {million(market['median'])} ({market['discount']:.0f}٪ ارزان‌تر)",
        f"🔧 تخمین اولیه تعمیرات: {million(to_int(analysis.get('estimated_repair_cost_toman')))}",
        f"🏷 تخمین فروش: {million(to_int(analysis.get('estimated_resale_toman')))}",
        f"✅ سود ناخالص حدودی: <b>{million(profit)}</b> تومان",
    ]
    pos = analysis.get("positives") or []
    neg = analysis.get("red_flags") or []
    if pos:
        lines += ["", "👍 " + e("، ".join(map(str, pos[:4])))]
    if neg:
        lines += ["⚠️ " + e("، ".join(map(str, neg[:4])))]
    if analysis.get("summary_fa"):
        lines += ["", "💡 " + e(str(analysis["summary_fa"]))]
    lines += ["", url or ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# اجرای یک دور
# ----------------------------------------------------------------------------
async def cycle(session: ClientSession, http: httpx.AsyncClient, db, args):
    params = {
        "category": CATEGORY,
        "city": CITY,
        "sort": "newest",
        "pages": 2,
        "limit": 30,
        "min_price_toman": MIN_PRICE_TOMAN,
        "max_price_toman": MAX_PRICE_TOMAN,
    }
    if SELLER_TYPE:
        params["seller_type"] = SELLER_TYPE

    data = await call_tool(session, "search_ads", params)
    cards = find_cards(data)
    new_cards = []
    for c in cards:
        t = token_of(c)
        if t and not is_seen(db, t):
            new_cards.append((t, c))
    log(f"{len(cards)} آگهی دریافت شد، {len(new_cards)} تا جدیده")

    analyzed = 0
    sent = 0
    for token, card in new_cards:
        if analyzed >= MAX_ANALYZE_PER_CYCLE:
            break
        try:
            price = card.get("price_toman")
            if not price or card.get("price_is_placeholder"):
                mark_seen(db, token, "skip_no_price")
                continue
            price = to_int(price)

            # --- پیش‌فیلتر اولیه روی عنوان قبل از فراخوانی MCP
            card_title = card.get("title") or ""
            if any(k in card_title for k in BLACKLIST_KEYWORDS):
                mark_seen(db, token, "skip_blacklist_title", price)
                continue

            # --- محاسبه قیمت بازار
            analyzed += 1
            mp = await call_tool(session, "market_price", {"token": token, "max_sample": 40})
            median = to_int(deep_get(mp, "median_toman"))
            if not median:
                mark_seen(db, token, "skip_no_median", price)
                continue
            discount = (median - price) / median * 100
            if discount < MIN_DISCOUNT_PERCENT:
                mark_seen(db, token, "skip_not_cheap", price)
                log(f"- {token}: فقط {discount:.0f}٪ ارزون‌تر، رد شد")
                continue

            market = {
                "median": median,
                "price": price,
                "discount": discount,
            }

            # --- جزئیات آگهی
            await asyncio.sleep(PACE_SECONDS)
            details = await call_tool(session, "ad_details", {"token": token, "detail": "full"})
            title = card.get("title") or deep_get(details, "title") or ""
            description = deep_get(details, "description") or deep_get(details, "text") or ""
            url = card.get("url") or deep_get(details, "url") or f"https://divar.ir/v/{token}"

            # --- بررسی فیلترهای کامل (تصادفی، سال ساخت، لیست سیاه)
            if not is_valid_car_prefilter(title, description):
                mark_seen(db, token, "skip_prefilter", price)
                log(f"- {token}: رد شد در فیلتر اولیه (تصادفی/سال ساخت/لیست سیاه)")
                continue

            # --- تحلیل هوش مصنوعی
            analysis = await analyze_with_llm(http, details, market)
            repair = to_int(analysis.get("estimated_repair_cost_toman"))
            resale = min(to_int(analysis.get("estimated_resale_toman")), int(median * 1.05))
            profit = resale - price - repair - OVERHEAD_TOMAN
            confidence = to_int(analysis.get("confidence"))

            # شرط سخت‌گیرانه‌تر برای سمند
            required_profit = MIN_PROFIT_SAMAND_TOMAN if "سمند" in title else MIN_PROFIT_TOMAN

            ok = (
                analysis.get("is_candidate") is True
                and profit >= required_profit
                and confidence >= MIN_CONFIDENCE
            )
            log(f"- {token}: {discount:.0f}٪ زیر میانه | سود حدود {million(profit)} | {'تایید کاندیدا' if ok else 'رد'}")

            if ok:
                msg = build_message(title, url, price, market, analysis, profit)
                if args.dry_run:
                    print("\n----- DRY RUN MESSAGE -----\n" + msg + "\n---------------------------\n")
                else:
                    await telegram_send(http, msg)
                sent += 1
                mark_seen(db, token, "sent", price, profit)
            else:
                mark_seen(db, token, "rejected", price, profit)

            await asyncio.sleep(PACE_SECONDS)

        except Exception as e:
            log(f"خطا برای {token}: {e}")

    log(f"پایان دور؛ {sent} کاندیدا فرستاده شد")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def check_config(args):
    missing = []
    if not TELEGRAM_BOT_TOKEN and not args.dry_run:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_IDS and not args.dry_run:
        missing.append("TELEGRAM_CHAT_ID")
    if not OPENROUTER_API_KEY and not args.test_telegram:
        missing.append("OPENROUTER_API_KEY")
    if missing:
        sys.exit("این مقادیر توی فایل .env خالی هستن: " + ", ".join(missing))


async def run(args):
    check_config(args)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
        if args.test_telegram:
            await telegram_send(http, "✅ ربات شکارچی دیوار وصله و پیام می‌ده.")
            log("پیام تست فرستاده شد")
            return

        db = init_db()
        while True:
            try:
                async with streamablehttp_client(MCP_URL) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await cycle(session, http, db, args)
            except Exception as e:
                log(f"خطای دور: {e}")
            if args.once:
                break
            await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="فقط یک دور اجرا کن")
    ap.add_argument("--dry-run", action="store_true", help="به تلگرام نفرست، فقط چاپ کن")
    ap.add_argument("--debug", action="store_true", help="خروجی خام MCP رو چاپ کن")
    ap.add_argument("--test-telegram", action="store_true", help="یک پیام تست بفرست و خارج شو")
    asyncio.run(run(ap.parse_args()))
