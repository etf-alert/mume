import sqlite3
from datetime import datetime
from kis_api import order_overseas_stock
from market import market_status

DB_FILE = "rsi_history.db"

def run():
    is_open, _ = market_status()
    if not is_open:
        print("❌ Market not open")
        return

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT id, ticker, side, price, qty
        FROM queued_orders
        ORDER BY created_at
    """).fetchall()

    for oid, ticker, side, price, qty in rows:
        try:
            print("▶ executing:", ticker, side, qty, price)
            order_overseas_stock(
                ticker=ticker,
                price=price,
                qty=qty,
                side="buy" if side.startswith("BUY") else "sell"
            )
            cur.execute("DELETE FROM queued_orders WHERE id = ?", (oid,))
            conn.commit()
        except Exception as e:
            print("❌ order failed:", oid, e)

    conn.close()

if __name__ == "__main__":
    run()
