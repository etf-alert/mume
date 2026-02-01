import sqlite3
from datetime import date
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
from bs4 import BeautifulSoup
import json
import os
import yfinance as yf
import pandas as pd
from kis_api import order_stock
from kis_api import get_overseas_avg_price
import json
from uuid import uuid4
app = FastAPI()
ORDER_CACHE = {}
@app.post("/api/order/preview")
def order_preview(data: dict):
    ticker = data["ticker"]
    side = data["side"]
    avg = float(data["avg_price"])
    current = float(data["current_price"])
    seed = float(data["seed"])

    if side == "SELL":
        price = avg

    elif side == "BUY_AVG":
        price = avg
        qty = int((seed / 80) // price)

    elif side == "BUY_LOC":
        price = min(avg * 1.05, current * 1.15)
        qty = int((seed / 80) // price)

    else:
        raise HTTPException(400, "invalid side")

    if qty <= 0:
        raise HTTPException(400, "수량 0")

    order_id = str(uuid4())
    ORDER_CACHE[order_id] = {
        "ticker": ticker,
        "side": side,
        "price": round(price, 2),
        "qty": qty
    }

    return {
        "order_id": order_id,
        "price": round(price, 2),
        "qty": qty
    }

    return {
        "order_id": order_id,
        "price": price,
        "qty": qty
    }

@app.post("/api/order/confirm/{order_id}")
def order_confirm(order_id: str):
    if order_id not in ORDER_CACHE:
        raise HTTPException(status_code=404, detail="order not found")

    order = ORDER_CACHE.pop(order_id)

    try:
        result = order_stock(
            ticker=order["ticker"],
            price=order["price"],
            qty=order["qty"],
            side=order["side"].lower()  # BUY → buy
        )
        return {
            "status": "ok",
            "order": order,
            "result": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/trade")
def trade(
    ticker: str = Query(...),
    price: float = Query(...),
    qty: int = Query(...),
    side: str = Query(...)  # buy / sell
):
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    try:
        result = order_stock(ticker, price, qty, side)
        return {
            "status": "ok",
            "result": result
        }
    except Exception as e:
        raise HTTPException(500, str(e))
# =====================
# DB 설정 (Cron용)
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
# FastAPI
# =====================
HEADERS = {"User-Agent": "Mozilla/5.0"}
# =====================
# Watchlist 파일
# =====================
WATCHLIST_FILE = "watchlist.json"
def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r") as f:
            return json.load(f)
    return ["TQQQ", "SOXL", "FNGU", "UPRO"]
def save_watchlist(data):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f)
WATCHLIST = load_watchlist()
# =====================
# RSI (Wilder)
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
# Finviz RSI (Cron용)
# =====================
def get_finviz_rsi(ticker: str):
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="snapshot-table2")
    data = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        for i in range(0, len(cells), 2):
            data[cells[i].text.strip()] = cells[i + 1].text.strip()
    return float(data["RSI (14)"]), data["Change"]
# =====================
# Watchlist 화면용
# =====================
def get_watchlist_item(ticker: str):
    df = yf.download(ticker,
                     period="18mo",
                     interval="1d",
                     progress=False,
                     threads=False)
    if df is None or df.empty:
        raise ValueError("Empty DataFrame")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.astype(float)
    if len(close) < 20:
        raise ValueError("Not enough data")
    # 가격
    price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2])
    price_change = price - prev_price
    price_change_pct = (price_change / prev_price) * 100
    # RSI
    rsi_series = calculate_wilder_rsi_series(close)
    rsi_today = float(rsi_series.iloc[-1])
    rsi_prev = float(rsi_series.iloc[-2])
    rsi_change = rsi_today - rsi_prev
    rsi_change_pct = (rsi_change / rsi_prev * 100) if rsi_prev != 0 else 0.0
    return {
        "ticker": ticker,
        "price": round(price, 2),
        "price_change": round(price_change, 2),
        "price_change_pct": round(price_change_pct, 2),
        "rsi": round(rsi_today, 2),
        "rsi_change": round(rsi_change, 2),
        "rsi_change_pct": round(rsi_change_pct, 2)
    }
# =====================
# Cron 저장 (선택)
# =====================
@app.post("/cron/save")
def cron_save(secret: str = Query(...)):
    if secret != "MY_SECRET_KEY":
        raise HTTPException(status_code=403, detail="Forbidden")
    today = date.today().isoformat()
    saved = []
    for t in WATCHLIST:
        try:
            rsi, _ = get_finviz_rsi(t)
            price = yf.Ticker(t).fast_info["last_price"]
            cur.execute(
                """
                INSERT OR REPLACE INTO rsi_history (ticker, day, rsi, price)
                VALUES (?, ?, ?, ?)
                """, (t, today, rsi, round(price, 2)))
            saved.append(t)
        except Exception:
            continue
    conn.commit()
    return {"saved": saved, "day": today}
# =====================
# API
# =====================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )
@app.get("/tickers")
def get_tickers():
    return {"tickers": WATCHLIST}
@app.post("/tickers")
def add_ticker(ticker: str = Query(...)):
    t = ticker.upper()
    if t in WATCHLIST:
        raise HTTPException(status_code=400, detail="Already exists")
    WATCHLIST.append(t)
    save_watchlist(WATCHLIST)
    return {"added": t}
@app.delete("/tickers/{ticker}")
def delete_ticker(ticker: str):
    t = ticker.upper()
    if t not in WATCHLIST:
        raise HTTPException(status_code=404, detail="Not found")
    WATCHLIST.remove(t)
    save_watchlist(WATCHLIST)
    return {"removed": t}
@app.get("/watchlist")
def watchlist():
    result = []
    for t in WATCHLIST:
        try:
            result.append(get_watchlist_item(t))
        except Exception as e:
            print(t, e)
    result.sort(key=lambda x: x["rsi"])
    return result
    
@app.get("/api/avg-price/{ticker}")
def avg_price(ticker: str):
    result = get_overseas_avg_price(ticker.upper())
    if not result:
        raise HTTPException(404, "보유 종목 아님")
    return result

# =====================
# 프론트
# =====================

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    return templates.TemplateResponse("app.html", {"request": request})
from fastapi.responses import JSONResponse

@app.get("/chart/{ticker}")
def chart_data(ticker: str):
    df = yf.download(ticker,
                     period="18mo",
                     interval="1d",
                     progress=False,
                     threads=False)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="No data")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.astype(float)
    rsi_series = calculate_wilder_rsi_series(close)
    close_6m = close.iloc[-126:]
    rsi_6m = rsi_series.iloc[-126:]
    data = []
    for i in range(len(close)):
        if pd.isna(rsi_series.iloc[i]):
            continue
        data.append({
            "date": close.index[i].strftime("%Y-%m-%d"),
            "price": round(float(close.iloc[i]), 2),
            "rsi": round(float(rsi_series.iloc[i]), 2)
        })
    return JSONResponse(data)
    
@app.get("/chart-page", response_class=HTMLResponse)
def chart_page(request: Request):
    return templates.TemplateResponse("chart.html", {"request": request})
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
