"""
Tests for ui/watcher.py — focused on the new folder-mode logic.

These tests don't start the QThread (which would require a QApplication
and the Qt event loop running). They test the helper methods directly:
constructor states, file discovery, attach logic.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class _FakeSignal:
    """Stand-in for pyqtSignal so we don't need Qt to instantiate the class."""
    def __init__(self):
        self.emissions = []
        self.connect = MagicMock()

    def emit(self, *args):
        self.emissions.append(args)


class TestWatcherFolderMode(unittest.TestCase):
    """Verify the folder-mode constructor and _find_latest_file logic."""

    def setUp(self):
        # Import inside setUp so import failures still produce a clean
        # test report from the rest of the suite.
        from ui.watcher import LogWatcherThread
        self.LogWatcherThread = LogWatcherThread
        self._tmpdir = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_watcher_in_folder_mode(self):
        """Build a folder-mode watcher and stub out the Qt signals so we
        can inspect what would have been emitted without running the
        thread or having a real QApplication."""
        w = self.LogWatcherThread.from_folder(str(self.folder))
        # Replace signals (bound pyqtSignals) with our fake on the
        # instance — pyqtSignals are read-only at class level but the
        # following sets an instance attribute that shadows the class one
        # for the purposes of our calls.
        w.new_events = _FakeSignal()
        w.log_switched = _FakeSignal()
        w.watch_error = _FakeSignal()
        w.watch_stopped = _FakeSignal()
        return w

    def test_from_folder_starts_unattached(self):
        w = self.LogWatcherThread.from_folder(str(self.folder))
        self.assertTrue(w._folder_mode)
        self.assertEqual(w.path, "")
        self.assertEqual(w._current_resolved, "")
        self.assertEqual(w._last_pos, 0)
        self.assertEqual(Path(w._log_dir).resolve(), self.folder.resolve())

    def test_find_latest_with_empty_folder(self):
        w = self._make_watcher_in_folder_mode()
        self.assertIsNone(w._find_latest_file())

    def test_find_latest_picks_newest_by_mtime(self):
        # Three files, deliberately stamped with different mtimes
        a = self.folder / "combat_old.txt";    a.write_text("a")
        b = self.folder / "combat_middle.txt"; b.write_text("b")
        c = self.folder / "combat_new.txt";    c.write_text("c")
        now = time.time()
        os.utime(a, (now - 300, now - 300))
        os.utime(b, (now - 200, now - 200))
        os.utime(c, (now - 100, now - 100))

        w = self._make_watcher_in_folder_mode()
        latest = w._find_latest_file()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.name, "combat_new.txt")

    def test_find_latest_ignores_subdirs(self):
        (self.folder / "subdir").mkdir()
        (self.folder / "subdir" / "combat_bogus.txt").write_text("nope")
        real = self.folder / "combat_real.txt"
        real.write_text("data")
        w = self._make_watcher_in_folder_mode()
        latest = w._find_latest_file()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.name, "combat_real.txt")

    def test_find_latest_missing_folder_returns_none(self):
        nonexistent = self.folder / "does_not_exist"
        w = self.LogWatcherThread.from_folder(str(nonexistent))
        w.new_events = _FakeSignal()
        w.log_switched = _FakeSignal()
        w.watch_error = _FakeSignal()
        w.watch_stopped = _FakeSignal()
        self.assertIsNone(w._find_latest_file())


class TestWatcherFileModePreserved(unittest.TestCase):
    """Make sure existing file-mode behavior is intact."""

    def setUp(self):
        from ui.watcher import LogWatcherThread
        self.LogWatcherThread = LogWatcherThread
        self._tmpdir = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmpdir.name)
        self.file = self.folder / "combat_test.txt"
        self.file.write_text("seed contents\n")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_file_mode_init(self):
        w = self.LogWatcherThread(str(self.file))
        self.assertFalse(w._folder_mode)
        self.assertEqual(Path(w.path).resolve(), self.file.resolve())
        self.assertEqual(
            Path(w._current_resolved).resolve(),
            self.file.resolve(),
        )
        self.assertEqual(Path(w._log_dir).resolve(), self.folder.resolve())


if __name__ == "__main__":
    unittest.main()
