import asyncio
import logging
import re
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
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
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = "8565706620:AAEwo_-F2RuRYUrCz7b7X3GvwydJmGHlLXM"

# تنظیمات پروکسی برای اتصال به تلگرام در ایران
PROXY_URL = "socks5://127.0.0.1:10808"

URL_CURRENCY = "https://alanchand.com/en/currencies-price"
URL_CRYPTO = "https://alanchand.com/en/crypto-price"
URL_GOLD = "https://alanchand.com/en/gold-price"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

dp = Dispatcher()


def to_int(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def irr_to_toman(cell: str) -> int | None:
    """عدد ریالی رو از متن سلول درمیاره و به تومان تبدیل می‌کنه."""
    m = re.search(r"([\d,]+)\s*IRR", cell)
    if not m:
        return None
    n = to_int(m.group(1))
    return n // 10 if n else None


def usd_value(cell: str) -> str | None:
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", cell)
    return m.group(1) if m else None


async def get_rows(session: aiohttp.ClientSession, url: str) -> list[list[str]]:
    async with session.get(url) as resp:
        resp.raise_for_status()
        html = await resp.text()
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    return rows


def parse_currency(rows: list[list[str]]) -> dict:
    out = {}
    for r in rows:
        if len(r) < 3:
            continue
        name = r[0].strip().lower()
        if name in ("us dollar", "euro"):
            buy, sell = to_int(r[1]), to_int(r[2])
            if buy and sell:
                out[name] = {"buy": buy // 10, "sell": sell // 10}  # ریال ÷ ۱۰ = تومان
    if "us dollar" not in out:
        raise ValueError("قیمت دلار پیدا نشد")
    return out


def parse_crypto(rows: list[list[str]]) -> dict:
    out = {}
    for r in rows:
        if len(r) < 3:
            continue
        name = r[0].strip().upper()
        if name.endswith("USDT"):
            out["usdt"] = irr_to_toman(r[1])
        elif name.endswith("BTC"):
            out["btc"] = usd_value(r[2])
    if not out:
        raise ValueError("قیمت رمزارز پیدا نشد")
    return out


def parse_gold(rows: list[list[str]]) -> dict:
    out = {}
    for r in rows:
        if len(r) < 2:
            continue
        name = r[0].strip().lower()
        if name.startswith("18k gold"):
            out["gold18"] = irr_to_toman(r[1])
        elif name.startswith("full coin"):
            out["coin"] = irr_to_toman(r[1])
        elif name.startswith("gold ounce"):
            out["ounce"] = usd_value(r[1])
    if not out:
        raise ValueError("قیمت طلا پیدا نشد")
    return out


async def fetch_all() -> dict:
    """هر سه صفحه همزمان گرفته می‌شن. اگه یکی خطا بده، بقیه سالم می‌مونن."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        results = await asyncio.gather(
            get_rows(session, URL_CURRENCY),
            get_rows(session, URL_CRYPTO),
            get_rows(session, URL_GOLD),
            return_exceptions=True,
        )

    data = {}
    for key, parser, res in zip(
        ("currency", "crypto", "gold"),
        (parse_currency, parse_crypto, parse_gold),
        results,
    ):
        try:
            if isinstance(res, Exception):
                raise res
            data[key] = parser(res)
        except Exception as e:
            logging.warning("%s failed: %s: %s", key, type(e).__name__, e)
            data[key] = None

    if all(v is None for v in data.values()):
        raise ValueError("هیچ قیمتی دریافت نشد (اتصال به سایت یا ساختار صفحه رو بررسی کن)")
    return data


def fmt_toman(n: int | None) -> str:
    return f"{n:,}" if n else "—"


def build_text(d: dict) -> str:
    now = datetime.now(ZoneInfo("Asia/Tehran")).strftime("%H:%M:%S")

    cur = d["currency"] or {}
    usd = cur.get("us dollar")
    eur = cur.get("euro")
    crypto = d["crypto"] or {}
    gold = d["gold"] or {}

    lines = ["📊 <b>قیمت لحظه‌ای بازار</b>\n"]

    if usd:
        lines.append(
            f"💵 <b>دلار</b>\n"
            f"   خرید: <b>{usd['buy']:,}</b> | فروش: <b>{usd['sell']:,}</b> تومان\n"
        )
    else:
        lines.append("💵 <b>دلار:</b> —\n")

    if eur:
        lines.append(
            f"💶 <b>یورو</b>\n"
            f"   خرید: <b>{eur['buy']:,}</b> | فروش: <b>{eur['sell']:,}</b> تومان\n"
        )
    else:
        lines.append("💶 <b>یورو:</b> —\n")

    lines.append(f"💲 <b>تتر:</b> {fmt_toman(crypto.get('usdt'))} تومان")
    btc = crypto.get("btc")
    lines.append(f"₿ <b>بیت‌کوین:</b> {btc + ' دلار' if btc else '—'}\n")

    lines.append(f"🥇 <b>طلا ۱۸ عیار (هر گرم):</b> {fmt_toman(gold.get('gold18'))} تومان")
    lines.append(f"🪙 <b>سکه امامی:</b> {fmt_toman(gold.get('coin'))} تومان")
    ounce = gold.get("ounce")
    lines.append(f"🌍 <b>اونس جهانی طلا:</b> {ounce + ' دلار' if ounce else '—'}\n")

    lines.append(f"🕒 آخرین بروزرسانی: {now}")
    lines.append("📡 منبع: alanchand.com")
    return "\n".join(lines)


def refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🔄 بروزرسانی قیمت‌ها",
                callback_data="refresh_prices",
                style=ButtonStyle.SUCCESS,
            )
        ]]
    )


@dp.message(Command("start", "dollar", "price"))
async def cmd_price(message: Message):
    wait = await message.answer("⏳ در حال دریافت قیمت‌ها...")
    try:
        data = await fetch_all()
        await wait.edit_text(
            build_text(data), reply_markup=refresh_keyboard(), parse_mode="HTML"
        )
    except Exception as e:
        traceback.print_exc()
        await wait.edit_text(f"❌ خطا: {type(e).__name__}: {e}")


@dp.callback_query(F.data.in_({"refresh_prices", "refresh_dollar"}))
async def on_refresh(call: CallbackQuery):
    try:
        data = await fetch_all()
    except Exception:
        traceback.print_exc()
        await call.answer("❌ دریافت قیمت ناموفق بود، دوباره تلاش کن", show_alert=True)
        return

    try:
        await call.message.edit_text(
            build_text(data), reply_markup=refresh_keyboard(), parse_mode="HTML"
        )
        await call.answer("✅ قیمت‌ها بروز شد")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await call.answer("قیمت تغییری نکرده")
        else:
            raise


async def main():
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(BOT_TOKEN, session=session)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
