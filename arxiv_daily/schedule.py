from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


ARXIV_ANNOUNCEMENT_TIME = time(20, 0)
ARXIV_TIMEZONE = ZoneInfo("America/New_York")
ANNOUNCEMENT_EVENING_WEEKDAYS = {0, 1, 2, 3, 6}


@dataclass(frozen=True)
class DigestCycle:
    digest_date: date
    announcement_at: datetime
    submission_end: date


def digest_cycle_for_date(digest_date: date) -> DigestCycle:
    if digest_date.weekday() >= 5:
        raise ValueError("Digest dates must be weekdays")
    announcement_date = digest_date - timedelta(days=1)
    submission_end = digest_date - timedelta(
        days=3 if digest_date.weekday() == 0 else 1
    )
    return DigestCycle(
        digest_date=digest_date,
        announcement_at=datetime.combine(
            announcement_date,
            ARXIV_ANNOUNCEMENT_TIME,
            tzinfo=ARXIV_TIMEZONE,
        ),
        submission_end=submission_end,
    )


def _most_recent_friday(day: date) -> date:
    candidate = day
    while candidate.weekday() != 4:
        candidate -= timedelta(days=1)
    return candidate


def eligible_digest_cycle(
    now: datetime,
    search_start_time: time,
) -> DigestCycle:
    local_time = now.timetz().replace(tzinfo=None)
    if now.weekday() == 6 and local_time >= search_start_time:
        digest_date = now.date() + timedelta(days=1)
    elif now.weekday() <= 3 and local_time >= search_start_time:
        digest_date = now.date() + timedelta(days=1)
    elif now.weekday() <= 4:
        digest_date = now.date()
    else:
        digest_date = _most_recent_friday(now.date())
    return digest_cycle_for_date(digest_date)


def next_digest_start(now: datetime, search_start_time: time) -> datetime:
    for offset in range(8):
        candidate_date = now.date() + timedelta(days=offset)
        if candidate_date.weekday() not in ANNOUNCEMENT_EVENING_WEEKDAYS:
            continue
        candidate = datetime.combine(
            candidate_date,
            search_start_time,
            tzinfo=now.tzinfo,
        )
        if candidate > now:
            return candidate
    raise RuntimeError("Could not determine the next digest start")
