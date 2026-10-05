import asyncio
import logging
import os
import re
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


# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# اختیاری:
# اگر در Railway پروکسی نداری، این Variable را اصلاً نساز.
PROXY_URL = os.getenv("PROXY_URL", "").strip()

URL_CURRENCY = "https://alanchand.com/en/currencies-price"
URL_CRYPTO = "https://alanchand.com/en/crypto-price"
URL_GOLD = "https://alanchand.com/en/gold-price"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

dp = Dispatcher()


# =========================================================
# HELPERS
# =========================================================

def to_int(value: str) -> int | None:
    """تمام کاراکترهای غیرعددی را حذف و عدد را برمی‌گرداند."""
    digits = re.sub(r"[^\d]", "", value)

    if not digits:
        return None

    return int(digits)


def irr_to_toman(cell: str) -> int | None:
    """
    مقدار IRR را از متن پیدا می‌کند
    و ریال را به تومان تبدیل می‌کند.
    """

    match = re.search(
        r"([\d,]+)\s*IRR",
        cell,
        re.IGNORECASE,
    )

    if not match:
        return None

    number = to_int(match.group(1))

    if number is None:
        return None

    return number // 10


def usd_value(cell: str) -> str | None:
    """قیمت دلاری مثل $123,456 را استخراج می‌کند."""

    match = re.search(
        r"\$\s*([\d,]+(?:\.\d+)?)",
        cell,
    )

    if not match:
        return None

    return match.group(1)


def fmt_toman(number: int | None) -> str:
    """نمایش عدد به صورت سه‌رقمی."""

    if number is None:
        return "—"

    return f"{number:,}"


# =========================================================
# GET TABLE ROWS
# =========================================================

async def get_rows(
    session: aiohttp.ClientSession,
    url: str,
) -> list[list[str]]:

    async with session.get(url) as response:

        response.raise_for_status()

        html = await response.text()

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    rows = []

    for tr in soup.find_all("tr"):

        cells = [
            cell.get_text(
                " ",
                strip=True,
            )
            for cell in tr.find_all(
                ["td", "th"]
            )
        ]

        if cells:
            rows.append(cells)

    return rows


# =========================================================
# PARSE CURRENCY
# =========================================================

def parse_currency(
    rows: list[list[str]],
) -> dict:

    output = {}

    for row in rows:

        if len(row) < 3:
            continue

        name = row[0].strip().lower()

        if name in (
            "us dollar",
            "euro",
        ):

            buy = to_int(row[1])
            sell = to_int(row[2])

            if buy is not None and sell is not None:

                output[name] = {
                    "buy": buy // 10,
                    "sell": sell // 10,
                }

    if "us dollar" not in output:

        raise ValueError(
            "قیمت دلار پیدا نشد"
        )

    return output


# =========================================================
# PARSE CRYPTO
# =========================================================

def parse_crypto(
    rows: list[list[str]],
) -> dict:

    output = {}

    for row in rows:

        if len(row) < 3:
            continue

        name = row[0].strip().upper()

        if name.endswith("USDT"):

            output["usdt"] = irr_to_toman(
                row[1]
            )

        elif name.endswith("BTC"):

            output["btc"] = usd_value(
                row[2]
            )

    if not output:

        raise ValueError(
            "قیمت رمزارز پیدا نشد"
        )

    return output


# =========================================================
# PARSE GOLD
# =========================================================

def parse_gold(
    rows: list[list[str]],
) -> dict:

    output = {}

    for row in rows:

        if len(row) < 2:
            continue

        name = row[0].strip().lower()

        if name.startswith("18k gold"):

            output["gold18"] = irr_to_toman(
                row[1]
            )

        elif name.startswith("full coin"):

            output["coin"] = irr_to_toman(
                row[1]
            )

        elif name.startswith("gold ounce"):

            output["ounce"] = usd_value(
                row[1]
            )

    if not output:

        raise ValueError(
            "قیمت طلا پیدا نشد"
        )

    return output


# =========================================================
# FETCH ALL PRICES
# =========================================================

async def fetch_all() -> dict:

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    connector = aiohttp.TCPConnector(
        limit=10,
        ttl_dns_cache=300,
    )

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout,
        connector=connector,
    ) as session:

        results = await asyncio.gather(

            get_rows(
                session,
                URL_CURRENCY,
            ),

            get_rows(
                session,
                URL_CRYPTO,
            ),

            get_rows(
                session,
                URL_GOLD,
            ),

            return_exceptions=True,
        )

    data = {}

    parsers = (

        (
            "currency",
            parse_currency,
        ),

        (
            "crypto",
            parse_crypto,
        ),

        (
            "gold",
            parse_gold,
        ),
    )

    for (
        key,
        parser,
    ), result in zip(
        parsers,
        results,
    ):

        try:

            if isinstance(
                result,
                Exception,
            ):
                raise result

            data[key] = parser(result)

        except Exception as error:

            logging.warning(
                "%s failed: %s: %s",
                key,
                type(error).__name__,
                error,
            )

            data[key] = None

    if all(
        value is None
        for value in data.values()
    ):

        raise ValueError(
            "هیچ قیمتی دریافت نشد"
        )

    return data


# =========================================================
# BUILD BOT MESSAGE
# =========================================================

