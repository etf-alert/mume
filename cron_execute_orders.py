import sqlite3
from market_time import is_us_market_open
from kis_api import order_overseas_stock

DB_FILE = "rsi_history.db"

def run():
    if not is_us_market_open():
        print("❌ Market closed")
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
