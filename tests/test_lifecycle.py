"""The GPU handoff (docs/lifecycle.md) against fake servers: acceptance checks A2-A11 of the milestone 1 plan.

Everything runs in the cloud: a fake ComfyUI that renders in time and keeps memory books, and a fake
llama.cpp router / llama-swap sharing those books. No GPU. Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("COMFY_AGENT_POLL", "0.2")
os.environ.setdefault("COMFY_AGENT_STALE", "3")
os.environ.setdefault("COMFY_AGENT_RESOLVE_TRIES", "4")
os.environ.setdefault("COMFY_AGENT_PROGRESS_EVERY", "0.5")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import comfy_agent as ca  # noqa: E402
import comfy_lifecycle as cl  # noqa: E402
import fakes  # noqa: E402

ca.POLL, ca.STALE = 0.2, 3.0


def run_args(recipe, *values, count=1, no_wait=False, timeout=60, send=None):
    return ca.argparse.Namespace(recipe=recipe, values=list(values), count=count, set=None, no_wait=no_wait,
                                 send=send, free=False, timeout=timeout)


def cleanup(tmp):
    """Close the journals first: Windows can't delete a file that is still open."""
    for j in ca._journals.values():
        j.close()
    ca._journals.clear()
    try:
        tmp.cleanup()
    except OSError:
        import shutil
        shutil.rmtree(tmp.name, ignore_errors=True)


class Base(unittest.TestCase):
    adapter = "llama-router"

    def setUp(self):
        self.world = fakes.World()
        comfy_h, router_h, swap_h = fakes.make_handler(self.world)
        self.swap_h = swap_h
        self.servers = []
        for h in (comfy_h, router_h, swap_h):
            server, url = fakes.serve(h)
            self.servers.append((server, url))
        self.comfy_url, self.router_url, self.swap_url = (u for _, u in self.servers)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["COMFY_AGENT_LEASE_DIR"] = str(root / "lease")
        self.saved = dict(ca.S)
        ca.S.update(url=self.comfy_url, out=str(root / "out"), send={})
        ca.S.pop("unload_llm", None)
        ca._node_info.clear()
        ca._journals.clear()
        self.recipe = root / "recipes" / "test"
        self.recipe.mkdir(parents=True)
        (self.recipe / "card.json").write_text(json.dumps(fakes.CARD))
        (self.recipe / "prompt.json").write_text(json.dumps(fakes.PROMPT))
        ca.S["recipes"] = [str(self.recipe.parent)]
        self.set_adapter(self.adapter)

    def set_adapter(self, kind):
        url = self.swap_url if kind == "llama-swap" else self.router_url
        ca.S["inference"] = {"type": kind, "url": url, "unload_timeout": 3, "free_timeout": 2, "load_timeout": 5,
                             "idle_timeout": 15, "margin_mb": 256} if kind != "none" else {}

    def tearDown(self):
        for p in list(ca._children):
            if p.poll() is None:
                p.wait(timeout=30)
        self.world.stop.set()
        for server, _ in self.servers:
            server.shutdown()
            server.server_close()
        for j in ca._journals.values():
            j.close()
        ca._journals.clear()
        ca.S.clear()
        ca.S.update(self.saved)
        cleanup(self.tmp)

    # helpers
    def out(self, fn, *a, **kw):
        buf = io.StringIO()
        with ca.contextlib.redirect_stdout(buf):
            try:
                fn(*a, **kw)
            except ca.Fail as e:
                buf.write(f"\n[Fail] {e}")
        return buf.getvalue()

    def op(self, op_id=None):
        j = ca.journal()
        return j.op(op_id) if op_id else j.ops(limit=1)[0]

    def wait_for(self, cond, timeout=20, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def wait_op(self, op_id, timeout=30):
        self.wait_for(lambda: self.op(op_id)["state"] in cl.OP_FINAL, timeout, f"operation {op_id} to end")
        return self.op(op_id)

    def events(self, what):
        return [e for e in self.world.events if e["what"] == what]

    def assert_clean(self):
        """No duplicate prompt, and the model never loaded while ComfyUI was busy or into a full card."""
        sent = [s for s in self.world.submits if s]
        self.assertEqual(len(sent), len(set(sent)), "a prompt was submitted twice")
        self.assertEqual(self.world.violations, [])
        self.assertFalse(cl.Lease(ca.lease_dir()).held_by_anyone(), "the lease was not released")


class HappyPath(Base):
    def test_managed_run_unloads_renders_frees_and_restores_in_order(self):
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=a fox", "seed=5", count=2))
        self.assertIn("as operation", text)
        self.assertIn("unloaded the language model (qwen), 14.6 GB freed", text)
        self.assertIn("done 1/2, seed 5", text)
        self.assertIn("done 2/2, seed 6", text)
        self.assertIn("restored the language model (qwen)", text)
        op = self.op()
        self.assertEqual(op["state"], "done")
        self.assertIs(self.world.llm_loaded["qwen"], True)
        order = [e["what"] for e in self.world.events if e["what"] in ("llm-unload", "submit", "end", "free", "llm-load")]
        self.assertEqual(order, ["llm-unload", "submit", "submit", "end", "end", "free", "llm-load"])
        self.assert_clean()
        d = op["data"]
        self.assertEqual(set(d["phases"]), {"lease", "unload", "render", "idle", "free", "restore"})
        self.assertEqual(d["vram_mb"]["before_unload"], 9000)
        self.assertEqual(d["vram_mb"]["after_unload"], 24000)
        files = ca.load_state()["last"]
        self.assertEqual(len(files), 2)
        self.assertTrue(all(Path(f).read_bytes() == fakes.PNG for f in files))
        js = json.loads(self.out(ca.cmd_ops, ca.argparse.Namespace(op=op["id"], json=True, limit=10)))
        self.assertEqual(js["state"], "done")
        self.assertEqual([x["state"] for x in js["jobs"]], ["done", "done"])
        self.assertTrue(js["jobs"][0]["prompt_id"])

    def test_nothing_loaded_means_nothing_to_unload_or_restore(self):
        self.world.llm_loaded["qwen"] = False
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertNotIn("unloaded", text)
        self.assertEqual(self.events("llm-load"), [])
        self.assertEqual(self.op()["state"], "done")

    def test_validation_failure_unloads_nothing(self):
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "colour=red"))
        self.assertIn("has no setting 'colour'", text)
        self.assertEqual(self.events("llm-unload"), [])
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x", "image=/nope.png"))
        self.assertEqual(self.events("llm-unload"), [])


