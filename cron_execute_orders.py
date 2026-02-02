# cron_execute_orders.py
import sqlite3
from datetime import datetime
from market_time import is_us_market_open
from kis_api import order_overseas_stock

DB_FILE = "rsi_history.db"

def run():
    # ❌ 장 안 열렸으면 스킵
    if not is_us_market_open():
        print("❌ Market closed")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ✅ execute_after 지난 주문만 실행
    rows = cur.execute("""
        SELECT *
        FROM queued_orders
        WHERE execute_after <= ?
        ORDER BY created_at ASC
    """, (datetime.utcnow().isoformat(),)).fetchall()

    print(f"▶ queued orders: {len(rows)}")

    for o in rows:
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

            # ✅ 성공 → 삭제
            cur.execute(
                "DELETE FROM queued_orders WHERE id = ?",
                (o["id"],)
            )
            conn.commit()

            print("✅ done:", o["id"])

        except Exception as e:
            # ❗ 실패 → 유지 (다음 cron 재시도)
            print("❌ order failed:", o["id"], str(e))

    conn.close()

if __name__ == "__main__":
    run()
