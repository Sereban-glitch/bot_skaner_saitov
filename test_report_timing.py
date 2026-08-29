import unittest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from send_channel_report import local_day_start

class TestReportTiming(unittest.TestCase):
    def test_local_day_start_always_returns_yesterday_midnight_to_midnight(self):
        tz = ZoneInfo("Europe/Kyiv")
        run_time = datetime(2026, 8, 28, 1, 5, tzinfo=tz)
        
        start_utc, end_utc, local_report_day = local_day_start(run_time)
        
        expected_start = datetime(2026, 8, 27, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        expected_end = datetime(2026, 8, 28, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        
        self.assertEqual(start_utc, expected_start)
        self.assertEqual(end_utc, expected_end)
        self.assertEqual(local_report_day.strftime("%Y-%m-%d"), "2026-08-27")

    def test_local_day_start_dst_boundary(self):
        tz = ZoneInfo("Europe/Kyiv")
        run_time = datetime(2026, 10, 26, 1, 0, tzinfo=tz)
        
        start_utc, end_utc, local_report_day = local_day_start(run_time)
        
        expected_start = datetime(2026, 10, 25, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        expected_end = datetime(2026, 10, 26, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        
        self.assertEqual(start_utc, expected_start)
        self.assertEqual(end_utc, expected_end)
        
        duration = end_utc - start_utc
        self.assertEqual(duration, timedelta(hours=25))

    def test_local_day_start_spring_dst_boundary(self):
        tz = ZoneInfo("Europe/Kyiv")
        run_time = datetime(2026, 3, 30, 1, 0, tzinfo=tz)

        start_utc, end_utc, local_report_day = local_day_start(run_time)

        self.assertEqual(local_report_day.strftime("%Y-%m-%d"), "2026-03-29")
        self.assertEqual(end_utc - start_utc, timedelta(hours=23))

if __name__ == "__main__":
    unittest.main()