class Timeouts(Base):
    def test_a2_generate_timeout_keeps_model_unloaded_then_results_returns_files_once(self):
        self.world.render_time = 2.5
        os.environ["COMFY_AGENT_MCP_WAIT"] = "1"
        try:
            res = ca.mcp_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "generate", "arguments": {"recipe": "test", "settings": {"prompt": "x"}}}})["result"]
        finally:
            del os.environ["COMFY_AGENT_MCP_WAIT"]
        text = res["content"][0]["text"]
        self.assertNotIn("isError", res)
        self.assertIn("still rendering: operation", text)
        self.assertIs(self.world.llm_loaded["qwen"], False)  # stays unloaded while ComfyUI renders
        res = ca.mcp_handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "results", "arguments": {}}})["result"]
        self.assertIn("done 1/1", res["content"][0]["text"])
        self.assertIn("restored the language model", res["content"][0]["text"])
        self.assertEqual([c["type"] for c in res["content"]], ["text", "image"])
        self.assertIs(self.world.llm_loaded["qwen"], True)
        again = ca.mcp_handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "results", "arguments": {}}})["result"]
        self.assertEqual(again["content"][0]["text"], "No jobs are waiting.")
        self.assert_clean()

    def test_a2_stdio_server_answers_while_generate_runs_and_honours_cancel(self):
        self.world.render_time = 2.0
        r_fd, w_fd = os.pipe()
        stdin, writer = os.fdopen(r_fd, "r"), os.fdopen(w_fd, "w")
        out = io.StringIO()
        lock = threading.Lock()

        class Out(io.StringIO):
            def write(self, s):
                with lock:
                    return out.write(s)

        real = Out()
        old = sys.stdout
        sys.stdout = ca._ThreadOut(old)
        t = threading.Thread(target=ca.mcp_stdio, args=(stdin, real), daemon=True)
        t.start()

        def send(obj):
            writer.write(json.dumps(obj) + "\n")
            writer.flush()

        def replies():
            with lock:
                return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "generate", "arguments": {"recipe": "test", "settings": {"prompt": "x"}},
                "_meta": {"progressToken": "tok"}}})
            time.sleep(0.4)
            send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            self.wait_for(lambda: any(r.get("id") == 2 for r in replies()), 5, "ping reply")
            self.wait_for(lambda: any(r.get("method") == "notifications/progress" for r in replies()), 5, "progress")
            send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "results", "arguments": {}}})
            self.wait_for(lambda: any(r.get("id") == 3 for r in replies()), 30, "results reply")
            got = [r for r in replies() if r.get("id") == 3][0]["result"]["content"][0]["text"]
            self.assertIn("done 1/1", got)
            time.sleep(0.5)
            self.assertFalse(any(r.get("id") == 1 for r in replies()), "a cancelled request got an answer")
            prog = [r["params"]["progress"] for r in replies() if r.get("method") == "notifications/progress"]
            self.assertEqual(prog, sorted(prog))
        finally:
            writer.close()
            t.join(timeout=5)
            stdin.close()
            sys.stdout = old
        self.assert_clean()

    def test_generate_in_managed_mode_follows_through_the_restore_by_default(self):
        res = ca.mcp_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "generate", "arguments": {"recipe": "test", "settings": {"prompt": ["a", "b"]}}}})["result"]
        text = res["content"][0]["text"]
        self.assertIn("done 2/2", text)
        self.assertIn("restored the language model", text)
        self.assertIs(self.world.llm_loaded["qwen"], True)


