# cron_execute_orders.py
import sqlite3
from datetime import datetime, timezone
from kis_api import order_overseas_stock

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
        # 🔒 실행 락
        cur.execute("""
            UPDATE queued_orders
            SET status = 'RUNNING'
            WHERE id = ? AND status = 'PENDING'
        """, (o["id"],))
        conn.commit()

        if cur.rowcount == 0:
            continue

        try:
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

            cur.execute(
                "DELETE FROM queued_orders WHERE id = ?",
                (o["id"],)
            )
            conn.commit()
            print("✅ done:", o["id"])

        except Exception as e:
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
