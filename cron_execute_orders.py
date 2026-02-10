# cron_execute_orders.py
from datetime import datetime, timezone
from supabase import create_client
from kis_api import order_overseas_stock
from price_api import get_current_price

# =========================
# 🔐 Supabase 설정
# =========================
SUPABASE_URL = "https://xxxx.supabase.co"
SUPABASE_KEY = "SERVICE_ROLE_KEY"  # ⚠️ 반드시 service_role
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def run():
    now = datetime.now(timezone.utc).isoformat()

    # =========================
    # 1️⃣ 실행 대상 주문 조회
    # =========================
    res = (
        supabase
        .table("queued_orders")
        .select("*")
        .eq("status", "PENDING")
        .lte("execute_after", now)
        .execute()
    )

    orders = res.data or []
    print(f"▶ ready orders: {len(orders)}")

    for o in orders:
        # =========================
        # 2️⃣ 실행 락 (PENDING → RUNNING)
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
            continue  # 다른 워커가 잡음

        try:
            # =========================
            # 3️⃣ 실행 시점 현재가 조회
            # =========================
            current_price = get_current_price(o["ticker"])
            if not current_price or current_price <= 0:
                raise ValueError("invalid current price")

            avg_price = o["avg_price"]
            seed = o["seed"]

            # =========================
            # 4️⃣ 실행 시점 가격 계산
            # =========================
            half_split = (seed / 40) / 2

            if o["side"] == "BUY_AVG":
                price = min(
                    avg_price * 1.05,
                    current_price * 1.15
                )
            elif o["side"] == "BUY_MARKET":
                price = current_price * 1.15
            elif o["side"] == "SELL":
                price = avg_price * 1.10
            else:
                raise ValueError(f"unknown side: {o['side']}")

            price = round(price, 2)

            # =========================
            # 5️⃣ 수량 계산
            # =========================
            qty = int(half_split // price)
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
            # 6️⃣ 실제 주문 실행
            # =========================
            order_overseas_stock(
                ticker=o["ticker"],
                price=price,
                qty=qty,
                side="buy" if o["side"].startswith("BUY") else "sell"
            )

            # =========================
            # 7️⃣ 성공 → 삭제
            # =========================
            supabase \
                .table("queued_orders") \
                .delete() \
                .eq("id", o["id"]) \
                .execute()

            print("✅ done:", o["id"])

        except Exception as e:
            # =========================
            # 🔴 실패 → PENDING 복구
            # =========================
            supabase \
                .table("queued_orders") \
                .update({"status": "PENDING"}) \
                .eq("id", o["id"]) \
                .execute()

            print("❌ order failed:", o["id"], str(e))


if __name__ == "__main__":
    run()