class Crashes(Base):
    def test_a3_killed_runner_is_reconciled_without_resubmitting(self):
        self.world.render_time = 1.5
        with ca.contextlib.redirect_stdout(io.StringIO()):
            op_id = ca.start_managed(ca.get_recipe(str(self.recipe)), [("prompt", "x")], 2)
        self.wait_for(lambda: len(self.events("submit")) == 2, 10, "both submits")
        proc = ca._children[-1]
        proc.kill()  # SIGKILL on Linux and macOS, TerminateProcess on Windows
        proc.wait()
        self.assertEqual(self.op(op_id)["state"], "rendering")
        self.assertTrue(ca.runner_dead(self.op(op_id)))
        self.assertEqual(ca.reconcile(), [op_id])
        op = self.wait_op(op_id)
        self.assertEqual(op["state"], "done")
        self.assertEqual(len(self.world.submits), 2)
        self.assertEqual([x["state"] for x in ca.journal().jobs(op_id)], ["done", "done"])
        self.assertIs(self.world.llm_loaded["qwen"], True)
        self.assert_clean()

    def test_a3_killed_caller_does_not_stop_the_render(self):
        """The runner is its own process: a caller that dies (an MCP server killed by its client) loses nothing."""
        self.world.render_time = 0.8
        with ca.contextlib.redirect_stdout(io.StringIO()):
            op_id = ca.start_managed(ca.get_recipe(str(self.recipe)), [("prompt", "x")], 1)
        # nobody follows it
        op = self.wait_op(op_id)
        self.assertEqual(op["state"], "done")
        self.assertIs(self.world.llm_loaded["qwen"], True)
        text = self.out(ca.cmd_wait, ca.argparse.Namespace(timeout=10, send=None, free=False))
        self.assertIn("done 1/1", text)


