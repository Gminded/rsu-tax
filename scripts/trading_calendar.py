# Copyright (C) 2025 Gianluca Guidi
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see
# <https://www.gnu.org/licenses/>.

"""Roll a nominal date forward to the first real trading day on/after it.

A confirmation PDF's release date is the *nominal* vesting date from the grant
schedule; it does not skip weekends or market holidays.  The actual vest settles
on the first trading day on or after that date, which is what FX month, UK tax
year and HS284 matching must key off.

# ponytail: rejected hand-rolled holiday table — market holidays != federal
# (Good Friday closed, Columbus/Veterans Day open) and a static list rots yearly.
# ponytail: one helper, lru_cache the calendar (building it is not free).
"""

from datetime import date
from functools import lru_cache

import pandas as pd
import exchange_calendars as xcals

DEFAULT_EXCHANGE = "XNAS"  # NASDAQ; the employer's stock trades there.


@lru_cache(maxsize=None)
def _calendar(exchange: str):
    if exchange not in xcals.get_calendar_names():
        raise ValueError(
            f"Unknown exchange calendar {exchange!r}. "
            f"Valid codes: {', '.join(sorted(xcals.get_calendar_names()))}"
        )
    return xcals.get_calendar(exchange)


def first_trading_day_on_or_after(d, exchange: str = DEFAULT_EXCHANGE) -> date:
    """First exchange session on or after `d` (a date or an ISO 'YYYY-MM-DD' str).

    Returns `d` unchanged when it is already a trading day.
    """
    cal = _calendar(exchange)
    return cal.date_to_session(pd.Timestamp(d), direction="next").date()


if __name__ == "__main__":
    # ponytail: self-check — weekend roll, holiday roll, already-a-session unchanged.
    assert first_trading_day_on_or_after("2019-06-01").isoformat() == "2019-06-03"  # Sat
    assert first_trading_day_on_or_after("2019-09-01").isoformat() == "2019-09-03"  # Sun
    assert first_trading_day_on_or_after("2019-04-19").isoformat() == "2019-04-22"  # Good Friday
    assert first_trading_day_on_or_after("2019-06-03").isoformat() == "2019-06-03"  # already a session
    try:
        first_trading_day_on_or_after("2020-01-02", exchange="NOPE")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on bad exchange code")
    print("ok")
