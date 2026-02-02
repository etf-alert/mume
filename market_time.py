import pandas_market_calendars as mcal
from datetime import datetime, timedelta
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

    open_time = schedule.iloc[0]["market_open"].to_pydatetime()
    close_time = schedule.iloc[0]["market_close"].to_pydatetime()

    return open_time <= now <= close_time


def next_market_open():
    now = datetime.now(ny_tz)
    schedule = nyse.schedule(
        start_date=now.date(),
        end_date=now.date() + timedelta(days=7)
    )

    if schedule.empty:
        return None

    return schedule.iloc[0]["market_open"].to_pydatetime()
