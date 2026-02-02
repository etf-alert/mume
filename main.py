import sqlite3
from datetime import date, datetime, timedelta
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
    expire = datetime.utcnow() + timedelta(minutes=30)
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
    fi = yf.Ticker(ticker).fast_info
    return {
        "regular": fi.get("last_price"),
        "pre": fi.get("pre_market_price"),
        "post": fi.get("post_market_price")
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

    # ✅ 장 상태 확인 (서머타임 + 휴장일 자동 반영)
    is_open = is_us_market_open()
    next_open = next_market_open()

    # ==========================
    # 🌙 장전 / 시간외 → 주문 큐잉
    # ==========================
    if not is_open:
        cur.execute("""
        INSERT INTO queued_orders
        (id, ticker, side, price, qty, created_at, execute_after)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id,
            order["ticker"],
            order["side"],
            order["price"],
            order["qty"],
            datetime.utcnow().isoformat(),
            next_open.isoformat()
        ))
        conn.commit()

        ORDER_CACHE.pop(order_id, None)

        return {
            "status": "queued",
            "message": "장 시작 후 자동 실행",
            "execute_after": next_open.strftime("%Y-%m-%d %H:%M (ET)")
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
        msg = "KIS 주문 오류"
        if hasattr(e, "response") and e.response is not None:
            try:
                msg = e.response.text
            except Exception:
                msg = str(e)
        else:
            msg = str(e)

        raise HTTPException(status_code=400, detail=msg)



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
# 장전 주문 큐 테이블
# =====================
cur.execute("""
CREATE TABLE IF NOT EXISTS queued_orders (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,           -- BUY / SELL
    price REAL NOT NULL,
    qty INTEGER NOT NULL,
    created_at TEXT NOT NULL
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
    df = yf.download(ticker,
                     period="2y",
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
    realtime = get_realtime_price(ticker)

    price = (
        realtime["pre"]
        or realtime["post"]
        or realtime["regular"]
        or float(close.iloc[-1])
    )

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
def chart_data(ticker: str):
    df = yf.download(ticker,
                     period="2y",
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
