# cron_execute_orders.py
from datetime import datetime, timezone, timedelta
from supabase import create_client
from kis_api import order_overseas_stock, get_overseas_avg_price
from price_api import get_current_price

SUPABASE_URL = "SUPABASE_URL"
SUPABASE_KEY = "SERVICE_ROLE_KEY"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MAX_RETRY = 3  # 🟢 NEW

def run():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # =========================
    # 🟢 NEW: 오래된 RUNNING 복구
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

            avg_price = o["avg_price"]
            seed = o["seed"]

            # =========================
            # 4️⃣ 가격 / 수량 계산
            # =========================
            if o["side"].startswith("BUY"):
                price = (
                    min(avg_price * 1.05, current_price * 1.15)
                    if o["side"] == "BUY_AVG"
                    else current_price * 1.15
                )
                price = round(price, 2)

                qty = int((seed / 40) // price)
                if qty <= 0:
                    raise ValueError("qty <= 0")

                side = "buy"

            elif o["side"] == "SELL":
                pos = get_overseas_avg_price(o["ticker"])
                qty = pos["qty"]
                if qty <= 0:
                    raise ValueError("no position to sell")

                price = round(avg_price * 1.10, 2)
                side = "sell"

            else:
                raise ValueError(f"unknown side: {o['side']}")

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
            order_overseas_stock(
                ticker=o["ticker"],
                price=price,
                qty=qty,
                side=side
            )

            # =========================
            # 6️⃣ 성공 처리
            # =========================
            supabase.table("queued_orders").update({
                "status": "DONE",                 # 🔧 CHANGED
                "executed_at": now_iso
            }).eq("id", o["id"]).execute()

            print("✅ done:", o["id"])

        except Exception as e:
            retry = (o.get("retry_count") or 0) + 1

            update = {
                "retry_count": retry,
                "error": str(e)
            }

            if retry >= MAX_RETRY:
                update["status"] = "ERROR"      # 🟢 NEW
            else:
                update["status"] = "PENDING"

            supabase.table("queued_orders") \
                .update(update) \
                .eq("id", o["id"]) \
                .execute()

            print("❌ order failed:", o["id"], str(e))

if __name__ == "__main__":
    run()
