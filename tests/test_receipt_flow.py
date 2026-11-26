"""Receipt persistence and provenance, with no services or real generation."""
import contextlib
import copy
import datetime as dt
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import test_comfy_agent as fixtures

ca = fixtures.ca


class ReceiptFlowTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.folder = root / "recipes" / "test"
        self.folder.mkdir(parents=True)
        (self.folder / "card.json").write_text(json.dumps(fixtures.CARD))
        (self.folder / "prompt.json").write_text(json.dumps(fixtures.PROMPT))
        self.stack.enter_context(mock.patch.dict(ca.S, {"url": "fake", "out": str(root / "out"), "send": {},
                                                         "recipes": [str(self.folder.parent)]}, clear=True))
        self.graphs = []

        def graph_receipt(folder, card, prompt, given):
            self.graphs.append((folder, card, copy.deepcopy(prompt), copy.deepcopy(given)))
            return {"graph_marker": prompt["4"]["inputs"]["text"]}

        self.stack.enter_context(mock.patch.dict(sys.modules, {"generation_receipt": types.SimpleNamespace(graph_receipt=graph_receipt)}))
        self.stack.enter_context(mock.patch.object(ca, "preflight", return_value=([], [])))
        self.stack.enter_context(mock.patch.object(ca, "comfy", self.comfy))
        self.stack.enter_context(mock.patch.object(ca, "download", self.download))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stats_calls = 0
        self.stats_available = True
        self.times_available = True
        self.graphs_sent = []

    def comfy(self, path, body=None, **kw):
        if path == "/system_stats":
            self.stats_calls += 1
            if not self.stats_available:
                raise ca.ComfyError(503, "optional stats unavailable")
            return {"system": {"argv": ["irrelevant-system-details"]},
                    "devices": [{"name": "test GPU", "type": "cuda", "vram_free": 400}, {"name": "cpu", "type": "cpu"}]}
        if path == "/prompt":
            self.graphs_sent.append(copy.deepcopy(body["prompt"]))
            return {"prompt_id": str(len(self.graphs_sent))}
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            messages = [["execution_start", {"timestamp": 1700000001750}],
                        ["execution_success", {"timestamp": 1700000004500}]] if self.times_available else []
            return {pid: {"prompt": [1, pid, {}, {"create_time": 1700000000000} if self.times_available else {}],
                          "status": {"status_str": "success", "messages": messages},
                          "outputs": {"9": {"images": [{"filename": "a.png", "type": "output"},
                                                       {"filename": "b.png", "type": "output"}]}}}}
        if path.startswith("/view?") and kw.get("raw"):
            return fixtures.PNG
        self.fail("Unexpected fake ComfyUI call: " + path)

    def download(self, _, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fixtures.PNG)

    def test_cli_batch_receipts_follow_final_graph_and_survive_later_collection(self):
        request = "  Make two for the winter cover.\nUse my image.  "
        prompt = "a fox " * 100
        code = ca.main(["run", "test", "prompt=" + prompt, "seed=55", "--set", "7.steps=13", "--by", "my connector",
                        "--request", request, "--count", "2", "--no-wait"])
        self.assertEqual(code, 0)
        jobs = ca.load_state()["pending"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(self.stats_calls, 1)
        for i, job in enumerate(jobs):
            folder, card, graph, given = self.graphs[i]
            self.assertEqual((folder, card, graph), (self.folder, fixtures.CARD, self.graphs_sent[i]))
            self.assertEqual(given["prompt"], prompt)
            self.assertEqual((graph["7"]["inputs"]["steps"], graph["7"]["inputs"]["seed"]), (13, 55 + i))
            receipt = job["receipt"]
            self.assertEqual(receipt["graph_marker"], prompt)
            self.assertEqual(receipt["source"], {"transport": "cli", "by": "my connector"})
            self.assertEqual(receipt["request"], request)
            self.assertIsNone(receipt["review"])
            self.assertEqual(receipt["execution"]["prompt_id"], job["id"])
            self.assertIsNone(receipt["execution"]["started_at"])
            self.assertEqual(dt.datetime.fromisoformat(receipt["execution"]["submitted_at"]).utcoffset(), dt.timedelta(0))
        self.assertEqual(ca.main(["wait"]), 0)
        made = ca.load_state()["made"]
        self.assertEqual(len(made), 4)
        self.assertEqual(sorted(m["receipt"]["execution"]["prompt_id"] for m in made.values()), ["1", "1", "2", "2"])
        for m in made.values():
            rec = m["receipt"]
            ex = rec["execution"]
            self.assertEqual(ex["devices"], ["test GPU"])
            self.assertNotIn("irrelevant-system-details", json.dumps(rec))
            for key, value in {"queued_at": 1700000000, "started_at": 1700000001.75,
                               "completed_at": 1700000004.5}.items():
                self.assertEqual(dt.datetime.fromisoformat(ex[key]).timestamp(), value)
            self.assertEqual((ex["queue_seconds"], ex["run_seconds"]), (1.75, 2.75))

    def test_mcp_uses_actual_transport_and_only_explicit_attribution(self):
        self.stats_available = self.times_available = False
        for transport in ("mcp-stdio", "mcp-http"):
            args = {"recipe": "test", "settings": {"prompt": "this is not a user request"}, "transport": "ignore-me"}
            if transport == "mcp-http":
                args.update(by="my phone connector", request="Make a fox.")
            msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "generate", "arguments": args}}
            reply = ca.mcp_handle(msg, transport=transport)["result"]
            self.assertNotIn("isError", reply)
            rel = ca.rel_out(ca.load_state()["last"][0])
            rec = ca.load_state()["made"][rel]["receipt"]
            self.assertEqual(rec["source"], {"transport": transport, "by": args.get("by")})
            self.assertEqual(rec["request"], args.get("request"))
            for field in ("queued_at", "started_at", "completed_at", "queue_seconds", "run_seconds"):
                self.assertIsNone(rec["execution"][field])
            self.assertEqual(rec["execution"]["devices"], [])

    def test_caller_label_rejects_sensitive_or_unbounded_values_before_queueing(self):
        for label in ("https://invalid.example/mcp/test-value", "Authorization: example", "Bearer test-value",
                      "Basic test-value", "some\nname", "name" * 30, "192.0.2.1"):
            with self.subTest(label=label), self.assertRaises(ca.Fail) as error:
                ca.queue_jobs(ca.get_recipe("test"), [("prompt", "fox")], by=label)
            self.assertNotIn(label, str(error.exception))
        self.assertEqual(self.stats_calls, 0)
        self.assertEqual(self.graphs_sent, [])

    def test_variation_needs_finished_good_and_does_not_fulfil_a_requested_redo(self):
        rel = "test/20261125-120512-01.png"
        src = ca.out_dir() / rel
        src.parent.mkdir(parents=True)
        src.write_bytes(fixtures.PNG)
        old = {"recipe": "test", "given": {"prompt": "sleepy fox", "width": "768", "seed": "11"},
               "seed": 11, "sets": [["7.steps", "20"], ["7.seed", "11"]]}
        bad_rel = rel.replace("-01.png", "-02.png")
        (ca.out_dir() / bad_rel).write_bytes(fixtures.PNG)
        ca.save_state({"made": {rel: old, bad_rel: old}, "pending": [], "last": [str(src)]})
        path = ca.out_dir() / ".comfy-agent-marks.json"
        marks = {"files": {rel: {"mark": "good", "note": "snow", "redo": True}, bad_rel: {"mark": "bad"}}}
        path.write_text(json.dumps(marks))
        with self.assertRaisesRegex(ca.Fail, "finished review"):
            ca.queue_redo(rel, [], variation=True)
        run = ca.result_run(rel, old)
        done = "2026-11-25T14:00:00+00:00"
        marks["runs"] = {run: {"done_at": done, "files": [rel, bad_rel]}}
        path.write_text(json.dumps(marks))
        with self.assertRaisesRegex(ca.Fail, "Good result"):
            ca.queue_redo(bad_rel, [], variation=True)
        self.assertEqual(self.graphs_sent, [])
        with mock.patch.object(ca.random, "randint", return_value=42):
            self.assertEqual(ca.main(["redo", rel, "--variation", "--no-wait", "--by", "review desk"]), 0)
        variation = ca.load_state()["pending"][0]
        origin = variation["receipt"]["review"]
        self.assertEqual(origin, {"kind": "variation", "run": run, "file": rel, "mark": "good", "note": "snow",
                                  "requested_redo": False, "finished_at": done})
        self.assertNotIn("redo_of", variation)
        self.assertEqual(variation["receipt"]["source"]["by"], "review desk")
        self.assertEqual((variation["seed"], variation["given"]["width"], variation["sets"]), (42, "768", [["7.steps", "20"]]))
        self.assertEqual(self.graphs_sent[-1]["7"]["inputs"]["seed"], 42)
        self.assertTrue(ca.load_marks()["files"][rel]["redo"])
        ca.mcp_call("generate", {"redo": rel, "request": "Change the light."}, previews=False, transport="mcp-http")
        redo = ca.load_state()["made"][ca.rel_out(ca.load_state()["last"][0])]
        self.assertEqual(redo["receipt"]["review"], {**origin, "kind": "redo", "requested_redo": True})
        self.assertEqual(redo["redo_of"], rel)
        self.assertEqual(json.loads(path.read_text()), marks)

    def test_redo_leaves_marks_untouched_and_variation_requires_a_finished_good_pick(self):
        self.assertEqual(ca.main(["run", "test", "prompt=fox"]), 0)
        rel = ca.rel_out(ca.load_state()["last"][0])
        run = ca.load_state()["made"][rel]["run"]
        path = ca.out_dir() / ".comfy-agent-marks.json"
        redo = ca.queue_redo(rel, [])[0]["receipt"]["review"]
        self.assertIsNone(redo["mark"])
        self.assertIsNone(redo["note"])
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ca.Fail, "Good result"):
            ca.queue_redo(rel, [], variation=True)

        marks = {"runs": {run: {"done_at": "2026-11-25T14:00:00+00:00", "files": []}},
                 "files": {rel: {"mark": "good", "redo": True}}}
        path.write_text(json.dumps(marks))
        with self.assertRaisesRegex(ca.Fail, "Good result"):
            ca.queue_redo(rel, [], variation=True)
        marks["runs"][run]["files"].append(rel)
        path.write_text(json.dumps(marks, indent=2))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(ca.main(["redo", rel, "--no-wait"]), 0)
        self.assertTrue(ca.load_state()["pending"][-1]["receipt"]["review"]["requested_redo"])
        self.assertEqual(ca.main(["redo", rel, "--no-wait"]), 0)  # one request, one answer
        self.assertFalse(ca.load_state()["pending"][-1]["receipt"]["review"]["requested_redo"])
        self.assertEqual(ca.main(["redo", rel, "--variation"]), 0)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(list(path.parent.glob(".comfy-agent-marks.*")), [path])

    def test_gallery_settings_point_to_the_review_add_on(self):
        config = ca.out_dir().parent / "comfy-agent.local.json"
        config.write_text(json.dumps({"gallery_port": 8766, "send": {}}))
        with mock.patch.object(ca, "CONFIG", config), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertNotEqual(ca.main(["config", "gallery_port=8888"]), 0)
            self.assertEqual(ca.main(["config"]), 0)
        self.assertIn("comfy-agent-review", err.getvalue())
        self.assertEqual(json.loads(config.read_text()), {"gallery_port": 8766, "send": {}})

    def test_old_pending_jobs_collect_without_inventing_receipts(self):
        pending = [{"id": "old", "n": 1, "of": 1, "recipe": "test", "stamp": "20251010-120000"}]
        ca.save_state({"pending": pending, "last": []})
        self.assertEqual(ca.main(["wait"]), 0)
        self.assertEqual(ca.load_state()["pending"], [])
        self.assertTrue(ca.load_state()["made"])
        self.assertTrue(all("receipt" not in m for m in ca.load_state()["made"].values()))


if __name__ == "__main__":
    unittest.main()
