from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

import pb_db_init_guard_v1215 as guard


class DBInitGuardTests(unittest.TestCase):
    def test_repeated_reruns_initialize_database_once(self):
        calls = []

        def base_init():
            calls.append("init")

        app = SimpleNamespace(init_local_db=base_init)
        guard.apply(app)
        app.init_local_db()
        app.init_local_db()
        app.init_local_db()
        self.assertEqual(calls, ["init"])

    def test_concurrent_first_calls_still_initialize_once(self):
        calls = []
        calls_lock = threading.Lock()

        def base_init():
            time.sleep(0.02)
            with calls_lock:
                calls.append("init")

        app = SimpleNamespace(init_local_db=base_init)
        guard.apply(app)
        threads = [threading.Thread(target=app.init_local_db) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(calls, ["init"])

    def test_apply_is_idempotent(self):
        app = SimpleNamespace(init_local_db=lambda: None)
        guard.apply(app)
        first = app.init_local_db
        guard.apply(app)
        self.assertIs(app.init_local_db, first)


if __name__ == "__main__":
    unittest.main()
