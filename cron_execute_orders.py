# cron_execute_orders.py
import sqlite3
from datetime import datetime
from kis_api import order_overseas_stock

DB_FILE = "rsi_history.db"

def run():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    now = datetime.utcnow().isoformat()

    rows = cur.execute("""
        SELECT *
        FROM queued_orders
        WHERE execute_after <= ?
          AND status = 'PENDING'
        ORDER BY created_at ASC
    """, (now,)).fetchall()

    print(f"▶ queued orders: {len(rows)}")

    for o in rows:
        try:
            # 🔒 실행 잠금
            cur.execute(
                "UPDATE queued_orders SET status = 'RUNNING' WHERE id = ?",
                (o["id"],)
            )
            conn.commit()

            print(
                "▶ executing:",
                o["ticker"],
                o["side"],
                o["qty"],
                o["price"]
            )

            order_overseas_stock(
                ticker=o["ticker"],
                price=o["price"],
                qty=o["qty"],
                side="buy" if o["side"].startswith("BUY") else "sell"
            )

            # ✅ 성공 → 삭제
            cur.execute(
                "DELETE FROM queued_orders WHERE id = ?",
                (o["id"],)
            )
            conn.commit()
            print("✅ done:", o["id"])

        except Exception as e:
            # ❗ 실패 → 다시 대기 상태
            cur.execute(
                "UPDATE queued_orders SET status = 'PENDING' WHERE id = ?",
                (o["id"],)
            )
            conn.commit()
            print("❌ order failed:", o["id"], str(e))

    conn.close()

if __name__ == "__main__":
    run()