class Batches(Base):
    def test_a4_partial_batch_collects_what_was_accepted(self):
        self.world.refuse_nth = 3
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x", count=4))
        self.assertIn("done 1/4", text)
        self.assertIn("done 2/4", text)
        self.assertIn("FAILED 3/4: ComfyUI refused the job", text)
        self.assertIn("skipped 4/4", text)
        op = self.op()
        self.assertEqual(op["error"], "ComfyUI refused job 3/4")
        self.assertEqual([x["state"] for x in ca.journal().jobs(op["id"])], ["done", "done", "failed", "skipped"])
        self.assertIs(self.world.llm_loaded["qwen"], True)
        self.assert_clean()

    def test_a4_unmanaged_partial_batch_too(self):
        self.set_adapter("none")
        self.world.refuse_nth = 2
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x", count=3))
        self.assertIn("FAILED 2/3", text)
        self.assertIn("done 1/3", text)
        self.assertIn("[Fail] ComfyUI refused job 2/3", text)

    def test_a5_lost_answer_after_accept_is_matched_not_resent(self):
        self.world.drop_nth = (1, True)
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x", count=2))
        self.assertIn("done 1/2", text)
        self.assertIn("done 2/2", text)
        self.assertEqual(len(self.world.submits), 2)
        self.assertEqual(len(self.world.history), 2)
        self.assert_clean()

    def test_a5_lost_answer_without_accept_is_reported_not_resent(self):
        self.world.drop_nth = (1, False)
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertIn("FAILED 1/1: ComfyUI may not have received this job", text)
        self.assertEqual(len(self.world.submits), 1)
        self.assertIs(self.world.llm_loaded["qwen"], True)
        self.assert_clean()

    def test_a5_unmanaged_lost_answer(self):
        self.set_adapter("none")
        self.world.drop_nth = (1, True)
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertIn("done 1/1", text)
        self.assertEqual(len(self.world.submits), 1)


class Memory(Base):
    def test_a6_memory_not_released_blocks_instead_of_loading(self):
        self.world.comfy_free_delay = None
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertIn("BLOCKED: memory not released (13.7 GB free, need 14.9 GB)", text)
        op = self.op()
        self.assertEqual(op["state"], "blocked")
        self.assertEqual(self.events("llm-load"), [])
        self.assertIs(self.world.llm_loaded["qwen"], False)
        self.assert_clean()
        # memory comes back; reconcile restores
        with self.world.lock:
            self.world.comfy_used = 0
        text = self.out(ca.cmd_reconcile, ca.argparse.Namespace(no_wait=False, timeout=30))
        self.assertIn("restored the language model (qwen)", text)
        self.assertEqual(self.op(op["id"])["state"], "done")
        self.assert_clean()

    def test_a6_slow_free_waits_for_headroom(self):
        self.world.comfy_free_delay = 1.2
        self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        freed = self.events("comfy-freed")[0]["t"]
        load = self.events("llm-load")[0]["t"]
        self.assertGreaterEqual(load, freed)
        self.assertEqual(self.op()["state"], "done")
        self.assert_clean()

    def test_next_operation_restores_a_blocked_one(self):
        self.world.comfy_free_delay = None
        self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        first = self.op()
        self.assertEqual(first["state"], "blocked")
        self.world.comfy_free_delay = 0.1
        with self.world.lock:
            self.world.comfy_used = 0
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=y"))
        self.assertIn("restored the language model (qwen)", text)
        self.assertEqual(self.op(first["id"])["state"], "done")
        self.assertIs(self.world.llm_loaded["qwen"], True)

    def test_a_gpu_comfyui_cant_measure_skips_the_check_and_says_so(self):
        self.world.device_type = "privateuseone"  # DirectML reports a placeholder, not free memory
        self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        op = self.op()
        self.assertEqual(op["state"], "done")
        self.assertTrue(any(n.startswith("no free-memory check") for n in op["data"]["notes"]), op["data"])
        self.assertIs(self.world.llm_loaded["qwen"], True)

    def test_free_mb_takes_off_what_pytorch_reserved(self):
        stats = {"devices": [{"type": "cuda", "vram_free": 10 * fakes.MiB * 1024, "torch_vram_free": 2 * fakes.MiB * 1024}]}
        self.assertEqual(cl.free_mb(stats), 8 * 1024)
        self.assertIsNone(cl.free_mb({"devices": [{"type": "cpu", "vram_free": 1}]}))
        self.assertIsNone(cl.free_mb({"devices": []}))
        self.assertIsNone(cl.free_mb({"devices": [{"type": "privateuseone", "vram_free": 1 << 30}]}))


