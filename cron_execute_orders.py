# cron_execute_orders.py
from datetime import datetime, timezone, timedelta
from supabase import create_client
from kis_api import order_overseas_stock, get_overseas_avg_price
from price_api import get_current_price
from telegram import (
    send_order_success_telegram,
    send_order_fail_telegram
)
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase env not set")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MAX_RETRY = 3


def run():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # =========================
    # 🔥 오래된 RUNNING 복구
    # =========================
    supabase.table("queued_orders") \
        .update({"status": "PENDING"}) \
        .eq("status", "RUNNING") \
        .lte("updated_at", (now - timedelta(minutes=10)).isoformat()) \
        .execute()

    # =========================
    # 실행 대상 조회
    # =========================
    claim_res = supabase.rpc(
        "claim_next_orders",
        {"batch_size": 20}
    ).execute()

    orders = claim_res.data or []
    print(f"▶ claimed orders: {len(orders)}")

    for o in orders:
        try:
            ticker = o["ticker"]

            # =========================
            # 🔥 실시간 현재가 조회
            # =========================
            current_price = get_current_price(ticker)
            if not current_price or current_price <= 0:
                raise ValueError("invalid current price")

            seed = float(o["seed"])

            # =========================
            # 🔥 실시간 평단가 조회 (DB 값 사용 안함)
            # =========================
            pos = get_overseas_avg_price(ticker)
            if not pos:
                raise ValueError("position fetch failed")

            avg_price = float(pos.get("avg_price") or 0)  # 🔥 수정
            qty_owned = int(pos.get("qty") or 0)          # 🔥 수정

            # =========================
            # 가격 / 수량 계산
            # =========================
            if o["side"] == "BUY_MARKET":

                if avg_price <= 0:  # 🔥 방어
                    raise ValueError("invalid avg_price")

                price = round(min(avg_price * 1.05, current_price * 1.15), 2)
                if price <= 0:
                    raise ValueError("invalid price")

                qty = int((seed / 80) // price)
                side = "buy"

            elif o["side"] == "BUY_AVG":

                if avg_price <= 0:  # 🔥 방어
                    raise ValueError("invalid avg_price")

                price = round(avg_price, 2)
                qty = int((seed / 80) // price)
                side = "buy"

            elif o["side"] == "SELL":

                if qty_owned <= 0:
                    raise ValueError("no position to sell")

                target_price = round(avg_price * 1.10, 2)

                if current_price > target_price:
                    price = round(current_price, 2)
                else:
                    price = target_price

                qty = qty_owned
                side = "sell"

            else:
                raise ValueError(f"unknown side: {o['side']}")

            if qty <= 0:
                raise ValueError("qty <= 0")

            print(
                "▶ executing:",
                ticker,
                o["side"],
                f"price={price}",
                f"qty={qty}",
                f"current={current_price}"
            )

            # =========================
            # 실제 주문
            # =========================
            kis_res = order_overseas_stock(
                ticker=ticker,
                price=price,
                qty=qty,
                side=side
            )

            # 🔥 KIS 응답 검증 (실패 응답 방어)
            if isinstance(kis_res, dict):
                if kis_res.get("rt_cd") not in ["0", 0, None]:
                    raise ValueError(f"KIS error: {kis_res}")

            # =========================
            # 성공 처리
            # =========================
            supabase.table("queued_orders").update({
                "status": "DONE",
                "executed_at": now_iso,
                "error": None
            }).eq("id", o["id"]).execute()

            # 🔥 텔레그램도 안전하게
            try:
                send_order_success_telegram(
                    order=o,
                    executed_price=price,
                    executed_qty=qty,
                    executed_at=now,
                    kis_msg=kis_res.get("msg1") if isinstance(kis_res, dict) else None,
                    db=supabase
                )
            except Exception as tg_err:
                print("⚠ telegram error:", tg_err)

            print("✅ done:", o["id"])

        except Exception as e:
            retry = (o.get("retry_count") or 0) + 1

            if retry >= MAX_RETRY:
                update = {
                    "retry_count": retry,
                    "status": "DONE",   # 🔥 ERROR 대신 DONE 처리
                    "error": str(e)
                }
            else:
                update = {
                    "retry_count": retry,
                    "status": "PENDING",
                    "error": str(e)
                }

            supabase.table("queued_orders") \
                .update(update) \
                .eq("id", o["id"]) \
                .execute()

            # 🔥 같은 그룹 이후 회차 하루 밀기
            try:
                supabase.rpc("shift_group_forward", {
                    "p_repeat_group": o["repeat_group"],
                    "p_repeat_index": o["repeat_index"]
                }).execute()
            except Exception as rpc_err:
                print("⚠ shift_group_forward error:", rpc_err)
        
            print("❌ order failed:", o["id"], str(e))
        
if __name__ == "__main__":
    run()
