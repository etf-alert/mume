from datetime import date, datetime, timedelta, timezone, UTC
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import requests
import time
from pydantic import BaseModel
from bs4 import BeautifulSoup
from supabase import create_client, ClientOptions
import json
import os
import yfinance as yf
import pandas as pd
from kis_api import order_overseas_stock, get_overseas_avg_price
from uuid import UUID, uuid4
from market_time import is_us_market_open, next_market_open
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockSnapshotRequest
from market_time import (is_us_market_open,is_us_premarket,is_us_postmarket,)

# =====================
# ENV
# =====================
SECRET_KEY = os.getenv("JWT_SECRET", "change-this")
ALGORITHM = "HS256"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL not set")
if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY not set")
if not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_ANON_KEY not set")
if SECRET_KEY == "change-this":
    raise RuntimeError("JWT_SECRET not set")

# =====================
# Supabase clients
# =====================

# 🔥 서버 / cron 전용 (service role, RLS 무시)
supabase_admin = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

# 👤 사용자 요청용 (RLS 적용)
def get_user_supabase(token: str):
    return create_client(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        options=ClientOptions(
            headers={
                "Authorization": f"Bearer {token}"
            }
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

# =====================
# Auth utils
# =====================
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]  # user_id (uuid string)
    except JWTError:
        raise HTTPException(status_code=401, detail="invalid token")
        
def require_login_page(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

# =====================
# Auth API
# =====================
@app.get("/")
def root():
    return RedirectResponse("/app")

@app.post("/api/auth/login")
def login(data: dict):
    user_id = data["id"]
    password = data["password"]

    if user_id != os.getenv("ADMIN_ID") or password != os.getenv("ADMIN_PW"):
        raise HTTPException(status_code=401, detail="invalid credentials")

    supabase_user_id = os.getenv("ADMIN_USER_UUID")  # ✅ UUID

    token = create_access_token({
        "sub": supabase_user_id   # ⭐ UUID 들어가야 함
    })

    res = JSONResponse({
        "access_token": token,
        "token_type": "bearer"
    })
    res.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax"
    )
    return res
     
def get_yahoo_quote(ticker: str) -> dict:
    """
    Returns:
    {
        "regular": float | None,
        "pre": float | None,
        "post": float | None
    }
    """
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ticker}
    try:
        r = requests.get(url, params=params, timeout=3)
        r.raise_for_status()
        q = r.json()["quoteResponse"]["result"][0]

        return {
            "regular": q.get("regularMarketPrice"),
            "pre": q.get("preMarketPrice"),
            "post": q.get("postMarketPrice"),
        }
    except Exception:
        return {"regular": None, "pre": None, "post": None}

def get_realtime_price(ticker: str) -> dict:
    """
    Alpaca 우선 → Yahoo fallback
    """
    regular = pre = post = None

    # =====================
    # Alpaca
    # =====================
    try:
        trade = alpaca_data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=ticker)
        )
        regular = float(trade[ticker].price)
    except Exception:
        pass

    try:
        snap = alpaca_data.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=ticker)
        )
        s = snap[ticker]
        if s.pre_market_trade:
            pre = float(s.pre_market_trade.price)
        if s.post_market_trade:
            post = float(s.post_market_trade.price)
    except Exception:
        pass

    # =====================
    # Yahoo fallback
    # =====================
    if pre is None or post is None:
        y = get_yahoo_quote(ticker)
        pre = pre or y["pre"]
        post = post or y["post"]
        regular = regular or y["regular"]

    return {
        "regular": regular,
        "pre": pre,
        "post": post,
    }
    
def get_market_phase(now=None):
    """
    Returns: REGULAR | PRE | POST | CLOSE
    """
    if is_us_market_open(now):
        return "REGULAR"
    if is_us_premarket(now):
        return "PRE"
    if is_us_postmarket(now):
        return "POST"
    return "CLOSE"

def resolve_prices(ticker: str):
    closes = get_yf_daily_closes(ticker, period="5d")
    close_price = closes[-1]
    prev_close = closes[-2]
    realtime = get_realtime_price(ticker)
    phase = get_market_phase()

    # 기준가 (항상 정규장 기준)
    base_price = realtime["regular"] or close_price

    if phase == "REGULAR":
        display_price = base_price
        price_source = "REGULAR"
    else:
        display_price = base_price
        price_source = "CLOSE"

    current_change = base_price - prev_close
    current_change_pct = (current_change / prev_close) * 100

    return {
        "base_price": round(base_price, 2),
        "display_price": round(display_price, 2),
        "price_source": price_source,
        "current_change": round(current_change, 2),
        "current_change_pct": round(current_change_pct, 2),
        # ❌ 시간외 완전 제거
        "after_change": None,
        "after_change_pct": None,
    }

