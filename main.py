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
import pytz
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
import pandas_market_calendars as mcal

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

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise RuntimeError("Alpaca API key not set")

alpaca_data = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

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

nyse = mcal.get_calendar("NYSE")
ny_tz = pytz.timezone("US/Eastern")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    requests.post(url, json=payload, timeout=5)

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

def get_next_n_trading_days(start_date, n):
    schedule = nyse.schedule(
        start_date=start_date,
        end_date=start_date + timedelta(days=n * 2)
    )
    return list(schedule.index[:n])

def calculate_execute_at_from_market_open(
    execute_after_minutes: int,
    base_date: date | None = None
):
    if base_date:
        market_open = next_market_open(base_date)
    else:
        market_open = next_market_open()

    if market_open is None:
        raise ValueError("다음 정규장 시작 시간을 찾을 수 없습니다")

    return market_open + timedelta(minutes=execute_after_minutes)

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

# =====================
# 🔥 예약 주문 목록 조회 (index용)
# =====================
@app.get("/api/queued-orders")
def list_queued_orders(user: str = Depends(get_current_user)):
    res = (
        supabase_admin
        .table("queued_orders")
        .select("""
            id,
            ticker,
            side,
            execute_after,
            status,
            repeat_group,
            repeat_index
        """)
        .eq("user_id", user)
        .eq("status", "PENDING")   # 🔥 실행 전 것만
        .order("execute_after")
        .execute()
    )

    rows = res.data or []

    # 🔥 repeat_group별 전체 개수 계산
    group_totals: dict[str, int] = {}
    for r in rows:
        g = r["repeat_group"]
        if g:
            group_totals[g] = group_totals.get(g, 0) + 1

    result = []
    for r in rows:
        total = group_totals.get(r["repeat_group"], 1)

        result.append({
            **r,

            # 🔥 이것만 쓴다. 다른 개념 없음.
            # 예: "1/40", "2/40"
            "repeat_label": (
                f'{r["repeat_index"]}/{total}'
                if total > 1 else None
            )
        })

    return result

# =====================
# 🔥 repeat_group 전체 개수 계산 (공통)
# =====================
def get_repeat_total(db, repeat_group: str) -> int:
    if not repeat_group:
        return 1
    res = (
        db
        .table("queued_orders")
        .select("id", count="exact")
        .eq("repeat_group", repeat_group)
        .neq("status", "ERROR")   # 🔧 FIX
        .execute()
    )
    return res.count or 1


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

# =====================
# 🔧 FIX: order_preview 로직 분리 (순수 함수)
# =====================
# =====================
# 🔧 FIX: 순수 가격/수량 계산 함수 (API / Cron 공용)
# =====================
def build_order_preview(data: dict):
    side = data["side"]
    avg = float(data["avg_price"])
    cur = float(data["current_price"])
    seed = float(data["seed"])
    ticker = data["ticker"]

    price_type = None
    message = None

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
            raise ValueError("매도 가능 수량 없음")

        target = round(avg * 1.10, 2)
        if cur > target:
            price = round(cur, 2)
            price_type = "MARKET_BETTER"
            message = "현재가로 매도"
        else:
            price = target
            price_type = "TARGET"
            message = "목표가 매도"

    else:
        raise ValueError("invalid side")

    if qty <= 0:
        raise ValueError("수량 0")

    return {
        "price": price,
        "qty": qty,
        "price_type": price_type,
        "message": message
    }


@app.post("/api/order/preview")
def order_preview(
    data: dict,
    user: str = Depends(get_current_user)
):
    cleanup_order_cache()

    try:
        preview = build_order_preview(data)  # 🔧 FIX
        order_id = str(uuid4())

        ORDER_CACHE[order_id] = {
            **preview,
            "side": data["side"],
            "ticker": data["ticker"],
            "created_at": datetime.now(UTC)
        }

        return {"order_id": order_id, **preview}

    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/order/execute/{order_id}")
