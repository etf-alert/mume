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

MAX_RETRY = 3  # 🟢 NEW: 최대 재시도 횟수


def run():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # =========================
    # 🟢 NEW: 오래된 RUNNING 복구 (락 유실 대비)
    # =========================
    supabase.table("queued_orders") \
        .update({"status": "PENDING"}) \
        .eq("status", "RUNNING") \
        .lte("updated_at", (now - timedelta(minutes=10)).isoformat()) \
        .execute()

    # =========================
    # 1️⃣ 실행 대상 조회
    # =========================
    res = (
        supabase
        .table("queued_orders")
        .select("*")
        .eq("status", "PENDING")
        .lte("execute_after", now_iso)
        .execute()
    )

    orders = res.data or []
    print(f"▶ ready orders: {len(orders)}")

    for o in orders:
        # =========================
        # 2️⃣ 실행 락
        # =========================
        lock = (
            supabase
            .table("queued_orders")
            .update({"status": "RUNNING"})
            .eq("id", o["id"])
            .eq("status", "PENDING")
            .execute()
        )

        if not lock.data:
            continue

        try:
            # =========================
            # 3️⃣ 현재가 조회
            # =========================
            current_price = get_current_price(o["ticker"])
            if not current_price or current_price <= 0:
                raise ValueError("invalid current price")

            avg_price = float(o["avg_price"])
            seed = float(o["seed"])

            # =========================
            # 4️⃣ 가격 / 수량 계산
            # (preview / reserve 로직과 완전히 동일)
            # =========================
            if o["side"] == "BUY_MARKET":
                price = round(min(avg_price * 1.05, current_price * 1.15), 2)
                qty = int((seed / 80) // price)  # 🔧 CHANGED: preview와 통일
                side = "buy"

            elif o["side"] == "BUY_AVG":
                price = round(avg_price, 2)
                qty = int((seed / 80) // price)  # 🔧 CHANGED
                side = "buy"

            elif o["side"] == "SELL":
                pos = get_overseas_avg_price(o["ticker"])
                qty = pos["qty"]
                if qty <= 0:
                    raise ValueError("no position to sell")

                target_price = round(avg_price * 1.10, 2)

                # 🔧 CHANGED: preview와 동일한 분기
                if current_price > target_price:
                    price = round(current_price, 2)
                else:
                    price = target_price

                side = "sell"

            else:
                raise ValueError(f"unknown side: {o['side']}")

            if qty <= 0:
                raise ValueError("qty <= 0")

            print(
                "▶ executing:",
                o["ticker"],
                o["side"],
                f"price={price}",
                f"qty={qty}",
                f"current={current_price}"
            )

            # =========================
            # 5️⃣ 실제 주문
            # =========================
            kis_res = order_overseas_stock(   
                ticker=o["ticker"],
                price=price,
                qty=qty,
                side=side
            )

            # =========================
            # 6️⃣ 성공 처리
            # =========================
            supabase.table("queued_orders").update({
                "status": "DONE",
                "executed_at": now_iso,
                "error": None
            }).eq("id", o["id"]).execute()
            # 🟢 NEW: 텔레그램 성공 알림
            send_order_success_telegram(
                order=o,
                executed_price=price,     
                executed_qty=qty,         
                executed_at=now,
                kis_msg=kis_res.get("msg1") if isinstance(kis_res, dict) else None,  
                db=supabase
            )

            print("✅ done:", o["id"])

        except Exception as e:
            retry = (o.get("retry_count") or 0) + 1

            update = {
                "retry_count": retry,
                "error": str(e)
            }

            if retry >= MAX_RETRY:
                update["status"] = "ERROR"

                # 🟢 NEW: 최종 실패 시 텔레그램 알림
                send_order_fail_telegram(
                    order=o,
                    error_msg=str(e),
                    db=supabase_admin
                )
            else:
                update["status"] = "PENDING"

            supabase.table("queued_orders") \
                .update(update) \
                .eq("id", o["id"]) \
                .execute()

            print("❌ order failed:", o["id"], str(e))


if __name__ == "__main__":
    run()
