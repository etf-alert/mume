# =========================
# worker.py  (BACKGROUND WORKER)
# =========================

from datetime import datetime, timedelta, timezone
import time
import os
import pytz
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from supabase import create_client

from kis_api import order_overseas_stock, get_overseas_avg_price
from market_time import is_us_market_open

# =====================
# ENV
# =====================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE ENV not set")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

ny_tz = pytz.timezone("US/Eastern")

# =====================
# 🔥 예약 주문 실행
# =====================

def execute_pending_orders():
    now = datetime.now(timezone.utc)

    res = (
        supabase
        .table("queued_orders")
        .select("*")
        .eq("status", "PENDING")
        .lte("execute_after", now.isoformat())
        .execute()
    )

    for o in res.data or []:
        try:
            pos = get_overseas_avg_price(o["ticker"])
            avg_price = float(pos.get("avg_price", 0))
            sellable_qty = float(pos.get("sellable_qty", 0))

            if o["side"].startswith("BUY"):
                price = avg_price
                qty = int((float(o["seed"]) / 80) // price)
                side = "buy"
            else:
                price = avg_price * 1.1
                qty = int(sellable_qty)
                side = "sell"

            if qty <= 0:
                raise RuntimeError("qty 0")

            order_overseas_stock(
                ticker=o["ticker"],
                price=price,
                qty=qty,
                side=side
            )

            supabase.table("queued_orders").update({
                "status": "DONE",
                "executed_at": now.isoformat()
            }).eq("id", o["id"]).execute()

            print("ORDER DONE:", o["ticker"])

        except Exception as e:
            print("ORDER ERROR:", e)

# =====================
# 🔥 RSI 저장
# =====================

HEADERS = {"User-Agent": "Mozilla/5.0"}

def get_finviz_rsi(ticker):
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="snapshot-table2")
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        for i in range(0, len(cells), 2):
            if cells[i].text.strip() == "RSI (14)":
                return float(cells[i+1].text.strip())
    return None

def save_rsi():
    now_ny = datetime.now(ny_tz)

    if now_ny.weekday() >= 5:
        return

    if now_ny.hour != 16:  # 장마감 직후 시간대 맞춰 조정 가능
        return

    today = now_ny.date().isoformat()

    res = supabase.table("watchlist").select("ticker").execute()
    tickers = [r["ticker"] for r in (res.data or [])]

    for t in tickers:
        try:
            rsi = get_finviz_rsi(t)
            if rsi is None:
                continue

            supabase.table("rsi_history").upsert({
                "ticker": t,
                "day": today,
                "rsi": round(rsi, 2)
            }, on_conflict="ticker,day").execute()

            time.sleep(0.5)

        except Exception as e:
            print("RSI ERROR:", t, e)

# =====================
# 🔥 MAIN LOOP
# =====================

print("🔥 Worker started")

while True:
    try:
        execute_pending_orders()
        save_rsi()
    except Exception as e:
        print("WORKER ERROR:", e)

    time.sleep(60)  # 1분 루프
