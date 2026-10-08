import asyncio
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from bs4 import BeautifulSoup
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY_URL = os.getenv("PROXY_URL", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL در Environment Variables تنظیم نشده است. "
        "در Railway یک PostgreSQL اضافه کن."
    )

URL_CURRENCY = "https://alanchand.com/en/currencies-price"
URL_CRYPTO = "https://alanchand.com/en/crypto-price"
URL_GOLD = "https://alanchand.com/en/gold-price"
URL_TGJU_DOLLAR = "https://www.tgju.org/profile/price_dollar_rl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

TEHRAN = ZoneInfo("Asia/Tehran")
dp = Dispatcher()
app = FastAPI()

# =========================================================
# DATABASE (PRICE HISTORY LOG)
# =========================================================

db_pool: asyncpg.Pool | None = None


async def init_db():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=20,
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_price_history_symbol_time
            ON price_history (symbol, created_at DESC)
            """
        )

        # اولین قیمت ثبت‌شده هر نماد در هر روز؛
        # اگر سابقه دیروز موجود نباشد، برای مقایسه همان روز استفاده می‌شود.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_open_prices (
                price_date DATE NOT NULL,
                symbol TEXT NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (price_date, symbol)
            )
            """
        )

    logging.info("PostgreSQL database (price_history + daily_open_prices) is ready.")


async def update_and_get_prices() -> dict:
    """
    سایت‌ها را اسکرپ می‌کند، آخرین قیمت ثبت‌شده در دیتابیس را می‌خواند؛
    اگر قیمت تغییر کرده باشد، آن را ثبت می‌کند و در غیر این صورت رکورد جدید نمی‌سازد.
    """
    timeout = aiohttp.ClientTimeout(total=25)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout, connector=connector) as session:
        results = await asyncio.gather(
            get_rows(session, URL_CURRENCY),
            get_rows(session, URL_CRYPTO),
            get_rows(session, URL_GOLD),
            get_tgju_dollar(session),
            return_exceptions=True,
        )

    raw_data = {}
    parsers = (("currency", parse_currency), ("crypto", parse_crypto), ("gold", parse_gold))

    for (key, parser), result in zip(parsers, results[:3]):
        try:
            if isinstance(result, Exception):
                raise result
            raw_data[key] = parser(result)
        except Exception:
            raw_data[key] = None

    try:
        if isinstance(results[3], Exception):
            raise results[3]
        raw_data["tgju_dollar"] = results[3]
    except Exception:
        raw_data["tgju_dollar"] = None

    current_prices = {
        "dollar": raw_data.get("tgju_dollar"),
        "euro": (
            raw_data.get("currency", {}).get("euro", {}).get("sell")
            if raw_data.get("currency")
            else None
        ),
        "usdt": raw_data.get("crypto", {}).get("usdt"),
        "btc": raw_data.get("crypto", {}).get("btc"),
        "gold18": raw_data.get("gold", {}).get("gold18"),
        "coin": raw_data.get("gold", {}).get("coin"),
        "ounce": raw_data.get("gold", {}).get("ounce"),
    }

    result_data = {
        "raw": raw_data,
        "comparison": {}
    }

    if db_pool is None:
        return result_data

    async with db_pool.acquire() as conn:
        # ---------------------------------------------------------
        # مرجع مقایسه:
        # 1) اگر از روزهای قبل سابقه داریم، آخرین روز قبل از امروز
        # 2) اگر نداریم، اولین قیمت ثبت‌شده امروز
        # ---------------------------------------------------------
        today = datetime.now(TEHRAN).date()

        yesterday_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (symbol) symbol, value
            FROM price_history
            WHERE (created_at AT TIME ZONE 'Asia/Tehran')::date < $1
            ORDER BY symbol, created_at DESC
            """,
            today,
        )

        comparison_prices = {
            row["symbol"]: float(row["value"])
            for row in yesterday_rows
        }

        comparison_label = "دیروز"

        if not comparison_prices:
            open_rows = await conn.fetch(
                """
                SELECT symbol, value
                FROM daily_open_prices
                WHERE price_date = $1
                """,
                today,
            )

            comparison_prices = {
                row["symbol"]: float(row["value"])
                for row in open_rows
            }
            comparison_label = "اولین قیمت امروز"

        result_data["comparison_label"] = comparison_label

        for symbol, val in current_prices.items():
            if val is None:
                continue

            try:
                numeric_val = float(val)
            except (TypeError, ValueError):
                continue

            # اولین قیمت امروز را فقط یک بار ذخیره کن.
            await conn.execute(
                """
                INSERT INTO daily_open_prices (price_date, symbol, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (price_date, symbol) DO NOTHING
                """,
                today,
                symbol,
                numeric_val,
            )

            old_val = comparison_prices.get(symbol)

            # اگر قیمت تغییر کرده باشد، در تاریخچه ثبتش کن.
            rows = await conn.fetch(
                """
                SELECT value
                FROM price_history
                WHERE symbol = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                symbol,
            )

            last_db_val = float(rows[0]["value"]) if rows else None

            if last_db_val is None or numeric_val != last_db_val:
                await conn.execute(
                    """
                    INSERT INTO price_history (symbol, value, created_at)
                    VALUES ($1, $2, NOW())
                    """,
                    symbol,
                    numeric_val,
                )
                logging.info(
                    f"تغییر قیمت برای {symbol}: "
                    f"از {last_db_val} به {numeric_val} (ثبت شد)"
                )

            # برای همان لحظه، مقایسه را با مرجع روزانه انجام بده.
            result_data["comparison"][symbol] = {
                "current": numeric_val,
                "previous": old_val if old_val is not None else numeric_val,
            }

            # اگر این اولین قیمت امروز است، همان را به عنوان مرجع
            # برای دفعات بعدی همین روز نگه می‌داریم.
            if old_val is None:
                result_data["comparison"][symbol]["previous"] = numeric_val

    return result_data


