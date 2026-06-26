from __future__ import annotations

import unittest
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from arxiv_daily.schedule import (
    digest_cycle_for_date,
    eligible_digest_cycle,
    next_digest_start,
)


class DigestScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chicago = ZoneInfo("America/Chicago")
        self.search_time = time(20, 0)

    def test_sunday_evening_and_monday_daytime_target_monday_digest(self) -> None:
        sunday = eligible_digest_cycle(
            datetime(2026, 6, 14, 20, 0, tzinfo=self.chicago),
            self.search_time,
        )
        monday = eligible_digest_cycle(
            datetime(2026, 6, 15, 9, 0, tzinfo=self.chicago),
            self.search_time,
        )

        self.assertEqual(sunday.digest_date, date(2026, 6, 15))
        self.assertEqual(monday.digest_date, date(2026, 6, 15))
        self.assertEqual(sunday.submission_end, date(2026, 6, 12))
        self.assertEqual(
            sunday.announcement_at.astimezone(self.chicago).strftime(
                "%Y-%m-%d %H:%M %Z"
            ),
            "2026-06-14 19:00 CDT",
        )

    def test_weekday_evening_opens_the_following_reading_day(self) -> None:
        cycle = eligible_digest_cycle(
            datetime(2026, 6, 15, 20, 0, tzinfo=self.chicago),
            self.search_time,
        )

        self.assertEqual(cycle.digest_date, date(2026, 6, 16))
        self.assertEqual(cycle.submission_end, date(2026, 6, 15))

    def test_friday_night_and_saturday_keep_friday_digest(self) -> None:
        friday = eligible_digest_cycle(
            datetime(2026, 6, 12, 22, 0, tzinfo=self.chicago),
            self.search_time,
        )
        saturday = eligible_digest_cycle(
            datetime(2026, 6, 13, 12, 0, tzinfo=self.chicago),
            self.search_time,
        )

        self.assertEqual(friday.digest_date, date(2026, 6, 12))
        self.assertEqual(saturday.digest_date, date(2026, 6, 12))

    def test_next_cycle_skips_friday_and_saturday_nights(self) -> None:
        next_start = next_digest_start(
            datetime(2026, 6, 12, 10, 0, tzinfo=self.chicago),
            self.search_time,
        )

        self.assertEqual(
            next_start,
            datetime(2026, 6, 14, 20, 0, tzinfo=self.chicago),
        )

    def test_announcement_time_tracks_daylight_saving_and_year_boundary(self) -> None:
        summer = digest_cycle_for_date(date(2026, 6, 15))
        winter = digest_cycle_for_date(date(2027, 1, 4))
        boundary = eligible_digest_cycle(
            datetime(2028, 12, 31, 20, 0, tzinfo=self.chicago),
            self.search_time,
        )

        self.assertEqual(summer.announcement_at.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(winter.announcement_at.utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(boundary.digest_date, date(2029, 1, 1))


if __name__ == "__main__":
    unittest.main()
