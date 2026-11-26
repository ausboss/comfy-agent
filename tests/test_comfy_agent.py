"""Tests for comfy_agent.py against a fake ComfyUI server. Run: python3 -m unittest discover -s tests"""
import argparse
import base64
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import comfy_agent as ca  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

OBJECT_INFO = {
    "UNETLoader": {"input": {"required": {"unet_name": [["sub/model-a.safetensors", "other.safetensors"]]}},
                   "python_module": "nodes"},
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}},
                       "python_module": "nodes"},
    "KSampler": {"input": {"required": {"seed": ["INT", {}], "steps": ["INT", {}], "model": ["MODEL"]}},
                 "python_module": "nodes"},
    "EmptyLatentImage": {"input": {"required": {"width": ["INT", {}], "height": ["INT", {}], "batch_size": ["INT", {}]}},
                         "python_module": "nodes"},
    "LoadImage": {"input": {"required": {"image": ["COMBO", {"options": ["example.png"], "image_upload": True}]}},
                  "python_module": "nodes"},
    "LoraText": {"input": {"required": {"loras": ["STRING", {"multiline": True}]}}, "python_module": "nodes"},
    "PrimitiveFloat": {"input": {"required": {"value": ["FLOAT", {}]}}, "python_module": "comfy_extras.nodes_primitive"},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "python_module": "nodes"},
}


class Fake(BaseHTTPRequestHandler):
    prompts, history, uploads, reboots, frees = {}, {}, [], [], []
    down = 0

    def reply(self, obj, status=200, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/object_info/"):
            cls = path.rsplit("/", 1)[1]
            return self.reply({cls: OBJECT_INFO[cls]} if cls in OBJECT_INFO else {})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            return self.reply({pid: Fake.history[pid]} if pid in Fake.history else {})
        if path == "/view":
            return self.reply(None, raw=PNG)
        if path == "/queue":
            return self.reply({"queue_running": [], "queue_pending": [[0, p] for p in Fake.prompts if p not in Fake.history]})
        if path == "/system_stats":
            if Fake.down:
                Fake.down -= 1
                return self.reply({}, 503)
            return self.reply({"system": {"comfyui_version": "test"}, "devices": []})
        self.reply({}, 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/upload/image":
            Fake.uploads.append(body)
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            return self.reply({"name": name, "subfolder": "", "type": "input"})
        if self.path == "/prompt":
            prompt = json.loads(body)["prompt"]
            if any(n["class_type"] == "Broken" for n in prompt.values()):
                return self.reply({"error": {"message": "Prompt outputs failed validation"},
                                   "node_errors": {"9": {"class_type": "Broken", "errors": [
                                       {"message": "Value not in list", "details": "x: 'a' not in [" + "b, " * 500 + "]"}]}}}, 400)
            pid = str(uuid.uuid4())
            Fake.prompts[pid] = prompt
            Fake.history[pid] = {"status": {"status_str": "success", "completed": True, "messages": []},
                                 "outputs": {"9": {"images": [{"filename": f"{pid}.png", "subfolder": "", "type": "output"}]}}}
            return self.reply({"prompt_id": pid})
        if self.path == "/free":
            Fake.frees.append(json.loads(body))
            return self.reply({})
        if self.path == "/v2/manager/reboot":
            Fake.reboots.append("POST " + self.path)
            Fake.down = 2
            return self.reply({})
        self.reply({}, 404)

    def log_message(self, *a):
        pass


PROMPT = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "model-a.safetensors"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "an apple", "clip": ["2", 0]}, "_meta": {"title": "Positive"}},
    "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    "7": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 8, "model": ["1", 0]}},
    "8": {"class_type": "LoadImage", "inputs": {"image": "example.png"}, "_meta": {"title": "Load Image"}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
}
CARD = {"title": "Test", "inputs": {
    "prompt": {"set": "4.text", "required": True, "help": "what to see"},
    "seed": {"set": ["7.seed"], "default": "random"},
    "width": {"set": "6.width", "default": 1024},
    "image": {"set": "8.image", "type": "image", "default": "example.png"}}}


class ComfyAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        config = mock.patch.dict(ca.S, {"url": f"http://127.0.0.1:{cls.server.server_port}"}, clear=True)
        config.start()
        cls.addClassCleanup(config.stop)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        ca.S["out"] = str(root / "out")
        ca.S["send"] = {}
        ca._node_info.clear()
        self.recipe_dir = root / "recipes" / "test"
        self.recipe_dir.mkdir(parents=True)
        (self.recipe_dir / "card.json").write_text(json.dumps(CARD))
        (self.recipe_dir / "prompt.json").write_text(json.dumps(PROMPT))
        Fake.prompts.clear()
        Fake.uploads.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def recipe(self):
        return ca.get_recipe(str(self.recipe_dir))

    def test_plan_jobs_pairs_repeated_settings_and_counts(self):
        jobs = ca.plan_jobs(self.recipe(), [("prompt", "a"), ("prompt", "b"), ("width", "512")], 2)
        self.assertEqual([j["prompt"] for j in jobs], ["a", "a", "b", "b"])
        self.assertTrue(all(j["width"] == "512" for j in jobs))

    def test_required_setting_is_enforced(self):
        with self.assertRaisesRegex(ca.Fail, "needs prompt"):
            ca.plan_jobs(self.recipe(), [], 1)

    def test_unknown_setting_lists_the_real_ones(self):
        with self.assertRaisesRegex(ca.Fail, "prompt, seed, width, image"):
            ca.plan_jobs(self.recipe(), [("prompt", "a"), ("colour", "red")], 1)

    def test_run_queues_fills_and_saves(self):
        jobs = ca.queue_jobs(self.recipe(), [("prompt", "a fox"), ("seed", "5"), ("width", "768")], 3)
        self.assertEqual([j["seed"] for j in jobs], [5, 6, 7])
        sent = [Fake.prompts[j["id"]] for j in jobs]
        self.assertEqual({p["4"]["inputs"]["text"] for p in sent}, {"a fox"})
        self.assertEqual([p["7"]["inputs"]["seed"] for p in sent], [5, 6, 7])
        self.assertEqual(sent[0]["6"]["inputs"]["width"], 768)
        self.assertEqual(sent[0]["1"]["inputs"]["unet_name"], "sub/model-a.safetensors")
        self.assertEqual(sent[0]["8"]["inputs"]["image"], "example.png")
        left = ca.wait_jobs(jobs, timeout=10)
        self.assertEqual(left, [])
        files = ca.load_state()["last"]
        self.assertEqual(len(files), 3)
        self.assertTrue(all(Path(f).read_bytes() == PNG for f in files))
        self.assertEqual(ca.load_state()["pending"], [])
        self.assertEqual({j["run"] for j in jobs}, {jobs[0]["run"]})
        self.assertEqual({m["run"] for m in ca.load_state()["made"].values()}, {jobs[0]["run"]})
        self.assertFalse((Path(ca.S["out"]) / "index.html").exists())

    def test_random_seeds_differ(self):
        jobs = ca.queue_jobs(self.recipe(), [("prompt", "x")], 4)
        self.assertEqual(len({j["seed"] for j in jobs}), 4)

    def test_media_is_uploaded_once(self):
        photo = Path(self.tmp.name) / "my photo.png"
        photo.write_bytes(PNG)
        jobs = ca.queue_jobs(self.recipe(), [("prompt", "x"), ("image", str(photo))], 2)
        self.assertEqual(len(Fake.uploads), 1)
        name = Fake.prompts[jobs[0]["id"]]["8"]["inputs"]["image"]
        self.assertTrue(name.endswith("-my_photo.png"), name)

    def test_missing_model_and_node_stop_before_queueing(self):
        prompt = dict(PROMPT, **{"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "nope.safetensors"}},
                                 "3": {"class_type": "NotInstalled", "inputs": {}}})
        (self.recipe_dir / "prompt.json").write_text(json.dumps(prompt))
        with self.assertRaises(ca.Fail) as e:
            ca.queue_jobs(self.recipe(), [("prompt", "x")], 1)
        self.assertIn("NotInstalled", str(e.exception))
        self.assertIn("nope.safetensors", str(e.exception))
        self.assertIn(f"{ca.cli_command()} install test", str(e.exception))
        self.assertEqual(Fake.prompts, {})

    def test_refused_job_is_explained_briefly(self):
        OBJECT_INFO["Broken"] = {"input": {"required": {}}}
        try:
            prompt = dict(PROMPT, **{"3": {"class_type": "Broken", "inputs": {}}})
            (self.recipe_dir / "prompt.json").write_text(json.dumps(prompt))
            with self.assertRaises(ca.Fail) as e:
                ca.queue_jobs(self.recipe(), [("prompt", "x")], 1)
            self.assertIn("Value not in list", str(e.exception))
            self.assertLess(len(str(e.exception)), 700)
        finally:
            del OBJECT_INFO["Broken"]

    def test_raw_set_reaches_any_input(self):
        jobs = ca.queue_jobs(self.recipe(), [("prompt", "x")], 1, [("7.steps", "20")])
        self.assertEqual(Fake.prompts[jobs[0]["id"]]["7"]["inputs"]["steps"], 20)

    def test_mcp_generate_returns_text_and_preview(self):
        ca.S["recipes"] = [str(self.recipe_dir.parent)]
        try:
            init = ca.mcp_handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            self.assertEqual(init["result"]["serverInfo"]["name"], "comfy-agent")
            tools = ca.mcp_handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["recipes", "generate", "results"])
            res = ca.mcp_handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "generate", "arguments": {"recipe": "test", "settings": {"prompt": ["a", "b"]}}}})["result"]
            self.assertNotIn("isError", res)
            self.assertIn("done 2/2", res["content"][0]["text"])
            self.assertEqual([c["type"] for c in res["content"]], ["text", "image", "image"])
            bad = ca.mcp_handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "generate", "arguments": {"recipe": "nope"}}})["result"]
            self.assertTrue(bad["isError"])
            self.assertIsNone(ca.mcp_handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        finally:
            ca.S.pop("recipes")

    def test_draft_card_finds_the_human_settings(self):
        prompt = dict(PROMPT)
        prompt["5"] = {"class_type": "LoraText", "inputs": {"loras": '[{"name": "x"}]'}}
        prompt["10"] = {"class_type": "PrimitiveFloat", "inputs": {"value": 5.0}, "_meta": {"title": "Float (Duration)"}}
        card = ca.draft_card(prompt, None, "Draft")
        self.assertEqual(card["inputs"]["prompt"]["set"], "4.text")
        self.assertEqual(card["inputs"]["seed"]["set"], ["7.seed"])
        self.assertEqual(card["inputs"]["image"]["type"], "image")
        self.assertEqual(card["inputs"]["width"]["default"], 1024)
        self.assertEqual(card["inputs"]["duration"]["set"], "10.value")
        self.assertNotIn("loras", json.dumps(card["inputs"]))
        self.assertEqual(card["needs"]["models"][0]["file"], "model-a.safetensors")

    def test_small_card_mode_unloads_the_llm_first_and_frees_comfyui_after(self):
        marker = Path(self.tmp.name) / "unloaded"
        ca.S["unload_llm"] = f"{sys.executable} -c \"open('{marker}', 'w')\""
        Fake.frees.clear()
        try:
            ca.cmd_run(ca.argparse.Namespace(recipe=str(self.recipe_dir), values=["prompt=x"], count=2, set=None,
                                             no_wait=True, send=None, free=False, timeout=10))
        finally:
            ca.S.pop("unload_llm")
        self.assertTrue(marker.exists())
        self.assertEqual(len(ca.load_state()["last"]), 2)
        self.assertEqual(Fake.frees, [{"unload_models": True, "free_memory": True}])

    def test_restart_asks_manager_and_waits_for_the_new_server(self):
        Fake.reboots.clear()
        ca.cmd_restart(None)
        self.assertEqual(Fake.reboots, ["POST /v2/manager/reboot"])
        self.assertEqual(Fake.down, 0)

    def test_node_pack_without_requirements_needs_no_python(self):
        root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "pack.git")], check=True)
        custom_nodes = root / "base" / "custom_nodes"
        custom_nodes.mkdir(parents=True)
        ca.install_pack({"repo": str(root / "pack.git")}, custom_nodes)
        self.assertTrue((custom_nodes / "pack").is_dir())

    def test_send_needs_a_known_target(self):
        with self.assertRaisesRegex(ca.Fail, "config phone="):
            ca.send_files(["x.png"], "phone")

    def make_one(self, *pairs, sets=()):
        jobs = ca.queue_jobs(self.recipe(), list(pairs), 1, sets)
        self.assertEqual(ca.wait_jobs(jobs, timeout=10), [])
        return ca.load_state()["last"][0]

    def test_redo_remakes_a_result_with_its_settings_and_note(self):
        photo = Path(self.tmp.name) / "my photo.png"
        photo.write_bytes(PNG)
        first = self.make_one(("prompt", "a fox"), ("width", "768"), ("image", str(photo)), sets=[("7.steps", "20")])
        rel = ca.rel_out(first)
        run = ca.load_state()["made"][rel]["run"]
        marks = {"files": {rel: {"mark": "bad", "note": "make it night", "redo": True}},
                 "runs": {run: {"done_at": "2026-11-25T10:00:00", "files": [rel]}}}
        (ca.out_dir() / ".comfy-agent-marks.json").write_text(json.dumps(marks))
        Fake.uploads.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            jobs = ca.queue_redo(first, [("prompt", "a fox at night")])
        sent = Fake.prompts[jobs[0]["id"]]
        self.assertEqual(sent["4"]["inputs"]["text"], "a fox at night")
        self.assertEqual(sent["6"]["inputs"]["width"], 768)
        self.assertEqual(sent["7"]["inputs"]["steps"], 20)
        self.assertTrue(sent["8"]["inputs"]["image"].endswith("-my_photo.png"))
        self.assertEqual(len(Fake.uploads), 1)
        ca.wait_jobs(jobs, timeout=10)
        made = ca.load_state()["made"][ca.rel_out(ca.load_state()["last"][0])]
        self.assertEqual((made["redo_of"], made["note"]), (rel, "make it night"))
        self.assertEqual(made["given"]["prompt"], "a fox at night")
        self.assertNotEqual(made["run"], ca.load_state()["made"][rel]["run"])

    def test_redo_needs_a_record(self):
        out = Path(ca.S["out"]) / "zimage"
        out.mkdir(parents=True)
        (out / "old.png").write_bytes(PNG)
        with self.assertRaisesRegex(ca.Fail, "no record"):
            ca.queue_redo(str(out / "old.png"), [])

    def test_redo_sends_the_new_result_back(self):
        ca.S["send"]["phone"] = "tailscale:test-device"
        first = self.make_one(("prompt", "a fox"))
        sent, real = [], ca.send_files
        ca.send_files = lambda files, target: sent.append((files, target)) or "sent"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ca.cmd_redo(argparse.Namespace(file=first, values=[], count=1, no_wait=False, send="phone",
                                               free=False, timeout=10))
        finally:
            ca.send_files = real
        new = ca.load_state()["last"][0]
        self.assertNotEqual(new, first)
        self.assertEqual(sent, [([new], "phone")])

    def test_mcp_results_collects_jobs_and_generate_can_redo(self):
        ca.S["recipes"] = [str(self.recipe_dir.parent)]
        try:
            first = self.make_one(("prompt", "a fox"))
            rel = ca.rel_out(first)
            run = ca.load_state()["made"][rel]["run"]
            note = 'more "snow"\nplease'
            marks = {"files": {rel: {"mark": "good", "note": note, "redo": "false"}},
                     "runs": {run: {"done_at": "2026-11-25T10:00:00", "files": [rel]}}}
            (ca.out_dir() / ".comfy-agent-marks.json").write_text(json.dumps(marks))
            call = lambda name, args: ca.mcp_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                    "params": {"name": name, "arguments": args}})["result"]
            pending = ca.queue_jobs(self.recipe(), [("prompt", "a second fox")])
            text = call("results", {})["content"][0]["text"]
            self.assertIn(pending[0]["run"], text)
            self.assertNotIn(run, text)
            self.assertEqual(call("results", {})["content"][0]["text"], "No jobs are waiting.")
            res = call("generate", {"redo": rel, "count": 2})
            self.assertNotIn("isError", res)
            self.assertIn(f'queued 2 redo(s) of {rel} with test (its note: {json.dumps(note)})', res["content"][0]["text"])
            self.assertEqual([c["type"] for c in res["content"]], ["text", "image", "image"])
            made = ca.load_state()["made"][ca.rel_out(ca.load_state()["last"][0])]
            self.assertIs(made["receipt"]["review"]["requested_redo"], False)
            self.assertTrue(call("generate", {})["isError"])
        finally:
            ca.S.pop("recipes")

    def test_manual_redo_does_not_use_an_unsubmitted_note(self):
        first = self.make_one(("prompt", "a fox"))
        rel = ca.rel_out(first)
        marks = {"files": {rel: {"note": "not submitted", "redo": True}}}
        (ca.out_dir() / ".comfy-agent-marks.json").write_text(json.dumps(marks))
        jobs = ca.queue_redo(first, [])
        self.assertNotIn("note", jobs[0])
        self.assertTrue(ca.load_marks()["files"][rel]["redo"])

    def test_legacy_runs_use_recipe_and_full_timestamp(self):
        self.assertEqual(ca.result_run("my recipe/20261124-232150-02-1.png", {}), "my recipe/20261124-232150")
        self.assertEqual(ca.result_run("wan/20261124-232150-01.mp4", {}), "wan/20261124-232150")
        self.assertEqual(ca.result_run("misc/my + # file.png", {}), "file:misc/my + # file.png")


if __name__ == "__main__":
    unittest.main()