def get_yf_daily_closes(ticker: str, period="6mo") -> list[float]:
    df = yf.download(
        ticker,
        period=period,
        interval="1d",
        progress=False,
        threads=False
    )
    if df is None or df.empty:
        raise ValueError("No yfinance data")

    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    return close.astype(float).tolist()

@app.post("/api/order/preview")
def order_preview(
    data: dict,
    user: str = Depends(get_current_user)
):
    try:
        # ✅ 기본값 (모든 분기에서 안전)
        price_type = None
        message = None

        side = data["side"]
        avg = float(data["avg_price"])
        cur = float(data["current_price"])
        seed = float(data["seed"])
        ticker = data["ticker"]

        # ✅ 가격 결정
        if side == "BUY_MARKET":
            price = round(min(avg * 1.05, cur * 1.15), 2)
            qty = int((seed / 80) // price)
            price_type = "LOC"
            message = "큰 수 매수 (LOC)"

        elif side == "BUY_AVG":
            price = round(avg, 2)
            qty = int((seed / 80) // price)
            price_type = "LOC"
            message = "평단가 매수 (LOC)"

        elif side == "SELL":
            pos = get_overseas_avg_price(ticker)
            qty = pos["qty"]

            if qty <= 0:
                raise HTTPException(400, "매도 가능 수량 없음")

            target_price = round(avg * 1.10, 2)

            if cur > target_price:
                price = round(cur, 2)
                price_type = "MARKET_BETTER"
                message = "현재가가 목표가보다 높아 현재가로 매도"
            else:
                price = target_price
                price_type = "TARGET"
                message = "목표가(평단+10%)로 매도"

        else:
            raise HTTPException(400, "invalid side")

        if qty <= 0:
            raise HTTPException(400, "수량 0")

        order_id = str(uuid4())
        ORDER_CACHE[order_id] = {
            "side": side,
            "price": price,
            "qty": qty,
            "ticker": ticker,
            "price_type": price_type,
            "message": message,
            "created_at": datetime.now(UTC)
        }

        if len(ORDER_CACHE) > 1000:
            ORDER_CACHE.clear()

        return {
            "order_id": order_id,
            "price": price,
            "qty": qty,
            "price_type": price_type,
            "message": message
        }

    except HTTPException:
        raise  # FastAPI용 에러는 그대로 던짐

    except Exception as e:
        print("❌ order_preview error:", e)
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/api/order/execute/{order_id}")
def execute_order(
    order_id: str,
    user: str = Depends(get_current_user)
):
    order = ORDER_CACHE.get(order_id)
    if not order:
        raise HTTPException(404, "order not found")

    # ⏰ 만료 체크
    if datetime.now(UTC) - order["created_at"] > timedelta(minutes=5):
        ORDER_CACHE.pop(order_id, None)
        raise HTTPException(400, "order expired")

    # 🔁 매도 수량 재검증
    if order["side"] == "SELL":
        pos = get_overseas_avg_price(order["ticker"])
        if order["qty"] > pos["qty"]:
            raise HTTPException(400, "보유 수량 부족")

    # ✅ 장 상태
    is_open = is_us_market_open()
    next_open = next_market_open()

    # ==========================
    # 🌙 장 닫힘 → Supabase 큐잉
    # ==========================
    if not is_open:
        supabase_admin.table("queued_orders").insert({
            "id": order_id,
            "ticker": order["ticker"],
            "side": order["side"],
            "price": order["price"],
            "qty": order["qty"],
            "execute_after": next_open.astimezone(timezone.utc).isoformat(),
            "status": "PENDING",
            "user_id": user   # ⭐ 필수
        }).execute()

        ORDER_CACHE.pop(order_id, None)

        # 응답용 KST 변환
        KST = timezone(timedelta(hours=9))
        execute_after_kst = next_open.astimezone(KST)

        return {
            "status": "queued",
            "message": "장 시작 후 자동 실행",
            "execute_after": execute_after_kst.strftime("%Y-%m-%d %H:%M (KST)")
        }

    # ==========================
    # 📈 정규장 → 즉시 실행
    # ==========================
    side = "buy" if order["side"].startswith("BUY") else "sell"

    try:
        result = order_overseas_stock(
            ticker=order["ticker"],
            price=order["price"],
            qty=order["qty"],
            side=side
        )

        ORDER_CACHE.pop(order_id, None)

        return {
            "status": "ok",
            "order": order,
            "result": result
        }

    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                msg = e.response.text
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=msg)

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
# RSI (wilder)
# =====================
def calculate_wilder_rsi_series(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder smoothing (EMA with alpha = 1/period)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi    
    
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
def get_rsi_from_history(ticker: str):
    """
    Finviz RSI 기준 (rsi_history 테이블)
    returns:
    {
      rsi: float | None,
      rsi_change: float | None,
      rsi_change_pct: float | None
    }
    """
    res = (
        supabase_admin
        .table("rsi_history")
        .select("day, rsi")
        .eq("ticker", ticker)
        .order("day", desc=True)
        .limit(2)
        .execute()
    )

    rows = res.data or []
    if len(rows) == 0:
        return {
            "rsi": None,
            "rsi_change": None,
            "rsi_change_pct": None,
            "rsi_valid": False
        }

    today = float(rows[0]["rsi"])

    if len(rows) == 1:
        return {
            "rsi": round(today, 2),
            "rsi_change": 0.0,
            "rsi_change_pct": 0.0
        }

    prev = float(rows[1]["rsi"])
    change = today - prev
    change_pct = (change / prev) * 100 if prev != 0 else 0.0

    return {
        "rsi": round(today, 2),
        "rsi_change": round(change, 2),
        "rsi_change_pct": round(change_pct, 2)
    }

def get_watchlist_item(ticker: str):
    p = resolve_prices(ticker)

    rsi_data = get_rsi_from_history(ticker)

    item = {
        "ticker": ticker,

        # 💰 가격
        "current_price": p["base_price"],
        "current_change": p["current_change"],
        "current_change_pct": p["current_change_pct"],
        "display_price": p["display_price"],
        "after_change": p["after_change"],
        "after_change_pct": p["after_change_pct"],
        "price_source": p["price_source"],

        # 📊 RSI (Finviz)
        "rsi": rsi_data["rsi"],
        "rsi_change": rsi_data["rsi_change"],
        "rsi_change_pct": rsi_data["rsi_change_pct"],
    }

    print("WATCHLIST ITEM DEBUG:", item)
    return item

# =====================
# Cron 저장
# =====================
@app.post("/cron/save")
def cron_save(secret: str = Query(...)):
    if secret != os.getenv("CRON_SECRET"):
        raise HTTPException(status_code=403, detail="Forbidden")

    today = date.today().isoformat()
    rows = []

    for t in WATCHLIST:
        try:
            rsi, _ = get_finviz_rsi(t)
            price = yf.Ticker(t).fast_info["last_price"]
            rows.append({
                "ticker": t,
                "day": today,
                "rsi": float(rsi),
                "price": round(float(price), 2),
            })
            time.sleep(1.2)
        except Exception as e:
            print("cron_save error:", t, e)

    if rows:
        supabase_admin.table("rsi_history").upsert(
            rows,
            on_conflict="ticker,day"
        ).execute()

    return {"saved": [r["ticker"] for r in rows], "day": today}


# =====================
# Cron 실행 (장 시작 시)
# =====================
@app.post("/cron/execute-orders")
def cron_execute_orders(secret: str = Query(...)):
    if secret != os.getenv("CRON_SECRET"):
        raise HTTPException(403)

    res = (
        supabase_admin
        .table("queued_orders")
        .select("*")
        .eq("status", "PENDING")
        .lte("execute_after", datetime.utcnow().isoformat())
        .execute()
    )

    for o in res.data or []:
        try:
            # 실제 주문 실행 로직
            ...
            supabase_admin.table("queued_orders").update({
                "status": "DONE",
                "executed_at": datetime.utcnow().isoformat()
            }).eq("id", o["id"]).execute()
        except Exception as e:
            supabase_admin.table("queued_orders").update({
                "status": "ERROR",
                "error": str(e)
            }).eq("id", o["id"]).execute()

    return {"status": "ok"}


# =====================
# Queued Orders (사용자 / RLS 적용)
# =====================
@app.get("/api/queued-orders")
def get_queued_orders(
    request: Request,
    user: str = Depends(get_current_user)
):
    # ✅ 헤더 → 쿠키 fallback
    token = request.headers.get("authorization")
    if token and token.startswith("Bearer "):
        token = token.replace("Bearer ", "")
    else:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(401, "No token")

    sb = get_user_supabase(token)  # ✅ RLS 적용 client

    res = (
        sb
        .table("queued_orders")
        .select("id, ticker, side, price, qty, execute_after")
        .eq("status", "PENDING")
        .eq("user_id", user)
        .order("execute_after", desc=False)
        .execute()
    )

    KST = timezone(timedelta(hours=9))
    orders = []

    for r in res.data or []:
        dt = datetime.fromisoformat(r["execute_after"].replace("Z", "+00:00"))
        orders.append({
            "id": r["id"],
            "ticker": r["ticker"],
            "side": r["side"],
            "price": float(r["price"]),
            "qty": int(r["qty"]),
            "execute_after": dt.astimezone(KST).strftime("%Y-%m-%d %H:%M (KST)")
        })

    return {"orders": orders}

@app.delete("/api/queued-orders/{order_id}")
def delete_queued_order(
    order_id: str,
    request: Request,
    user: str = Depends(get_current_user)
):
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    sb = get_user_supabase(token)   # ✅ 사용자 client

    res = (
        sb
        .table("queued_orders")
        .delete()
        .eq("id", order_id)
        .execute()
    )

    if not res.data:
        raise HTTPException(404, "order not found")

    return {"deleted": order_id}

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

# =====================
# Watchlist
# =====================
@app.get("/tickers")
def get_tickers():
    return {"tickers": WATCHLIST}


@app.post("/tickers")
def add_ticker(ticker: str = Query(...)):
    t = ticker.upper()
    if t in WATCHLIST:
        raise HTTPException(400, "Already exists")
    WATCHLIST.append(t)
    save_watchlist(WATCHLIST)
    return {"added": t}


@app.delete("/tickers/{ticker}")
def delete_ticker(ticker: str):
    t = ticker.upper()
    if t not in WATCHLIST:
        raise HTTPException(404, "Not found")
    WATCHLIST.remove(t)
    save_watchlist(WATCHLIST)
    return {"removed": t}

    
@app.get("/watchlist")
def watchlist():
    is_open = is_us_market_open()
    next_open = next_market_open()

    result = []
    for t in WATCHLIST:
        result.append(get_watchlist_item(t))

    # ✅ RSI 오름차순 정렬 (낮은 RSI → 높은 RSI)
    result.sort(key=lambda x: x["rsi"] if x["rsi"] is not None else 999)

    return {
        "market_open": is_open,
        "next_open": next_open.isoformat() if next_open else None,
        "items": result
    }
    
@app.get("/api/avg-price/{ticker}")
def avg_price(ticker: str):
    result = get_overseas_avg_price(ticker.upper())
    return result

# =====================
# 프론트
# =====================

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    user = require_login_page(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    return templates.TemplateResponse(
        "index.html",
        {"request": request}
    )
        
from fastapi.responses import JSONResponse

@app.get("/chart/{ticker}")
def chart_data(ticker: str, user=Depends(get_current_user)):
    df = yf.download(
        ticker,
        period="2y",
        interval="1d",
        progress=False,
        threads=False
    )
    if df is None or df.empty:
        raise HTTPException(400, "no data")

    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.astype(float)

    rsi_series = calculate_wilder_rsi_series(close)
    history = [
        {
            "date": close.index[i].strftime("%Y-%m-%d"),
            "price": round(float(close.iloc[i]), 2),
            "rsi": round(float(rsi_series.iloc[i]), 2)
            if not pd.isna(rsi_series.iloc[i]) else None
        }
        for i in range(len(close))
    ]

    # 🔥 가격 계산 (watchlist와 동일)
    p = resolve_prices(ticker)

    return {
        "ticker": ticker,
        "history": history,

        # 🔥 기준 현재가
        "current_price": p["base_price"],
        "current_change": p["current_change"],
        "current_change_pct": p["current_change_pct"],

        # 🔥 시간외
        "display_price": p["display_price"],
        "after_change": p["after_change"],
        "after_change_pct": p["after_change_pct"],

        # 🔥 뱃지
        "price_source": p["price_source"],
    }

@app.get("/chart-page", response_class=HTMLResponse)
def chart_page(request: Request):
    return templates.TemplateResponse("chart.html", {"request": request})
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