def build_text(
    data: dict,
) -> str:

    now = datetime.now(
        ZoneInfo("Asia/Tehran")
    ).strftime(
        "%Y/%m/%d | %H:%M:%S"
    )

    currency = (
        data.get("currency")
        or {}
    )

    crypto = (
        data.get("crypto")
        or {}
    )

    gold = (
        data.get("gold")
        or {}
    )

    usd = currency.get(
        "us dollar"
    )

    eur = currency.get(
        "euro"
    )

    lines = [
        "📊 <b>قیمت لحظه‌ای بازار</b>",
        "",
    ]

    # -------------------------
    # DOLLAR
    # -------------------------

    if usd:

        lines.extend([
            "💵 <b>دلار آمریکا</b>",
            (
                f"   خرید: "
                f"<b>{usd['buy']:,}</b> تومان"
            ),
            (
                f"   فروش: "
                f"<b>{usd['sell']:,}</b> تومان"
            ),
            "",
        ])

    else:

        lines.extend([
            "💵 <b>دلار:</b> —",
            "",
        ])

    # -------------------------
    # EURO
    # -------------------------

    if eur:

        lines.extend([
            "💶 <b>یورو</b>",
            (
                f"   خرید: "
                f"<b>{eur['buy']:,}</b> تومان"
            ),
            (
                f"   فروش: "
                f"<b>{eur['sell']:,}</b> تومان"
            ),
            "",
        ])

    else:

        lines.extend([
            "💶 <b>یورو:</b> —",
            "",
        ])

    # -------------------------
    # USDT
    # -------------------------

    usdt = crypto.get(
        "usdt"
    )

    lines.append(
        "💲 <b>تتر:</b> "
        f"{fmt_toman(usdt)} تومان"
    )

    # -------------------------
    # BITCOIN
    # -------------------------

    btc = crypto.get(
        "btc"
    )

    if btc:

        lines.append(
            f"₿ <b>بیت‌کوین:</b> "
            f"{btc} دلار"
        )

    else:

        lines.append(
            "₿ <b>بیت‌کوین:</b> —"
        )

    lines.append("")

    # -------------------------
    # GOLD
    # -------------------------

    gold18 = gold.get(
        "gold18"
    )

    lines.append(
        "🥇 <b>طلای ۱۸ عیار "
        "(هر گرم):</b> "
        f"{fmt_toman(gold18)} تومان"
    )

    # -------------------------
    # COIN
    # -------------------------

    coin = gold.get(
        "coin"
    )

    lines.append(
        "🪙 <b>سکه تمام:</b> "
        f"{fmt_toman(coin)} تومان"
    )

    # -------------------------
    # GOLD OUNCE
    # -------------------------

    ounce = gold.get(
        "ounce"
    )

    if ounce:

        lines.append(
            f"🌍 <b>اونس جهانی طلا:</b> "
            f"{ounce} دلار"
        )

    else:

        lines.append(
            "🌍 <b>اونس جهانی طلا:</b> —"
        )

    lines.extend([
        "",
        f"🕒 <b>آخرین دریافت:</b> {now}",
        "📡 <b>منبع:</b> alanchand.com",
        "",
        (
            "<i>قیمت‌ها ممکن است با "
            "تأخیر منبع همراه باشند.</i>"
        ),
    ])

    return "\n".join(lines)


# =========================================================
# KEYBOARD
# =========================================================

def refresh_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی قیمت‌ها",
                    callback_data="refresh_prices",
                    style=ButtonStyle.SUCCESS,
                )
            ]
        ]
    )


# =========================================================
# /START /DOLLAR /PRICE
# =========================================================

@dp.message(
    Command(
        "start",
        "dollar",
        "price",
    )
)
async def cmd_price(
    message: Message,
):

    wait = await message.answer(
        "⏳ در حال دریافت آخرین قیمت‌ها..."
    )

    try:

        data = await fetch_all()

        await wait.edit_text(
            build_text(data),
            reply_markup=refresh_keyboard(),
            parse_mode="HTML",
        )

    except Exception:

        logging.exception(
            "Failed to fetch prices"
        )

        await wait.edit_text(
            "❌ دریافت قیمت‌ها ناموفق بود.\n\n"
            "لطفاً چند لحظه دیگر دوباره تلاش کن."
        )


# =========================================================
# REFRESH BUTTON
# =========================================================

@dp.callback_query(
    F.data.in_(
        {
            "refresh_prices",
            "refresh_dollar",
        }
    )
)
async def on_refresh(
    call: CallbackQuery,
):

    try:

        await call.answer(
            "⏳ در حال بروزرسانی..."
        )

        data = await fetch_all()

        text = build_text(data)

        await call.message.edit_text(
            text,
            reply_markup=refresh_keyboard(),
            parse_mode="HTML",
        )

        await call.answer(
            "✅ قیمت‌ها بروزرسانی شد"
        )

    except TelegramBadRequest as error:

        if (
            "message is not modified"
            in str(error).lower()
        ):

            await call.answer(
                "قیمت‌ها تغییری نکرده"
            )

            return

        logging.exception(
            "Telegram update error"
        )

    except Exception:

        logging.exception(
            "Price refresh failed"
        )

        try:

            await call.answer(
                "❌ دریافت قیمت ناموفق بود",
                show_alert=True,
            )

        except Exception:

            logging.exception(
                "Could not answer callback"
            )


# =========================================================
# MAIN
# =========================================================

async def main():

    if PROXY_URL:

        logging.info(
            "Starting bot with proxy"
        )

        session = AiohttpSession(
            proxy=PROXY_URL
        )

    else:

        logging.info(
            "Starting bot without proxy"
        )

        session = AiohttpSession()

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
    )

    try:

        logging.info(
            "Price bot is starting..."
        )

        await dp.start_polling(
            bot
        )

    finally:

        await bot.session.close()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logging.info(
            "Bot stopped"
        )