class Sharing(Base):
    def test_a7_two_operations_take_turns_with_one_unload_and_load_each(self):
        self.world.render_time = 0.8
        outs = [io.StringIO(), io.StringIO()]

        # contextlib.redirect_stdout is process-wide, so route by thread like the MCP server does
        old = sys.stdout
        sys.stdout = ca._ThreadOut(old)
        try:
            threads = []
            for i in range(2):
                def worker(i=i):
                    sys.stdout.local.buf = outs[i]
                    ca.cmd_run(run_args(str(self.recipe), f"prompt=p{i}"))
                threads.append(threading.Thread(target=worker))
            for t in threads:
                t.start()
                time.sleep(0.15)
            for t in threads:
                t.join(60)
        finally:
            sys.stdout = old
        seq = [e["what"] for e in self.world.events if e["what"] in ("llm-unload", "llm-load")]
        self.assertEqual(seq, ["llm-unload", "llm-load", "llm-unload", "llm-load"])
        both = outs[0].getvalue() + outs[1].getvalue()
        self.assertIn("waiting for the GPU: operation", both)
        self.assertEqual(both.count("restored the language model"), 2)
        self.assert_clean()

    def test_two_output_folders_share_one_lease(self):
        self.world.render_time = 0.8
        recipe = ca.get_recipe(str(self.recipe))
        outs = [str(Path(self.tmp.name) / "out-a"), str(Path(self.tmp.name) / "out-b")]
        ids = []
        for out in outs:
            ca.S["out"] = out
            with ca.contextlib.redirect_stdout(io.StringIO()):
                ids.append(ca.start_managed(recipe, [("prompt", "x")], 1))
            time.sleep(0.3)
        for out, op_id in zip(outs, ids):
            ca.S["out"] = out
            self.assertEqual(self.wait_op(op_id)["state"], "done")
        second = self.op(ids[1])["data"]["events"]
        self.assertTrue(any(f"waiting for the GPU: operation {ids[0]} (output folder {outs[0]})" in e[1]
                            for e in second), second)
        seq = [e["what"] for e in self.world.events if e["what"] in ("llm-unload", "llm-load")]
        self.assertEqual(seq, ["llm-unload", "llm-load", "llm-unload", "llm-load"])
        self.assert_clean()

    def test_a_request_that_loads_the_model_mid_render_is_undone(self):
        """The router obeys ?autoload=true even with --no-models-autoload; the runner unloads it again."""
        self.world.render_time = 3.0

        def intruder():
            self.wait_for(lambda: self.events("start"), 10, "render to start")
            time.sleep(0.3)
            with self.world.lock:
                self.world.llm_loaded["qwen"] = True
            self.world.log("intrusion")
        threading.Thread(target=intruder, daemon=True).start()
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertIn("a request loaded the language model (qwen) while ComfyUI had the GPU; unloaded it again", text)
        intruded = self.events("intrusion")[0]["t"]
        undone = [e["t"] for e in self.events("llm-unload") if e["t"] > intruded]
        self.assertTrue(undone and undone[0] - intruded < 3, "the intrusion was not undone within 3 s")
        self.assertLess(undone[0], self.events("end")[0]["t"], "undone only after the render ended")
        self.assertIn("intrusion: qwen loaded during the lease", self.op()["data"]["notes"])
        self.assertEqual(self.op()["state"], "done")
        self.assertIs(self.world.llm_loaded["qwen"], True)
        self.assert_clean()

    def test_a8_browser_job_running_at_reload_time_delays_the_reload(self):
        self.world.render_time = 0.6
        done = []

        def browser():
            self.wait_for(lambda: self.events("submit"), 10, "our submit")
            self.world.add({"browser": True}, duration=2.0)
            done.append(True)
        threading.Thread(target=browser, daemon=True).start()
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertTrue(done)
        self.assertIn("waiting for ComfyUI's queue to empty", text)
        last_end = max(e["t"] for e in self.events("end"))
        self.assertGreaterEqual(self.events("llm-load")[0]["t"], last_end)
        self.assert_clean()

    def test_a9_cancel_removes_our_jobs_then_restores(self):
        self.world.render_time = 3.0
        with ca.contextlib.redirect_stdout(io.StringIO()):
            op_id = ca.start_managed(ca.get_recipe(str(self.recipe)), [("prompt", "x")], 3)
        self.wait_for(lambda: self.events("start"), 10, "first job to start")
        self.out(ca.cmd_cancel, ca.argparse.Namespace(op=op_id))
        op = self.wait_op(op_id)
        self.assertEqual(op["state"], "cancelled")
        states = [x["state"] for x in ca.journal().jobs(op_id)]
        self.assertEqual(states[0], "cancelled")  # it was running: interrupted
        self.assertTrue(set(states) <= {"cancelled", "skipped"}, states)  # the rest: removed, or never sent
        self.assertTrue(self.events("interrupt"))
        self.assertTrue(self.events("delete"))
        self.assertFalse(self.world.busy())
        self.assertIs(self.world.llm_loaded["qwen"], True)
        self.assert_clean()

    def test_cancel_before_the_lease_unloads_nothing(self):
        self.world.render_time = 1.5
        with ca.contextlib.redirect_stdout(io.StringIO()):
            first = ca.start_managed(ca.get_recipe(str(self.recipe)), [("prompt", "x")], 1)
            self.wait_for(lambda: self.events("submit"), 10, "first submit")
            second = ca.start_managed(ca.get_recipe(str(self.recipe)), [("prompt", "y")], 1)
        self.wait_for(lambda: self.op(second)["data"].get("events"), 10, "second to wait")
        self.out(ca.cmd_cancel, ca.argparse.Namespace(op=second))
        self.assertEqual(self.wait_op(second)["state"], "cancelled")
        self.assertEqual(self.wait_op(first)["state"], "done")
        self.assertEqual(len(self.world.submits), 1)
        self.assertEqual(len(self.events("llm-unload")), 1)