# =========================================================
# FASTAPI ENDPOINTS (WEB APP)
# =========================================================

@app.get("/api/prices")
async def get_prices_api():
    if db_pool is None:
        raise HTTPException(status_code=500, detail="Database pool is not initialized")
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (symbol) symbol, value, created_at as updated_at
            FROM price_history
            ORDER BY symbol, created_at DESC
        """)
        return {row["symbol"]: {"value": row["value"], "updated_at": row["updated_at"]} for row in rows}


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>مینی‌اپ قیمت‌ها فعال است 🚀</h1>"


# =========================================================
# HELPERS & SCRAPING
# =========================================================

def to_int(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def irr_to_toman(cell: str) -> int | None:
    match = re.search(r"([\d,]+)\s*IRR", cell, re.IGNORECASE)
    if not match:
        return None
    number = to_int(match.group(1))
    return number // 10 if number else None


def usd_value_number(cell: str) -> float | None:
    match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", cell)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def fmt_toman(number) -> str:
    if number is None:
        return "—"
    try:
        return f"{float(number):,.0f}"
    except (TypeError, ValueError):
        return "—"


def fmt_usd(number) -> str:
    if number is None:
        return "—"
    try:
        return f"{float(number):,.2f}"
    except (TypeError, ValueError):
        return "—"


def change_text(current, previous, percent=True) -> str:
    if current is None or previous is None or current == previous:
        return "⚪ —"
    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return "⚪ —"

    diff = current - previous
    if diff > 0:
        icon, sign = "🟢", "+"
    elif diff < 0:
        icon, sign = "🔴", ""
    else:
        return "⚪ —"

    result = f"{icon} {sign}{diff:,.0f}"
    if percent and previous != 0:
        pct = (diff / previous) * 100
        result += f" ({sign}{pct:.2f}٪)"
    return result


def change_usd_text(current, previous) -> str:
    if current is None or previous is None or current == previous:
        return "⚪ —"
    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return "⚪ —"

    diff = current - previous
    if diff > 0:
        icon, sign = "🟢", "+"
    elif diff < 0:
        icon, sign = "🔴", ""
    else:
        return "⚪ —"

    result = f"{icon} {sign}{diff:,.2f}"
    if previous != 0:
        pct = (diff / previous) * 100
        result += f" ({sign}{pct:.2f}٪)"
    return result


async def get_rows(session: aiohttp.ClientSession, url: str) -> list[list[str]]:
    async with session.get(url) as response:
        response.raise_for_status()
        html = await response.text()
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    return rows


def parse_currency(rows: list[list[str]]) -> dict:
    output = {}
    for row in rows:
        if len(row) < 3:
            continue
        name = row[0].strip().lower()
        if "euro" in name:
            buy, sell = to_int(row[1]), to_int(row[2])
            if buy is not None and sell is not None:
                output["euro"] = {"buy": buy // 10, "sell": sell // 10}
    return output


async def get_tgju_dollar(session: aiohttp.ClientSession) -> int:
    async with session.get(URL_TGJU_DOLLAR) as response:
        response.raise_for_status()
        html = await response.text()
    soup = BeautifulSoup(html, "html.parser")
    match = re.search(r"نرخ\s*فعلی\s*[:：]+\s*([\d,٬٫]+)", soup.get_text(" ", strip=True), re.IGNORECASE)
    if not match:
        raise ValueError("نرخ دلار پیدا نشد")
    rial_price = to_int(match.group(1))
    return rial_price // 10


def parse_crypto(rows: list[list[str]]) -> dict:
    output = {}
    for row in rows:
        if len(row) < 3:
            continue
        name = row[0].strip().upper()
        if name.endswith("USDT"):
            output["usdt"] = irr_to_toman(row[1])
        elif name.endswith("BTC"):
            output["btc"] = usd_value_number(row[2])
    return output


def parse_gold(rows: list[list[str]]) -> dict:
    output = {}
    for row in rows:
        if len(row) < 2:
            continue
        name = row[0].strip().lower()
        if name.startswith("18k gold"):
            output["gold18"] = irr_to_toman(row[1])
        elif name.startswith("full coin"):
            output["coin"] = irr_to_toman(row[1])
        elif name.startswith("gold ounce"):
            output["ounce"] = usd_value_number(row[1])
    return output


async def build_text(data: dict) -> str:
    now = datetime.now(TEHRAN).strftime("%Y/%m/%d | %H:%M:%S")
    comp = data.get("comparison", {})

    def get_diff(symbol):
        item = comp.get(symbol)
        if not item:
            return None, None
        return item["current"], item["previous"]

    dollar_curr, dollar_prev = get_diff("dollar")
    euro_curr, euro_prev = get_diff("euro")
    usdt_curr, usdt_prev = get_diff("usdt")
    btc_curr, btc_prev = get_diff("btc")
    gold18_curr, gold18_prev = get_diff("gold18")
    coin_curr, coin_prev = get_diff("coin")
    ounce_curr, ounce_prev = get_diff("ounce")

    comparison_label = data.get("comparison_label", "بدون سابقه")

    lines = [
        "📊 <b>قیمت لحظه‌ای بازار</b>",
        f"<i>مقایسه با {comparison_label}</i>",
        "",
    ]

    if dollar_curr is not None:
        lines.extend([
            "💵 <b>دلار آزاد</b>",
            f"   <b>{fmt_toman(dollar_curr)}</b> تومان  {change_text(dollar_curr, dollar_prev)}",
            "",
        ])

    if euro_curr is not None:
        lines.extend([
            "💶 <b>یورو (فروش)</b>",
            f"   <b>{fmt_toman(euro_curr)}</b> تومان  {change_text(euro_curr, euro_prev)}",
            "",
        ])

    if usdt_curr is not None:
        lines.append(f"💲 <b>تتر:</b> {fmt_toman(usdt_curr)} تومان  {change_text(usdt_curr, usdt_prev)}")

    if btc_curr is not None:
        lines.append(f"₿ <b>بیت‌کوین:</b> {fmt_usd(btc_curr)} دلار  {change_usd_text(btc_curr, btc_prev)}")

    lines.append("")

    if gold18_curr is not None:
        lines.append(f"🥇 <b>طلای ۱۸ عیار:</b> {fmt_toman(gold18_curr)} تومان  {change_text(gold18_curr, gold18_prev)}")

    if coin_curr is not None:
        lines.append(f"🪙 <b>سکه تمام:</b> {fmt_toman(coin_curr)} تومان  {change_text(coin_curr, coin_prev)}")

    if ounce_curr is not None:
        lines.append(f"🌍 <b>اونس جهانی طلا:</b> {fmt_usd(ounce_curr)} دلار  {change_usd_text(ounce_curr, ounce_prev)}")

    lines.extend([
        "",
        f"🕒 <b>آخرین بروزرسانی:</b> {now}",
        "",
        "<i>🟢 افزایش | 🔴 کاهش | ⚪ بدون تغییر</i>",
    ])

    return "\n".join(lines)


# =========================================================
# HANDLERS
# =========================================================

@dp.message(Command("start", "dollar", "price"))
async def cmd_price(message: Message):
    raw_url = os.environ.get("WEB_APP_URL", "dollar-production-82c0.up.railway.app")
    web_app_url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="refresh_prices", style=ButtonStyle.SUCCESS),
                InlineKeyboardButton(text="🌐 ورود به مینی‌اپ", web_app=WebAppInfo(url=web_app_url))
            ]
        ]
    )

    wait = await message.answer("⏳ در حال دریافت آخرین قیمت‌ها...")
    try:
        data = await update_and_get_prices()
        await wait.edit_text(await build_text(data), reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await wait.edit_text("❌ دریافت قیمت‌ها ناموفق بود. لطفاً دوباره تلاش کنید.")


@dp.callback_query(F.data == "refresh_prices")
async def on_refresh(call: CallbackQuery):
    try:
        await call.answer("⏳ در حال بروزرسانی...")
        data = await update_and_get_prices()
        
        raw_url = os.environ.get("WEB_APP_URL", "dollar-production-82c0.up.railway.app")
        web_app_url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="refresh_prices", style=ButtonStyle.SUCCESS),
                    InlineKeyboardButton(text="🌐 ورود به مینی‌اپ", web_app=WebAppInfo(url=web_app_url))
                ]
            ]
        )

        await call.message.edit_text(await build_text(data), reply_markup=keyboard, parse_mode="HTML")
        await call.answer("✅ قیمت‌ها بروزرسانی شد")
    except TelegramBadRequest:
        await call.answer("قیمت‌ها تغییری نکرده")
    except Exception:
        await call.answer("❌ خطا در بروزرسانی", show_alert=True)


# =========================================================
# RUNNERS (WEB SERVER + BOT + 5-MIN UPDATER)
# =========================================================

async def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def scheduled_price_updater():
    while True:
        try:
            await asyncio.sleep(300)  # هر ۵ دقیقه
            logging.info("بررسی خودکار قیمت‌ها برای ثبت تغییرات...")
            await update_and_get_prices()
            logging.info("بررسی خودکار به پایان رسید.")
        except Exception as e:
            logging.error(f"خطا در تسک به‌روزرسانی خودکار: {e}")


async def main():
    await init_db()

    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else AiohttpSession()
    bot = Bot(token=BOT_TOKEN, session=session)

    try:
        logging.info("Bot, Web Server and 5-min Change Tracker are starting concurrently...")
        await asyncio.gather(
            run_web_server(),
            dp.start_polling(bot),
            scheduled_price_updater()
        )
    finally:
        await bot.session.close()
        if db_pool is not None:
            await db_pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped")
