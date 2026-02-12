import pandas_market_calendars as mcal
from datetime import datetime, timedelta
import pytz

nyse = mcal.get_calendar("NYSE")
ny_tz = pytz.timezone("US/Eastern")

def is_us_premarket(now=None):
    if not now:
        now = datetime.now(ny_tz)

    schedule = nyse.schedule(
        start_date=now.date(),
        end_date=now.date()
    )
    if schedule.empty:
        return False

    open_time = schedule.iloc[0]["market_open"].to_pydatetime()
    pre_open = open_time.replace(hour=4, minute=0)

    return pre_open <= now < open_time


def is_us_postmarket(now=None):
    if not now:
        now = datetime.now(ny_tz)

    schedule = nyse.schedule(
        start_date=now.date(),
        end_date=now.date()
    )
    if schedule.empty:
        return False

    close_time = schedule.iloc[0]["market_close"].to_pydatetime()
    post_close = close_time.replace(hour=20, minute=0)

    return close_time < now <= post_close

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

    return open_time <= now < close_time

def next_market_open(base_date=None):
    if base_date is None:
        base_date = datetime.now(ny_tz).date()
    elif isinstance(base_date, datetime):
        base_date = base_date.date()

    schedule = nyse.schedule(
        start_date=base_date,
        end_date=base_date + timedelta(days=7)
    )

    if schedule.empty:
        return None

    # ✅ 그대로 반환 (tz 유지)
    return schedule.iloc[0]["market_open"]

def get_next_trading_day(base_date=None):
    if base_date is None:
        base_date = datetime.now(ny_tz).date()
    elif isinstance(base_date, datetime):
        base_date = base_date.date()

    # 🔥 연휴 대비 여유있게 14일
    schedule = nyse.schedule(
        start_date=base_date + timedelta(days=1),
        end_date=base_date + timedelta(days=14)
    )

    if schedule.empty:
        return None

    return schedule.index[0].date()

    
def get_next_n_trading_days(start_date, n):
    if isinstance(start_date, datetime):
        start_date = start_date.date()

    # 🔥 필요한 날짜만큼 넉넉히 확보 (n * 2 정도면 충분)
    end_date = start_date + timedelta(days=n * 2)

    schedule = nyse.schedule(
        start_date=start_date,
        end_date=end_date
    )

    if schedule.empty:
        return []

    days = schedule.index.date.tolist()

    # 🔥 정확히 n개만 반환
    return days[:n]


