import sqlite3
from datetime import date, datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import requests
from pydantic import BaseModel
from bs4 import BeautifulSoup
import json
import os
import yfinance as yf
import pandas as pd
from kis_api import order_overseas_stock, get_overseas_avg_price
from uuid import uuid4
from market_time import is_us_market_open, next_market_open

SECRET_KEY = os.getenv("JWT_SECRET", "change-this")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

if SECRET_KEY == "change-this":
    raise RuntimeError("JWT_SECRET not set")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

def require_login_page(request: Request):
    token = request.cookies.get("access_token")

    if not token:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


app = FastAPI()
ORDER_CACHE = {}

from fastapi.responses import JSONResponse

@app.post("/api/auth/login")
def login(data: dict):
    user_id = data["id"]
    password = data["password"]

    if user_id != os.getenv("ADMIN_ID") or password != os.getenv("ADMIN_PW"):
        raise HTTPException(401, "invalid credentials")

    token = create_access_token({"sub": user_id})

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
    
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]
    except JWTError:
        raise HTTPException(401, "invalid token")
        
def get_realtime_price(ticker: str):
    """
    정규장: KIS
    장외: yfinance
    """
    regular = None
    pre = None
    post = None

    # ======================
    # 1️⃣ 정규장 → KIS
    # ======================
    if is_us_market_open():
        try:
            kis = get_kis_overseas_price(ticker)
            regular = float(kis["price"])
        except Exception as e:
            print("KIS price error:", e)

    # ======================
    # 2️⃣ 장외 → yfinance
    # ======================
    try:
        yf_ticker = yf.Ticker(ticker)
        info = yf_ticker.fast_info
        if info:
            pre_val = info.get("preMarketPrice")
            post_val = info.get("postMarketPrice")
            pre = float(pre_val) if pre_val is not None else None
            post = float(post_val) if post_val is not None else None
    except Exception as e:
        print("yfinance pre/post error:", e)

    return {
        "regular": regular,
        "pre": pre,
        "post": post
    }

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
            "created_at": datetime.utcnow()
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
    if datetime.utcnow() - order["created_at"] > timedelta(minutes=5):
        ORDER_CACHE.pop(order_id, None)
        raise HTTPException(400, "order expired")

    # 🔁 매도 수량 재검증
    if order["side"] == "SELL":
        pos = get_overseas_avg_price(order["ticker"])
        if order["qty"] > pos["qty"]:
            raise HTTPException(400, "보유 수량 부족")

    # ✅ 장 상태 확인
    is_open = is_us_market_open()
    next_open = next_market_open()

    # ==========================
    # 🌙 장전 / 시간외 → 주문 큐잉
    # ==========================
    if not is_open:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO queued_orders
                (id, ticker, side, price, qty, created_at, execute_after, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                order_id,
                order["ticker"],
                order["side"],
                order["price"],
                order["qty"],
                datetime.utcnow().isoformat(),   # UTC 저장
                next_open.isoformat()            # ET 기준 저장
            ))
            conn.commit()
        finally:
            conn.close()

        ORDER_CACHE.pop(order_id, None)

        # ✅ KST 변환 (응답용)
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
# DB 설정
# =====================
DB_FILE = "rsi_history.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS queued_orders (
        id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        side TEXT NOT NULL,
        price REAL NOT NULL,
        qty INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        execute_after TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
    )
    """)

    conn.commit()
    conn.close()

init_db()

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
def get_watchlist_item(ticker: str):
    df = yf.download(
        ticker,
        period="2y",
        interval="1d",
        progress=False,
        threads=False
    )
    if df is None or df.empty:
        raise ValueError("Empty DataFrame")

    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.astype(float)

    close_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])

    realtime = get_realtime_price(ticker)
    market_open = is_us_market_open()

    # ==================================================
    # 1️⃣ 기준가 (정규장 현재가 or 종가)
    # ==================================================
    if realtime["regular"] is not None:
        base_price = realtime["regular"]
    else:
        base_price = close_price

    # ==================================================
    # 2️⃣ 표시 가격 (시간외 무조건 우선)
    # ==================================================
    display_price = base_price
    price_source = "REGULAR" if realtime["regular"] is not None else "CLOSE"

    if not market_open:
        if realtime["pre"] is not None:
            display_price = realtime["pre"]
            price_source = "PRE"
        elif realtime["post"] is not None:
            display_price = realtime["post"]
            price_source = "POST"

    # ==================================================
    # 3️⃣ 현재가 증감 (전일 종가 대비)
    # ==================================================
    current_change = base_price - prev_close
    current_change_pct = (current_change / prev_close) * 100

    # ==================================================
    # 4️⃣ 시간외 증감 (현재가 대비)
    # ==================================================
    after_change = None
    after_change_pct = None
    if price_source in ("PRE", "POST"):
        after_change = display_price - base_price
        after_change_pct = (after_change / base_price) * 100

    # ==================================================
    # 5️⃣ RSI
    # ==================================================
    rsi_series = calculate_wilder_rsi_series(close)
    rsi_today = float(rsi_series.iloc[-1])
    rsi_prev = float(rsi_series.iloc[-2])
    rsi_change = rsi_today - rsi_prev
    rsi_change_pct = (rsi_change / rsi_prev * 100) if rsi_prev != 0 else 0.0

    return {
        "ticker": ticker,

        # 🔥 기준 현재가
        "current_price": round(base_price, 2),
        "current_change": round(current_change, 2),
        "current_change_pct": round(current_change_pct, 2),

        # 🔥 시간외 (있을 때만 의미 있음)
        "display_price": round(display_price, 2),
        "after_change": round(after_change, 2) if after_change is not None else None,
        "after_change_pct": round(after_change_pct, 2) if after_change_pct is not None else None,

        # 🔥 이 값으로 뱃지 결정
        "price_source": price_source,

        # 종가 정보
        "close_price": round(close_price, 2),
        "close_change": round(close_price - prev_close, 2),
        "close_change_pct": round(
            ((close_price - prev_close) / prev_close) * 100, 2
        ),

        # RSI
        "rsi": round(rsi_today, 2),
        "rsi_change": round(rsi_change, 2),
        "rsi_change_pct": round(rsi_change_pct, 2),
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
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request}
    )

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = require_login_page(request)
    if user:
        return RedirectResponse("/app", status_code=302)

    return templates.TemplateResponse(
        "login.html",
        {"request": request}
    )
    
@app.get("/api/queued-orders")
def get_queued_orders(user: str = Depends(get_current_user)):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT id, ticker, side, price, qty, execute_after
        FROM queued_orders
        WHERE status = 'PENDING'
        ORDER BY execute_after ASC
    """)
    rows = cur.fetchall()
    conn.close()

    KST = timezone(timedelta(hours=9))
    orders = []

    for r in rows:
        # DB에는 ET/UTC ISO 문자열 → datetime 변환
        execute_dt = datetime.fromisoformat(r["execute_after"])

        # KST 변환
        execute_kst = execute_dt.astimezone(KST)

        orders.append({
            "id": r["id"],
            "ticker": r["ticker"],
            "side": r["side"],
            "price": r["price"],
            "qty": r["qty"],
            "execute_after": execute_kst.strftime("%Y-%m-%d %H:%M (KST)")
        })

    return {"orders": orders}