class Unmanaged(Base):
    adapter = "none"

    def test_a10_no_local_model_means_no_lease_and_no_runner(self):
        children = list(ca._children)
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x", count=2))
        self.assertIn("queued 2 job(s) of test\n", text)
        self.assertIn("done 2/2", text)
        self.assertFalse((ca.out_dir() / ".gpu.lock").exists())
        self.assertFalse((ca.lease_dir() / ".gpu.lock").exists())
        self.assertFalse((ca.out_dir() / ".runs").exists())
        self.assertEqual(ca._children, children)
        self.assertEqual(self.events("llm-unload"), [])

    def test_legacy_json_state_is_imported_once(self):
        out = ca.out_dir()
        out.mkdir(parents=True)
        pid = self.world.add(fakes.PROMPT, duration=0.1)
        (out / ".comfy-agent-jobs.json").write_text(json.dumps({"pending": [
            {"recipe": "test", "n": 1, "of": 1, "stamp": "old", "settings": {}, "id": pid}], "last": ["a.png"]}))
        ca._journals.clear()
        self.assertEqual(ca.load_state()["last"], ["a.png"])
        text = self.out(ca.cmd_wait, ca.argparse.Namespace(timeout=10, send=None, free=False))
        self.assertIn("done 1/1", text)
        self.assertTrue((out / ".comfy-agent-jobs.json.imported").exists())