def execute_order(order_id: str, user: str = Depends(get_current_user)):
    order = ORDER_CACHE.get(order_id)
    if not order:
        raise HTTPException(404, "order not found")

    if not is_us_market_open():
        raise HTTPException(
            400,
            "정규장에만 즉시 주문 가능합니다. 예약 주문을 사용하세요."
        )

    # 🔁 매도 수량 재검증
    if order["side"] == "SELL":
        pos = get_overseas_avg_price(order["ticker"])
        if order["qty"] > pos["qty"]:
            raise HTTPException(400, "보유 수량 부족")

    side = "buy" if order["side"].startswith("BUY") else "sell"

    result = order_overseas_stock(
        ticker=order["ticker"],
        price=order["price"],
        qty=order["qty"],
        side=side
    )

    ORDER_CACHE.pop(order_id, None)
    return {"status": "ok", "result": result}
    
# =====================
# 🔥 예약 주문 목록 조회
# =====================
@app.post("/api/order/reserve")
async def reserve_order(
    request: Request,
    user: str = Depends(get_current_user)
):
    body = await request.json()
    seed = body.get("seed")
    if seed is None:
        raise HTTPException(400, "seed is required")

    order_id = body["order_id"]
    minutes = int(body["execute_after_minutes"])
    repeat_days = int(body.get("repeat_days", 1))

    order = ORDER_CACHE.get(order_id)
    if not order:
        raise HTTPException(404, "order not found")

    if minutes < 0 or minutes > 60 * 6:
        raise HTTPException(400, "예약 시간은 0~360분만 가능")

    if repeat_days < 1 or repeat_days > 120:
        raise HTTPException(400, "repeat_days 범위 오류")

    # =========================
    # 🟢 NEW: 영업일 계산
    # =========================
    start_date = datetime.now(ny_tz).date()
    trading_days = get_next_n_trading_days(start_date, repeat_days)

    repeat_group = str(uuid4())  # 🟢 NEW
    rows = []

    for idx, day in enumerate(trading_days, start=1):
        execute_at = calculate_execute_at_from_market_open(
            minutes,
            base_date=day   # 🔧 CHANGED
        )

        if execute_at <= datetime.now(timezone.utc):
            continue

        rows.append({
            "user_id": user,
            "ticker": order["ticker"],
            "side": order["side"],

            # 🔧 실행 시점 계산용 데이터만 저장
            "seed": body["seed"],
            "avg_price": body["avg_price"],

            "execute_after": execute_at.astimezone(timezone.utc).isoformat(),
            "status": "PENDING",

            # 🟢 반복 주문 식별
            "repeat_group": repeat_group,
            "repeat_index": idx
        })

    if not rows:
        raise HTTPException(400, "유효한 예약 날짜 없음")

    # =========================
    # 🟢 NEW: 다건 insert
    # =========================
    try:
        supabase_admin.table("queued_orders").insert(rows).execute()
    except Exception as e:
        raise HTTPException(500, f"예약 저장 실패: {e}")

    ORDER_CACHE.pop(order_id, None)

    return {
        "status": "reserved",
        "repeat_days": len(rows),
        "first_execute_at": rows[0]["execute_after"],
        "repeat_group": repeat_group
    }

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
    series = series.dropna()

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1/period,
        adjust=False,
        min_periods=period   # 🔥 중요
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/period,
        adjust=False,
        min_periods=period   # 🔥 중요
    ).mean()

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
    rsi_history 테이블 기준 (전일 대비 계산용)
    """
    res = (
        supabase_admin
        .table("rsi_history")
        .select("day, rsi")
        .eq("ticker", ticker)
        .order("day", desc=True)
        .limit(1)
        .execute()
    )

    rows = res.data or []
    if not rows:
        return None

    return float(rows[0]["rsi"])


def get_watchlist_item(ticker: str):
    # =====================
    # 가격
    # =====================
    p = resolve_prices(ticker)

    # =====================
    # 🔥 Finviz 실시간 RSI
    # =====================
    try:
        realtime_rsi, _ = get_finviz_rsi(ticker)
        realtime_rsi = round(float(realtime_rsi), 2)
    except Exception as e:
        print("Finviz RSI error:", ticker, e)
        realtime_rsi = None

    # =====================
    # 📉 전일 RSI (DB)
    # =====================
    prev_rsi = get_rsi_from_history(ticker)

    if realtime_rsi is not None and prev_rsi is not None:
        rsi_change = round(realtime_rsi - prev_rsi, 2)
        rsi_change_pct = round(
            (rsi_change / prev_rsi) * 100, 2
        ) if prev_rsi != 0 else 0.0
    else:
        rsi_change = None
        rsi_change_pct = None

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

        # 📊 RSI (Finviz 실시간)
        "rsi": realtime_rsi,
        "rsi_change": rsi_change,
        "rsi_change_pct": rsi_change_pct,
    }

    print("WATCHLIST ITEM DEBUG:", item)
    return item

def cleanup_order_cache():
    now = datetime.now(UTC)
    expired = [
        k for k, v in ORDER_CACHE.items()
        if now - v["created_at"] > timedelta(minutes=10)
    ]
    for k in expired:
        ORDER_CACHE.pop(k, None)


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
@app.post("/cron/execute-reservations")
def cron_execute_reservations(secret: str = Query(...)):
    if secret != os.getenv("CRON_SECRET"):
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
            if not is_us_market_open():
                continue

            # 🔧 현재가 계산
            current_price = resolve_prices(o["ticker"])["base_price"]

            # 🔧 순수 함수 사용
            preview = build_order_preview({
                "side": o["side"],
                "avg_price": o["avg_price"],
                "current_price": current_price,
                "seed": o["seed"],
                "ticker": o["ticker"]
            })

            side = "buy" if o["side"].startswith("BUY") else "sell"

            kis_res = order_overseas_stock(
                ticker=o["ticker"],
                price=preview["price"],
                qty=preview["qty"],
                side=side
            )

            # 🔥 ADDED: KIS 실패 강제 예외 처리
            if not kis_res or kis_res.get("rt_cd") != "0":
                raise RuntimeError(
                    f"[KIS] {kis_res.get('msg_cd')} - {kis_res.get('msg1')}"
                )

            # 주문 완료 처리
            supabase_admin.table("queued_orders").update({
                "status": "DONE",
                "executed_at": now.isoformat()
            }).eq("id", o["id"]).execute()

            # 🔥 ADDED: KIS 응답 메시지 텔레그램 전달
            send_order_success_telegram(
                order=o,
                executed_price=preview["price"],
                executed_qty=preview["qty"],
                executed_at=now,
                kis_msg=kis_res.get("msg1"),
                db=supabase_admin
            )

        except Exception as e:
            supabase_admin.table("queued_orders").update({
                "status": "ERROR",
                "error": str(e)
            }).eq("id", o["id"]).execute()

            send_order_fail_telegram(
                order=o,
                error_msg=str(e),
                db=supabase_admin
            )

    done_limit = 3000

    done_res = (
        supabase_admin
        .table("queued_orders")
        .select("id")
        .eq("status", "DONE")
        .order("created_at", desc=True)
        .execute()
    )

    done_rows = done_res.data or []

    if len(done_rows) > done_limit:
        delete_ids = [r["id"] for r in done_rows[done_limit:]]
        supabase_admin.table("queued_orders").delete().in_("id", delete_ids).execute()


    # ===============================
    # 🧹 ERROR 최근 500개만 유지
    # ===============================
    error_limit = 500

    error_res = (
        supabase_admin
        .table("queued_orders")
        .select("id")
        .eq("status", "ERROR")
        .order("created_at", desc=True)
        .execute()
    )

    error_rows = error_res.data or []

    if len(error_rows) > error_limit:
        delete_ids = [r["id"] for r in error_rows[error_limit:]]
        supabase_admin.table("queued_orders").delete().in_("id", delete_ids).execute()
        
    return {"status": "ok"}

# =====================
# 🔥 예약 주문 삭제 API
# =====================
@app.delete("/api/order/reserve/{order_id}")
def delete_reserved_order(
    order_id: str,
    user: str = Depends(get_current_user)
):
    res = (
        supabase_admin
        .table("queued_orders")          # 🔧 CHANGED
        .delete()
        .eq("id", order_id)
        .eq("user_id", user)
        .eq("status", "PENDING")         # 실행 전만 삭제 가능
        .execute()
    )

    if not res.data:
        raise HTTPException(404, "예약 주문 없음 또는 삭제 불가")

    return {"deleted": order_id}


# =====================
# 🔥 예약 주문 1건 삭제
# =====================
@app.delete("/api/queued-orders/{order_id}")
def delete_queued_order(
    order_id: str,
    user: str = Depends(get_current_user)
):
    res = (
        supabase_admin
        .table("queued_orders")
        .delete()
        .eq("id", order_id)
        .eq("user_id", user)
        .eq("status", "PENDING")  # 🔒 실행 전만 삭제
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "삭제 불가")
    return {"deleted": order_id}

# =====================
# 🔥 repeat_group 전체 삭제
# =====================
@app.delete("/api/queued-orders/group/{group_id}")
def delete_repeat_group(
    group_id: str,
    user: str = Depends(get_current_user)
):
    res = (
        supabase_admin
        .table("queued_orders")
        .delete()
        .eq("repeat_group", group_id)
        .eq("user_id", user)
        .eq("status", "PENDING")
        .execute()
    )
    return {
        "deleted_count": len(res.data or [])
    }

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

    # =========================
    # 1️⃣ 컬럼 구조 정규화
    # =========================
    if isinstance(df.columns, pd.MultiIndex):
        # 보통 (price_type, ticker) 구조
        # → price_type만 사용
        df = df.copy()
        df.columns = df.columns.get_level_values(0)

    # =========================
    # 2️⃣ 종가 컬럼 명시적 선택
    # =========================
    if "Adj Close" in df.columns:
        close = df["Adj Close"]
    elif "Close" in df.columns:
        close = df["Close"]
    else:
        raise HTTPException(
            500,
            f"no close column: {df.columns.tolist()}"
        )

    # =========================
    # 3️⃣ Series 보장
    # =========================
    if isinstance(close, pd.DataFrame):
        if close.shape[1] != 1:
            raise HTTPException(
                500,
                f"ambiguous close columns: {close.columns.tolist()}"
            )
        close = close.iloc[:, 0]

    # =========================
    # 4️⃣ 타입 / 결측 정리
    # =========================
    close = pd.to_numeric(close, errors="coerce").dropna()

    # 🔥 2️⃣ RSI 계산
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
    
# =====================
# 🔧 FIX: executed_at 명시적으로 받기
# =====================
def send_order_success_telegram(
    order: dict,
    executed_price: float,
    executed_qty: int,
    executed_at: datetime,
    db,
    kis_msg: str | None = None,
):
    total = get_repeat_total(db, order["repeat_group"])
    executed_at_str = executed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    message = (
        "✅ 예약 주문 체결\n\n"
        f"종목: {order['ticker']}\n"
        f"구분: {order['side']}\n"
        f"체결가: ${executed_price}\n"
        f"수량: {executed_qty} 주\n"
        f"매수액: ${executed_price * executed_qty:,.2f}\n\n"
        f"진행률: {order['repeat_index']}/{total}\n"
        f"실행 시각: {executed_at_str}"
    )
    if kis_msg:
        message += f"\nKIS: {kis_msg}"
        
    send_telegram_message(message)

def send_order_fail_telegram(order: dict, error_msg: str, db):
    total = get_repeat_total(db, order["repeat_group"])

    # 🔥 execute_after 안전 처리
    execute_after = order.get("execute_after")
    if execute_after:
        execute_after = datetime.fromisoformat(execute_after)
        execute_after_str = execute_after.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    else:
        execute_after_str = "N/A"

    message = (
        "❌ 예약 주문 실패\n\n"
        f"종목: {order['ticker']}\n"
        f"구분: {order['side']}\n"
        f"사유: {error_msg}\n\n"
        f"진행률: {order['repeat_index']}/{total}\n"
        f"실행 예정 시각: {execute_after_str}"
    )

    send_telegram_message(message)

@app.get("/chart-page", response_class=HTMLResponse)
def chart_page(request: Request):
    return templates.TemplateResponse("chart.html", {"request": request})
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
