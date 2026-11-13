import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backup_manager import utils


def ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


class SanitizeJobId(unittest.TestCase):
    def test_lowercases_and_replaces_punctuation(self):
        self.assertEqual(utils.sanitize_job_id("George's Phone (Pixel)-2"), "georges_phone_pixel_2")


class ResolvePath(unittest.TestCase):
    def test_relative_is_joined_to_base(self):
        self.assertEqual(utils.resolve_path("tools/x.ps1", Path("/base")), Path("/base/tools/x.ps1"))

    def test_absolute_is_kept(self):
        self.assertEqual(utils.resolve_path("/abs/x", Path("/base")), Path("/abs/x"))


class Freshness(unittest.TestCase):
    def test_none_and_garbage_are_not_fresh(self):
        self.assertFalse(utils.is_fresh(None))
        self.assertFalse(utils.is_fresh("not a date"))

    def test_rolling_24_hour_window(self):
        self.assertTrue(utils.is_fresh(ago(hours=23)))
        self.assertFalse(utils.is_fresh(ago(hours=25)))

    def test_future_timestamp_counts_as_fresh(self):
        self.assertTrue(utils.is_fresh(datetime.now(timezone.utc) + timedelta(hours=2)))

    def test_naive_datetime_is_read_as_utc(self):
        naive = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
        self.assertTrue(utils.is_fresh(naive))

    def test_iso_string(self):
        self.assertTrue(utils.is_fresh(ago(hours=1).isoformat()))


class RelativeTime(unittest.TestCase):
    def test_never_and_unknown(self):
        self.assertEqual(utils.relative_time(None), "Never")
        self.assertEqual(utils.relative_time("nonsense"), "Unknown")

    def test_buckets(self):
        self.assertEqual(utils.relative_time(ago(minutes=5)), "Just now")
        self.assertEqual(utils.relative_time(ago(hours=5, minutes=1)), "5h ago")
        self.assertEqual(utils.relative_time(ago(days=3, hours=1)), "3d ago")
        self.assertEqual(utils.relative_time(ago(days=65)), "2mo ago")
        self.assertEqual(utils.relative_time(ago(days=800)), "2y ago")

    def test_just_under_a_year_is_months_not_zero_years(self):
        # Regression: 360-364 days used to fall through to "0y ago".
        self.assertEqual(utils.relative_time(ago(days=362)), "12mo ago")


class ConsolePython(unittest.TestCase):
    def test_plain_interpreter_is_unchanged(self):
        self.assertEqual(utils.console_python(), sys.executable)

    def test_pythonw_maps_to_python_exe_beside_it(self):
        with mock.patch.object(sys, "executable", "/x/Scripts/pythonw.exe"), \
                mock.patch.object(Path, "exists", return_value=True):
            self.assertEqual(Path(utils.console_python()).name, "python.exe")

    def test_pythonw_without_console_sibling_is_kept(self):
        with mock.patch.object(sys, "executable", "/x/Scripts/pythonw.exe"), \
                mock.patch.object(Path, "exists", return_value=False):
            self.assertEqual(Path(utils.console_python()).name, "pythonw.exe")


class LocalExe(unittest.TestCase):
    def test_off_windows_returns_the_original(self):
        p = Path("/some/rclone")
        with mock.patch.object(utils.os, "name", "posix"):
            self.assertEqual(utils.local_exe(p), p)
