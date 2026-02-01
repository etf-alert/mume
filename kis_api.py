import requests
import os
import time

# ===== KIS 모의투자 서버 =====
BASE_URL = "https://openapivts.koreainvestment.com:29443"

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")  # 예: 12345678-01

CANO, ACNT = ACCOUNT_NO.split("-")

_token_cache = {
    "access_token": None,
    "expire_at": 0
}

# ===== 토큰 =====
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

# ===== 해외주식 주문 (모의투자) =====
def order_overseas_stock(
    ticker: str,
    price: float,
    qty: int,
    side: str   # "buy" | "sell"
):
    token = get_access_token()

    tr_id = "VTTC0802U" if side == "buy" else "VTTC0801U"

    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P"
    }

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT,
        "OVRS_EXCG_CD": "NASD",        # NASDAQ
        "PDNO": ticker,
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": f"{price:.2f}",
        "ORD_SVR_DVSN_CD": "0"
    }

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    res = requests.post(url, headers=headers, json=body)
    res.raise_for_status()
    return res.json()
