# kis_api.py
import requests
import os
import time
import yfinance as yf

BASE_URL = "https://openapi.koreainvestment.com:9443"

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
_exchange_cache = {}

def get_kis_exchange_code(ticker: str) -> str:
    if ticker in _exchange_cache:
        return _exchange_cache[ticker]
    info = yf.Ticker(ticker).fast_info
    exchange = info.get("exchange", "")
    if exchange in ("NMS", "NASDAQ"):
        code = "NASD"
    elif exchange in ("NYQ", "NYSE"):
        code = "NYSE"
    elif exchange in ("ASE", "AMEX"):
        code = "AMEX"

    else:
        code = "NASD"  # 안전 fallback

    _exchange_cache[ticker] = code
    return code
     
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
# 🔥 공통 KIS 요청 함수 (자동 토큰 재발급 + 1회 재시도)
# =====================
def _kis_request(method, url, headers=None, params=None, json=None):
    token = get_access_token()

    if headers is None:
        headers = {}

    headers = {
        **headers,
        "authorization": f"Bearer {token}"
    }

    res = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json
    )

    # 🔥 401이면 토큰 만료 → 강제 재발급 후 1회 재시도
    if res.status_code == 401:
        print("🔥 KIS 토큰 만료 → 재발급 후 재시도")

        _token_cache["access_token"] = None
        token = get_access_token()

        headers["authorization"] = f"Bearer {token}"

        res = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json
        )

    res.raise_for_status()
    return res
    
# =====================
# 해외주식 평단가 조회
# =====================
def get_overseas_avg_price(ticker: str):
    excg_cd = get_kis_exchange_code(ticker)

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"

    headers = {
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3012R",
        "custtype": "P"
    }

    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT,
        "TR_CRCY_CD": "USD",
        "OVRS_EXCG_CD": excg_cd,
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": ""
    }

    # 🔥 네트워크 일시 오류 대비 재시도 1회
    for i in range(2):
        try:
            # 🔥 _kis_request 내부에서
            # 1) 토큰 자동 발급
            # 2) 401 발생 시 자동 재발급 후 재시도
            res = _kis_request(
                method="GET",
                url=url,
                headers=headers,
                params=params
            )

            data = res.json()
            print("KIS RAW:", data)

            break  # 🔥 성공 시 루프 탈출

        except Exception as e:
            print("KIS balance 조회 실패:", e)
            time.sleep(1)

    else:
        # 🔥 2회 모두 실패 시
        raise RuntimeError("KIS 잔고 조회 2회 실패")

    # ==============================
    # ✅ 종목별 보유 내역 파싱
    # ==============================

    items = data.get("output1") or []
    target = ticker.upper()

    for item in items:
        ovrs_pdno = item.get("ovrs_pdno", "").upper()
        qty = float(item.get("ovrs_cblc_qty", 0))
        sellable = float(item.get("ord_psbl_qty", 0))  # 🔥 실제 매도 가능 수량

        if qty <= 0:
            continue

        if ovrs_pdno == target:
            return {
                "found": True,
                "avg_price": float(item.get("pchs_avg_pric", 0)),
                "qty": int(qty),
                "sellable_qty": int(sellable),
                "total_cost": float(item.get("frcr_pchs_amt1", 0)),
                "excg": item.get("ovrs_excg_cd"),
            }

    # 🔥 해당 종목 미보유
    return {
        "found": False,
        "avg_price": 0,
        "qty": 0,
        "sellable_qty": 0,
        "total_cost": 0,
        "excg": None
    }

def get_overseas_buying_power(ticker="AAPL", price="1"):
    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount"

    headers = {
        # 🔥 authorization 제거 ( _kis_request 내부에서 자동 추가 )
        "Content-Type": "application/json; charset=utf-8",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3007R",   # 🔥 실계좌
        "custtype": "P"
    }

    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT,
        "OVRS_EXCG_CD": "NASD",     # 🔥 나스닥 기준
        "OVRS_ORD_UNPR": price,     # 🔥 외부에서 받은 price 사용
        "ITEM_CD": ticker           # 🔥 외부에서 받은 ticker 사용
    }

    # 🔥 네트워크 일시 오류 대비 재시도 1회
    for i in range(2):
        try:
            # 🔥 내부에서
            # 1) 토큰 자동 발급
            # 2) 401 발생 시 자동 재발급 후 재시도
            res = _kis_request(
                method="GET",
                url=url,
                headers=headers,
                params=params
            )

            data = res.json()
            break  # 🔥 성공 시 루프 탈출

        except Exception as e:
            print("KIS 매수 가능 금액 조회 실패:", e)
            time.sleep(1)

    else:
        # 🔥 2회 모두 실패
        raise RuntimeError("KIS 매수 가능 금액 조회 2회 실패")

    # ==============================
    # ✅ 응답 코드 확인
    # ==============================

    if data.get("rt_cd") != "0":
        print("❌ KIS 오류:", data)
        return 0.0

    output = data.get("output") or {}
    buying_power = float(output.get("ovrs_ord_psbl_amt", 0))

    return buying_power

# =====================
# 해외주식 주문
# =====================
def order_overseas_stock(
    ticker: str,
    price: float,
    qty: int,
    side: str   # "buy" | "sell"
):
    CANO, ACNT = ACCOUNT_NO.split("-")
    is_buy = side == "buy"

    # 🔥 거래소 코드 자동 판별
    excg_cd = get_kis_exchange_code(ticker)

    # 🔥 미국 실계좌 TR_ID
    tr_id = "TTTT1002U" if is_buy else "TTTT1006U"

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"

    headers = {
        # 🔥 authorization 제거 (_kis_request 내부에서 자동 추가)
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
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
        # 매수: LOC(34) / 매도: 지정가(00)
        "ORD_DVSN": "34" if is_buy else "00",

        # 🔥 해외주식 주문 가격 필드
        "OVRS_ORD_UNPR": f"{price:.2f}",

        # 기본값
        "ORD_SVR_DVSN_CD": "0"
    }

    # 🔥 네트워크 일시 오류 대비 재시도 1회
    for i in range(2):
        try:
            # 🔥 내부에서:
            # 1) 토큰 자동 발급
            # 2) 401 발생 시 자동 재발급 후 재시도
            res = _kis_request(
                method="POST",
                url=url,
                headers=headers,
                json=body
            )

            print("===== KIS ORDER DEBUG =====")
            print("STATUS:", res.status_code)
            print("URL:", url)
            print("HEADERS:", headers)
            print("BODY:", body)

            try:
                resp_json = res.json()
                print("RESPONSE JSON:", resp_json)
            except Exception:
                resp_json = None
                print("RESPONSE TEXT:", res.text)

            print("==========================")

            # 🔥 KIS 업무 오류 코드 체크
            if not resp_json or resp_json.get("rt_cd") != "0":
                raise RuntimeError(
                    f"KIS 주문 실패: {resp_json}"
                )

            return resp_json  # 🔥 정상 주문 성공

        except requests.exceptions.RequestException as e:
            print("KIS 네트워크 오류:", e)
            time.sleep(1)

        except Exception as e:
            print("KIS 주문 로직 오류:", e)
            time.sleep(1)

    # 🔥 2회 모두 실패
    raise RuntimeError("KIS 주문 2회 실패")


def sell_all_overseas_stock(ticker: str, price: float):
    info = get_overseas_avg_price(ticker)

    if not info["found"] or info["sellable_qty"] <= 0:
        return {"error": "매도 가능 수량 없음"}

    return order_overseas_stock(
        ticker=ticker,
        price=price,
        qty=info["sellable_qty"],
        side="sell"
    )