class Delivery(Base):
    def test_a11_send_failure_never_fails_the_render(self):
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x", send="nowhere"))
        self.assertIn("done 1/1", text)
        self.assertIn("Sending failed (the files are saved)", text)
        self.assertEqual(self.op()["state"], "done")
        self.assertTrue(Path(ca.load_state()["last"][0]).exists())
        self.assertIs(self.world.llm_loaded["qwen"], True)

    def test_a11_unmanaged_mcp_send_failure_still_returns_the_files(self):
        self.set_adapter("none")
        res = ca.mcp_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "generate", "arguments": {"recipe": "test", "settings": {"prompt": "x"}, "send": "nowhere"}}})["result"]
        self.assertNotIn("isError", res)
        self.assertIn("done 1/1", res["content"][0]["text"])
        self.assertIn("Sending failed", res["content"][0]["text"])


class Swap(Base):
    adapter = "llama-swap"

    def test_llama_swap_pause_and_resume(self):
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertIn("restored the language model (qwen)", text)
        self.assertEqual(self.events("llm-unload")[0]["via"], "/api/models/pause")
        self.assertTrue(self.events("swap-resume"))
        self.assert_clean()

    def test_older_llama_swap_falls_back_to_unload(self):
        self.swap_h.has_pause = False
        text = self.out(ca.cmd_run, run_args(str(self.recipe), "prompt=x"))
        self.assertIn("restored the language model (qwen)", text)
        self.assertEqual(self.events("llm-unload")[0]["via"], "/api/models/unload")
        self.assert_clean()


class Pieces(unittest.TestCase):
    def test_lease_is_exclusive_across_handles_and_reports_its_holder(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = cl.Lease(d), cl.Lease(d)
            self.assertTrue(a.acquire("op-a"))
            self.assertTrue(b.held_by_anyone())
            self.assertEqual(b.holder()["op"], "op-a")
            self.assertFalse(b.acquire("op-b", timeout=0.3))
            a.release()
            self.assertFalse(b.held_by_anyone())
            self.assertTrue(b.acquire("op-b", timeout=1))
            b.release()

    def test_lease_dir_is_per_computer_by_default(self):
        old = {k: os.environ.pop(k, None) for k in ("COMFY_AGENT_LEASE_DIR", "XDG_STATE_HOME")}
        try:
            d = cl.default_lease_dir()
            if os.name == "nt":
                self.assertEqual(d.name, "comfy-agent")
            elif sys.platform == "darwin":
                self.assertEqual(d, Path.home() / "Library" / "Application Support" / "comfy-agent")
            else:
                self.assertEqual(d, Path.home() / ".local" / "state" / "comfy-agent")
                os.environ["XDG_STATE_HOME"] = "/x/state"
                self.assertEqual(cl.default_lease_dir(), Path("/x/state/comfy-agent"))
            os.environ["COMFY_AGENT_LEASE_DIR"] = "/somewhere"
            self.assertEqual(cl.default_lease_dir(), Path("/somewhere"))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_pid_alive(self):
        self.assertTrue(cl.pid_alive(os.getpid()))
        p = ca.subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(cl.pid_alive(p.pid))
        self.assertFalse(cl.pid_alive(None))

    def test_unknown_adapter_is_refused(self):
        with self.assertRaises(cl.AdapterError):
            cl.make_adapter({"type": "ollama-magic"})

    def test_config_sets_inference_keys(self):
        with tempfile.TemporaryDirectory() as d:
            old = ca.CONFIG
            ca.CONFIG = Path(d) / "c.json"
            try:
                with ca.contextlib.redirect_stdout(io.StringIO()):
                    ca.cmd_config(ca.argparse.Namespace(values=["inference.type=llama-router", "inference.need_mb=15000",
                                                                "inference.model=qwen38-16g"]))
                cfg = json.loads(ca.CONFIG.read_text())["inference"]
                self.assertEqual(cfg, {"type": "llama-router", "need_mb": 15000, "model": "qwen38-16g"})
                with self.assertRaises(ca.Fail):
                    ca.cmd_config(ca.argparse.Namespace(values=["inference.type=bogus"]))
            finally:
                ca.CONFIG = old


if __name__ == "__main__":
    unittest.main()
