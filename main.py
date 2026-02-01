import os
import json
import sqlite3
from uuid import uuid4
from datetime import date

import requests
import yfinance as yf
import pandas as pd
from bs4 import BeautifulSoup

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kis_api import order_stock  # ✅ 실제 주문 함수

# =====================
# FastAPI 앱
# =====================
app = FastAPI()

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

HEADERS = {"User-Agent": "Mozilla/5.0"}

# =====================
# 주문 임시 저장소
# =====================
ORDER_CACHE = {}

# =====================
# 주문 미리보기
# =====================
@app.post("/api/order/preview")
def order_preview(data: dict):
    ticker = data["ticker"]
    side = data["side"]  # BUY / SELL
    avg = float(data["avg_price"])
    current_price = float(data["current_price"])
    seed = float(data["seed"])

    if side == "SELL":
        price = round(avg, 2)
    else:
        price = round(min(avg * 1.05, current_price * 1.15), 2)

    qty = int((seed / 80) // price)
    if qty <= 0:
        raise HTTPException(400, "수량이 0입니다")

    order_id = str(uuid4())
    ORDER_CACHE[order_id] = {
        "ticker": ticker,
        "side": side,
        "price": price,
        "qty": qty
    }

    return {
        "order_id": order_id,
        "price": price,
        "qty": qty
    }

# =====================
# 주문 확정
# =====================
@app.post("/api/order/confirm/{order_id}")
def order_confirm(order_id: str):
    if order_id not in ORDER_CACHE:
        raise HTTPException(404, "주문 없음")

    order = ORDER_CACHE.pop(order_id)

    result = order_stock(
        ticker=order["ticker"],
        price=order["price"],
        qty=order["qty"],
        side=order["side"].lower()  # buy / sell
    )

    return {
        "status": "ok",
        "order": order,
        "result": result
    }

# =====================
# DB (RSI 히스토리)
# =====================
DB_FILE = "rsi_history.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS rsi_history (
    ticker TEXT,
    day TEXT,
    rsi REAL,
    price REAL,
    PRIMARY KEY (ticker, day)
)
""")
conn.commit()

# =====================
# Watchlist
# =====================
WATCHLIST_FILE = "watchlist.json"

def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return ["TQQQ", "SOXL", "FNGU", "UPRO"]

def save_watchlist(data):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f)

WATCHLIST = load_watchlist()

# =====================
# RSI 계산
# =====================
def calculate_wilder_rsi_series(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =====================
# Watchlist 데이터
# =====================
def get_watchlist_item(ticker: str):
    df = yf.download(ticker, period="18mo", interval="1d", progress=False)
    close = df["Close"].astype(float)

    price = float(close.iloc[-1])
    prev = float(close.iloc[-2])

    rsi_series = calculate_wilder_rsi_series(close)

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "price_change": round(price - prev, 2),
        "price_change_pct": round((price - prev) / prev * 100, 2),
        "rsi": round(float(rsi_series.iloc[-1]), 2)
    }

# =====================
# API
# =====================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/watchlist")
def watchlist():
    return [get_watchlist_item(t) for t in WATCHLIST]

@app.get("/chart/{ticker}")
def chart_data(ticker: str):
    df = yf.download(ticker, period="18mo", interval="1d", progress=False)
    close = df["Close"].astype(float)
    rsi = calculate_wilder_rsi_series(close)

    data = []
    for i in range(len(close)):
        if pd.isna(rsi.iloc[i]):
            continue
        data.append({
            "date": close.index[i].strftime("%Y-%m-%d"),
            "price": round(float(close.iloc[i]), 2),
            "rsi": round(float(rsi.iloc[i]), 2)
        })

    return JSONResponse(data)

@app.get("/chart-page", response_class=HTMLResponse)
def chart_page(request: Request):
    return templates.TemplateResponse("chart.html", {"request": request})

# =====================
# 실행
# =====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
