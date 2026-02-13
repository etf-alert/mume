# ==========================================================
# 🔥 CRON-JOB.ORG VERSION
# APScheduler / asyncio 루프 완전 제거
# 외부 cron-job.org 에서 HTTP 호출하는 구조
# ==========================================================

from datetime import date, datetime, timedelta, timezone, UTC
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import requests
import time
from bs4 import BeautifulSoup
from supabase import create_client, ClientOptions
import pytz
import os
import yfinance as yf
import pandas as pd
from kis_api import order_overseas_stock, get_overseas_avg_price, get_overseas_buying_power
from uuid import uuid4
from market_time import (
    is_us_market_open,
    is_us_premarket,
    is_us_postmarket,
    next_market_open,
    get_next_trading_day,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockSnapshotRequest
import pandas_market_calendars as mcal

# =====================
# ENV
# =====================
SECRET_KEY = os.getenv("JWT_SECRET")
ALGORITHM = "HS256"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
CRON_SECRET = os.getenv("CRON_SECRET")

if not all([SECRET_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY, CRON_SECRET]):
    raise RuntimeError("ENV 누락")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

alpaca_data = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

# =====================
# Supabase
# =====================
supabase_admin = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

def get_user_supabase(token: str):
    return create_client(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {token}"}
        )
    )

# =====================
# FastAPI
# =====================
app = FastAPI()
ORDER_CACHE: dict[str, dict] = {}

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

nyse = mcal.get_calendar("NYSE")
ny_tz = pytz.timezone("US/Eastern")

# ==========================================================
# 🔥 CRON SAVE (RSI 저장용)
# cron-job.org 에서 하루 1회 호출
# ==========================================================
@app.post("/cron/save")
def cron_save(secret: str = Query(...)):
    if secret != CRON_SECRET:
        raise HTTPException(403)

    now = datetime.now(ny_tz)

    schedule = nyse.schedule(
        start_date=now.date(),
        end_date=now.date()
    )
    if schedule.empty:
        return {"status": "holiday"}

    close_time = schedule.iloc[0]["market_close"].to_pydatetime()

    if not (close_time + timedelta(minutes=3)
            <= now
            <= close_time + timedelta(minutes=8)):
        return {"status": "not close window"}

    today = now.date().isoformat()

    res = supabase_admin.table("watchlist").select("ticker").execute()
    tickers = [r["ticker"] for r in (res.data or [])]

    if not tickers:
        return {"status": "no tickers"}

    rows = []

    for t in tickers:
        try:
            rsi = get_finviz_rsi(t)
            if rsi is None:
                continue

            price_df = yf.download(t, period="1d", progress=False)
            if price_df.empty:
                continue

            price = float(price_df["Close"].iloc[-1])

            rows.append({
                "ticker": t,
                "day": today,
                "rsi": round(float(rsi), 2),
                "price": round(price, 2),
            })

            time.sleep(0.6)

        except Exception as e:
            print("cron_save error:", t, e)

    if not rows:
        return {"status": "no data"}

    supabase_admin.table("rsi_history").upsert(
        rows,
        on_conflict="ticker,day"
    ).execute()

    return {"saved": len(rows)}


# ==========================================================
# 🔥 CRON EXECUTE RESERVATIONS
# cron-job.org 에서 1~2분마다 호출
# ==========================================================
@app.post("/cron/execute-reservations")
def cron_execute(secret: str = Query(...)):
    if secret != CRON_SECRET:
        raise HTTPException(403)

    now = datetime.now(timezone.utc)

    res = (
        supabase_admin
        .table("queued_orders")
        .select("*")
        .eq("status", "PENDING")
        .lte("execute_after", now.isoformat())
        .execute()
    )

    for o in res.data or []:
        try:
            pos = get_overseas_avg_price(o["ticker"])
            if not pos.get("found"):
                raise RuntimeError("보유 종목 없음")

            avg_price = float(pos.get("avg_price", 0))
            sellable_qty = float(pos.get("sellable_qty", 0))

            current_price = resolve_prices(o["ticker"])["base_price"]

            preview = build_order_preview({
                "side": o["side"],
                "avg_price": avg_price,
                "current_price": current_price,
                "seed": o["seed"],
                "ticker": o["ticker"]
            })

            side = "buy" if o["side"].startswith("BUY") else "sell"

            if side == "sell":
                qty = int(sellable_qty)
            else:
                qty = preview["qty"]

            if qty <= 0:
                raise RuntimeError("주문 수량 0")

            kis_res = order_overseas_stock(
                ticker=o["ticker"],
                price=preview["price"],
                qty=qty,
                side=side
            )

            if not kis_res or kis_res.get("rt_cd") != "0":
                raise RuntimeError("KIS 실패")

            supabase_admin.table("queued_orders").update({
                "status": "DONE",
                "executed_at": now.isoformat(),
                "error": None
            }).eq("id", o["id"]).execute()

        except Exception as e:
            supabase_admin.table("queued_orders").update({
                "error": str(e)
            }).eq("id", o["id"]).execute()

    return {"status": "ok"}
