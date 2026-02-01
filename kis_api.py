import requests
import os
import time

BASE_URL = "https://openapi.koreainvestment.com:9443"

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")  # 예: 12345678-01

_token_cache = {
    "access_token": None,
    "expire_at": 0
}

def get_access_token():
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expire_at"]:
        return _token_cache["access_token"]

    url = f"{BASE_URL}/oauth2/tokenP"
    data = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, json=data)
    res.raise_for_status()
    j = res.json()

    _token_cache["access_token"] = j["access_token"]
    _token_cache["expire_at"] = now + j["expires_in"] - 60
    return j["access_token"]

def order_stock(
    ticker: str,
    price: float,
    qty: int,
    side: str  # "buy" or "sell"
):
    token = get_access_token()

    tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"

    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
        "tr_id": tr_id
    }

    body = {
        "CANO": ACCOUNT_NO.split("-")[0],
        "ACNT_PRDT_CD": ACCOUNT_NO.split("-")[1],
        "PDNO": ticker,
        "ORD_DVSN": "00",          # 지정가
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(int(price))
    }

    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    res = requests.post(url, headers=headers, json=body)
    res.raise_for_status()
    return res.json()
