# kis_api.py
import requests
import os
import time
import yfinance as yf

BASE_URL = "https://openapivts.koreainvestment.com:29443"

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")  # 12345678-01

if not ACCOUNT_NO or "-" not in ACCOUNT_NO:
    raise RuntimeError("KIS_ACCOUNT_NO must be like '12345678-01'")

CANO, ACNT = ACCOUNT_NO.split("-")

_token_cache = {
    "access_token": None,
    "expire_at": 0
}
# =====================
# 거래소 판별
# =====================
def get_kis_exchange_code(ticker: str) -> str:
    """
    티커 기준으로 KIS 해외거래소 코드 자동 판별
    """
    info = yf.Ticker(ticker).info
    exchange = info.get("exchange", "")

    if exchange in ("NMS", "NASDAQ"):
        return "NASD"
    elif exchange in ("NYQ", "NYSE"):
        return "NYSE"
    elif exchange in ("ASE", "AMEX"):
        return "AMEX"
    else:
        # fallback (대부분 NASDAQ)
        return "NASD"
        
# =====================
# Access Token
# =====================
def get_access_token():
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expire_at"]:
        return _token_cache["access_token"]

    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }

    res = requests.post(url, json=body)
    res.raise_for_status()
    j = res.json()

    _token_cache["access_token"] = j["access_token"]
    _token_cache["expire_at"] = now + j["expires_in"] - 60
    return j["access_token"]
    
# =====================
# 해외주식 평단가 조회
# =====================
def get_overseas_avg_price(ticker: str):
    token = get_access_token()

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTS3012R",
        "custtype": "P"
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT,
        "TR_CRCY_CD": "USD"
    }

    res = requests.get(url, headers=headers, params=params)
    res.raise_for_status()
    data = res.json()

    for item in data.get("output1", []):
        if item.get("ovrs_pdno") == ticker.upper():
          avg = item.get("pchs_avg_pric")
          qty = item.get("hldg_qty")
          if not avg or not qty:
            return None
          return {
            "avg_price": float(avg),
            "qty": int(float(qty)),
            "excg": item.get("ovrs_excg_cd")  # ⭐ 중요
        }
    return None
    
# =====================
# 해외주식 주문
# =====================
def order_overseas_stock(
    ticker: str,
    price: float,
    qty: int,
    side: str   # "buy" | "sell"
):
    token = get_access_token()
    CANO, ACNT = ACCOUNT_NO.split("-")

    is_buy = side == "buy"
    excg_cd = get_kis_exchange_code(ticker)

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC0802U" if is_buy else "VTTC0801U",
        "custtype": "P",
        "Content-Type": "application/json"
    }

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT,
        "OVRS_EXCG_CD": excg_cd,
        "PDNO": ticker,
        "ORD_QTY": str(qty),

        # 🔥 주문 방식
        "ORD_DVSN_CD": "31" if is_buy else "00",  # 매수=LOC / 매도=지정가

        # 🔥 해외주식 가격 필드
        "OVRS_ORD_UNPR": f"{price:.2f}",

        # 기본값
        "ORD_SVR_DVSN_CD": "0"
    }

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"

    res = requests.post(url, headers=headers, json=body)

    print("===== KIS ORDER DEBUG =====")
    print("STATUS:", res.status_code)
    print("URL:", url)
    print("HEADERS:", headers)
    print("BODY:", body)

    try:
        print("RESPONSE JSON:", res.json())
    except Exception:
        print("RESPONSE TEXT:", res.text)

    print("==========================")

    # 여기서 다시 에러 발생시킴
    res.raise_for_status()
    return res.json()
