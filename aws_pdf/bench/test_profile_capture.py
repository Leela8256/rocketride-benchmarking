import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from profile_capture import ProfileCapture


class FakeClient:
    def __init__(self, active=False):
        self.active, self.session = active, "someone-else"
        self.calls = []

    async def call(self, command, **kwargs):
        op = command.removeprefix("rrext_cprofile_")
        self.calls.append((op, kwargs))
        if op == "status":
            return {"active": self.active, "session": self.session}
        if op == "start":
            self.active, self.session = True, kwargs["session"]
            return {"status": "started", "session": self.session}
        if op == "stop":
            self.active = False
            return {"status": "completed", "runtime": 0.02}
        if op == "threads":
            return {"threads": [{"id": 0, "ttot": 0.02}]}
        if op == "report":
            return {"report": "marker 7 calls"}
        return {"tree": {"name": "marker"}, "total_calls": 7}


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_does_not_contact_server_or_write(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            async with ProfileCapture(client, "token", d):
                pass
            self.assertEqual(client.calls, [])
            self.assertEqual(list(Path(d).iterdir()), [])

    async def test_refuses_existing_session_without_stopping_it(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(active=True)
            with self.assertRaisesRegex(RuntimeError, "already"):
                async with ProfileCapture(client, "token", d, 1):
                    self.fail("must refuse")
            self.assertTrue(client.active)
            self.assertEqual([op for op, _ in client.calls], ["status"])

    async def test_deadline_stops_once_exports_after_work(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            async with ProfileCapture(client, "token", d, 0.01):
                await asyncio.sleep(0.04)
                self.assertFalse(client.active)
                self.assertFalse((Path(d) / "profiler/report.txt").exists())
            self.assertEqual(sum(op == "stop" for op, _ in client.calls), 1)
            self.assertTrue((Path(d) / "profiler/thread-0.json").exists())
            self.assertTrue(json.loads((Path(d) / "profiler/session.json").read_text())["complete"])

    async def test_workload_error_still_stops_and_exports(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            with self.assertRaisesRegex(ValueError, "work failed"):
                async with ProfileCapture(client, "token", d, 60):
                    raise ValueError("work failed")
            self.assertFalse(client.active)
            meta = json.loads((Path(d) / "profiler/session.json").read_text())
            self.assertTrue(meta["complete"])
            self.assertIn("work failed", meta["workload_error"])

    async def test_replaced_session_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            with self.assertRaisesRegex(RuntimeError, "replaced"):
                async with ProfileCapture(client, "token", d, 60):
                    client.session = "replacement"
            self.assertTrue(client.active)
            self.assertFalse(any(op == "stop" for op, _ in client.calls))


class TimingTests(unittest.TestCase):
    def test_mixed_batch_timestamps_do_not_publish_latency(self):
        from report import rep_report
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            rows = []
            for i, source in enumerate(["client-observed upload event", "batch_upload_time (derived, not measured)"]):
                rows.append({"doc": f"{i}.pdf", "ok": True, "identity_ok": True,
                             "input_sha256": "input", "n_chunks": 1,
                             "chunk_sha256": ["text"], "vector_dim": 384,
                             "l2_norms_minmax": [1, 1], "submit_ns": 1000000000,
                             "completion_ns": (i+2)*1000000000, "timing_source": source})
            rows.append({"kind": "shot_meta", "mode": "b2", "span_s": 2})
            (path / "per_doc.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
            (path / "manifest.json").write_text(json.dumps({"n": 2, "docs": ["0.pdf", "1.pdf"]}))
            result = rep_report(path, "rr", 32, 24, "b2", {"0.pdf": "input", "1.pdf": "input"})
            self.assertIn("unavailable", result["m2_latency"])
            self.assertIsNone(result["concurrency"]["achieved_peak"])
            rows[-1].update(measurement_t0_ns=1000000000,
                            measurement_t1_ns=5000000000, span_s=4)
            (path / "per_doc.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
            result = rep_report(path, "rr", 32, 24, "b2", {"0.pdf": "input", "1.pdf": "input"})
            self.assertEqual(result["m1_throughput"]["window_t1_ns"], 5000000000)
            self.assertEqual(result["m1_throughput"]["docs_per_s"], 0.5)


if __name__ == "__main__":
    unittest.main()
