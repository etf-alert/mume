# cron_execute_orders.py
import sqlite3
from datetime import datetime, timezone

from kis_api import order_overseas_stock
from price_api import get_current_price   # 🟢 신규: 현재가 조회

DB_FILE = "rsi_history.db"

def run():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    now = datetime.now(timezone.utc)

    rows = cur.execute("""
        SELECT *
        FROM queued_orders
        WHERE status = 'PENDING'
    """).fetchall()

    ready = []
    for o in rows:
        execute_after = datetime.fromisoformat(o["execute_after"])
        if execute_after <= now:
            ready.append(o)

    print(f"▶ ready orders: {len(ready)}")

    for o in ready:
        # 🔒 실행 락 (유지)
        cur.execute("""
            UPDATE queued_orders
            SET status = 'RUNNING'
            WHERE id = ? AND status = 'PENDING'
        """, (o["id"],))
        conn.commit()

        if cur.rowcount == 0:
            continue

        try:
            # =========================
            # 🟢 1️⃣ 실행 시점 현재가 조회
            # =========================
            current_price = get_current_price(o["ticker"])
            if not current_price or current_price <= 0:
                raise ValueError("invalid current price")

            avg_price = o["avg_price"]
            seed = o["seed"]

            # =========================
            # 🟢 2️⃣ 실행 시점 가격 계산
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

            # =========================
            # 🟢 3️⃣ 수량 계산 (여기서!)
            # =========================
            qty = int(half_split // price)
            if qty <= 0:
                raise ValueError("qty <= 0")

            price = round(price, 2)

            print(
                "▶ executing:",
                o["ticker"],
                o["side"],
                f"price={price}",
                f"qty={qty}",
                f"current={current_price}"
            )

            # =========================
            # 🟢 4️⃣ 실제 주문 실행
            # =========================
            order_overseas_stock(
                ticker=o["ticker"],
                price=price,
                qty=qty,
                side="buy" if o["side"].startswith("BUY") else "sell"
            )

            # 🟢 성공 시 삭제
            cur.execute(
                "DELETE FROM queued_orders WHERE id = ?",
                (o["id"],)
            )
            conn.commit()

            print("✅ done:", o["id"])

        except Exception as e:
            # 🔴 실패 시 다시 PENDING
            cur.execute("""
                UPDATE queued_orders
                SET status = 'PENDING'
                WHERE id = ?
            """, (o["id"],))
            conn.commit()

            print("❌ order failed:", o["id"], str(e))

    conn.close()


if __name__ == "__main__":
    run()
