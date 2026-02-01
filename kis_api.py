# kis_api.py
import requests
import os
import time

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
        "OVRS_EXCG_CD": "NASD",
        "TR_CRCY_CD": "USD"
    }

    res = requests.get(url, headers=headers, params=params)
    res.raise_for_status()
    data = res.json()

    for item in data.get("output1", []):
        if item["ovrs_pdno"] == ticker:
            return {
                "avg_price": float(item["pchs_avg_pric"]),
                "qty": int(float(item["hldg_qty"]))
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
    assert side in ("buy", "sell")
    token = get_access_token()

    # 🔥 주문 방식 결정
    if side == "buy":
        tr_id = "VTTC0802U"
        ord_dvsn = "03"     # LOC
    else:
        tr_id = "VTTC0801U"
        ord_dvsn = "00"     # 지정가

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P"
    }

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT,
        "OVRS_EXCG_CD": "NASD",
        "PDNO": ticker,
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": str(round(price, 2)),
        "ORD_SVR_DVSN_CD": "0"
    }

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    res = requests.post(url, headers=headers, json=body)
    res.raise_for_status()
    return res.json()
