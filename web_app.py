from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import asyncpg
import os

app = FastAPI()
DATABASE_URL = os.getenv("DATABASE_URL", "")

@app.get("/api/prices")
async def get_prices():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="Database URL not set")
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # آخرین قیمت‌های ثبت شده در دیتابیس
        rows = await conn.fetch("""
            SELECT symbol, value, updated_at 
            FROM daily_prices 
            WHERE price_date = (SELECT MAX(price_date) FROM daily_prices)
        """)
        return {row["symbol"]: {"value": row["value"], "updated_at": row["updated_at"]} for row in rows}
    finally:
        await conn.close()

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    # کدهای HTML مینی‌اپ که در بخش بعدی آمده را می‌توانید اینجا قرار دهید
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()
