"""Run in the pinned LangGraph profiling image; no model/server needed."""

import os
import runpy
import sys
import threading
import time
import types
import unittest


class FakeApp:
    def post(self, path):
        return lambda function: function


class ProfilerServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = sys.modules.get("service.app")
        stub = types.ModuleType("service.app")
        stub.app = FakeApp()
        sys.modules["service.app"] = stub
        os.environ["BENCH_PROFILE_CONTROL"] = "1"
        cls.module = runpy.run_path("/app/bench_profile_app.py")

    @classmethod
    def tearDownClass(cls):
        if cls.original is None:
            sys.modules.pop("service.app", None)
        else:
            sys.modules["service.app"] = cls.original

    def test_calls_thread_wall_wait_and_session_guards(self):
        m = self.module
        m["start_profile"]({"session": "known-calls"})
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            m["start_profile"]({"session": "duplicate"})
        with self.assertRaises(HTTPException):
            m["stop_profile"]({"session": "other-owner"})

        def known_leaf():
            time.sleep(0.01)

        def worker():
            for _ in range(7):
                known_leaf()

        thread = threading.Thread(target=worker, name="lg-profiler-canary")
        thread.start()
        thread.join()
        m["stop_profile"]({"session": "known-calls"})
        result = m["report_profile"]({"session": "known-calls"})
        leaf = next(r for r in result["functions"] if r["name"].endswith("known_leaf"))
        self.assertEqual(leaf["ncalls"], 7)
        self.assertGreaterEqual(leaf["cumtime"], 0.06)
        self.assertTrue(any(t["name"] == "lg-profiler-canary" for t in result["threads"]))
        self.assertTrue(result["per_thread"])
        m["start_profile"]({"session": "reset"})
        m["stop_profile"]({"session": "reset"})
        reset = m["report_profile"]({"session": "reset"})
        self.assertFalse(any(r["name"].endswith("known_leaf") for r in reset["functions"]))

    def test_disabled_does_not_register_control(self):
        os.environ["BENCH_PROFILE_CONTROL"] = "0"
        result = runpy.run_path("/app/bench_profile_app.py")
        self.assertNotIn("start_profile", result)
        self.assertNotIn("yappi", result)


if __name__ == "__main__":
    unittest.main()
