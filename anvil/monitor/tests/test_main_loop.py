"""v5 Phase 1b — the main-loop integration + the duplicate-log fix (1a
Amendment 4). Mock-first."""
import logging
import os
import tempfile
import unittest

from anvil.monitor import main


class TestLoggingNoFileHandler(unittest.TestCase):
    def test_configure_logging_is_stdout_only(self):
        # 1a Amendment 4: the FileHandler was dropped (systemd owns the file).
        # _configure_logging must add no FileHandler even when LOG_PATH is set.
        root = logging.getLogger()
        saved = root.handlers[:]
        os.environ["LOG_PATH"] = "/tmp/should-not-be-opened-by-anvil-monitor.log"
        try:
            root.handlers = []
            main._configure_logging()
            file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
            self.assertEqual(file_handlers, [], "FileHandler must be dropped (1a Amendment 4)")
            self.assertTrue(any(isinstance(h, logging.StreamHandler) for h in root.handlers))
        finally:
            root.handlers = saved
            os.environ.pop("LOG_PATH", None)


class TestSelfcheckStillWorks(unittest.TestCase):
    def test_selfcheck_inits_and_exits_zero(self):
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(db)
        try:
            self.assertEqual(main.selfcheck(db), 0)
        finally:
            if os.path.exists(db):
                os.unlink(db)


if __name__ == "__main__":
    unittest.main()
