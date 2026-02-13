# =========================
# main.py  (WEB ONLY)
# =========================

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
from market_time import is_us_market_open, next_market_open
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

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not all([SECRET_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY]):
    raise RuntimeError("ENV not set")

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

# =====================
# AUTH
# =====================

def create_access_token(data: dict):
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    data.update({"exp": expire})
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]
    except JWTError:
        raise HTTPException(401, "invalid token")

# =====================
# WATCHLIST API
# =====================

@app.get("/tickers")
def get_tickers():
    res = supabase_admin.table("watchlist").select("ticker").execute()
    return {"tickers": [r["ticker"] for r in (res.data or [])]}

@app.post("/tickers")
def add_ticker(ticker: str = Query(...)):
    t = ticker.upper()
    supabase_admin.table("watchlist").insert({"ticker": t}).execute()
    return {"added": t}

@app.delete("/tickers/{ticker}")
def delete_ticker(ticker: str):
    supabase_admin.table("watchlist").delete().eq("ticker", ticker.upper()).execute()
    return {"removed": ticker.upper()}

# =====================
# RESERVATION LIST
# =====================

@app.get("/reservations")
def get_reservations(user: str = Depends(get_current_user)):
    res = (
        supabase_admin
        .table("queued_orders")
        .select("*")
        .eq("user_id", user)
        .eq("status", "PENDING")
        .execute()
    )
    return {"reservations": res.data or []}

# =====================
# FRONT
# =====================

@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
