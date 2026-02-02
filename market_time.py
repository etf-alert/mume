import pandas_market_calendars as mcal
from datetime import datetime
import pytz

nyse = mcal.get_calendar("NYSE")
ny_tz = pytz.timezone("US/Eastern")

def is_us_market_open(now=None):
    if not now:
        now = datetime.now(ny_tz)

    schedule = nyse.schedule(
        start_date=now.date(),
        end_date=now.date()
    )

    if schedule.empty:
        return False

    open_time = schedule.iloc[0]["market_open"]
    close_time = schedule.iloc[0]["market_close"]

    return open_time <= now <= close_time


def next_market_open():
    today = datetime.now(ny_tz).date()
    schedule = nyse.schedule(
        start_date=today,
        end_date=today + timedelta(days=7)
    )
    return schedule.iloc[0]["market_open"]