@app.delete("/api/queued-orders/{order_id}")
def delete_queued_order(
    order_id: str,
    user: str = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM queued_orders WHERE id = ?",
        (order_id,)
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted == 0:
        raise HTTPException(404, "order not found")

    return {"deleted": order_id}

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
    is_open = is_us_market_open()
    next_open = next_market_open()

    result = []
    for t in WATCHLIST:
        result.append(get_watchlist_item(t))

    # ✅ RSI 오름차순 정렬 (낮은 RSI → 높은 RSI)
    result.sort(key=lambda x: x["rsi"])

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

    # ===== 히스토리 =====
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

    close_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])

    realtime = get_realtime_price(ticker)
    market_open = is_us_market_open()

    # ==================================================
    # 1️⃣ 기준 현재가 (전일 종가 대비 계산용)
    # ==================================================
    if realtime["regular"] is not None:
        base_price = float(realtime["regular"])
    else:
        base_price = close_price

    # ==================================================
    # 2️⃣ 표시 가격 (시간외 무조건 우선)
    # ==================================================
    display_price = base_price
    price_source = "REGULAR" if realtime["regular"] is not None else "CLOSE"

    if not market_open:
        if realtime["pre"] is not None:
            display_price = float(realtime["pre"])
            price_source = "PRE"
        elif realtime["post"] is not None:
            display_price = float(realtime["post"])
            price_source = "POST"

    # ==================================================
    # 3️⃣ 현재가 증감 (전일 종가 대비)
    # ==================================================
    current_change = base_price - prev_close
    current_change_pct = (current_change / prev_close) * 100

    # ==================================================
    # 4️⃣ 시간외 증감 (현재가 대비)
    # ==================================================
    after_change = None
    after_change_pct = None
    if price_source in ("PRE", "POST"):
        after_change = display_price - base_price
        after_change_pct = (after_change / base_price) * 100

    return {
        "ticker": ticker,
        "history": history,

        # 🔥 현재가 (전일 종가 대비)
        "current_price": round(base_price, 2),
        "current_change": round(current_change, 2),
        "current_change_pct": round(current_change_pct, 2),

        # 🔥 시간외 (현재가 대비)
        "after_change": round(after_change, 2) if after_change is not None else None,
        "after_change_pct": round(after_change_pct, 2) if after_change_pct is not None else None,

        # 🔥 표시용
        "display_price": round(display_price, 2),
        "price_source": price_source,
    }

@app.get("/chart-page", response_class=HTMLResponse)
def chart_page(request: Request):
    return templates.TemplateResponse("chart.html", {"request": request})
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
