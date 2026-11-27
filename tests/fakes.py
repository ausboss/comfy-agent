"""A fake ComfyUI that renders in time and keeps GPU memory books, and fake model servers (llama.cpp router,
llama-swap) that share those books. Used by test_lifecycle.py. Nothing here touches a real GPU."""
import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
MiB = 1 << 20

OBJECT_INFO = {
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}}},
    "KSampler": {"input": {"required": {"seed": ["INT", {}], "steps": ["INT", {}], "model": ["MODEL"]}}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}},
}
PROMPT = {
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "an apple", "clip": ["2", 0]}},
    "7": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 8, "model": ["1", 0]}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
}
CARD = {"title": "Test", "inputs": {"prompt": {"set": "4.text", "required": True},
                                    "seed": {"set": ["7.seed"], "default": "random"}}}


class World:
    """Shared state: ComfyUI's queue and history, and who holds how much of a 24 GB card."""

    def __init__(self, total_mb=24000, comfy_mb=10000, llm_mb=15000):
        self.lock = threading.RLock()
        self.total, self.comfy_need, self.llm_need = total_mb, comfy_mb, llm_mb
        self.comfy_used = 0          # ComfyUI's models, kept after a render until /free
        self.comfy_free_delay = 0.2  # seconds /free takes to give memory back; None = never
        self.llm_loaded = {"qwen": True}
        self.queue, self.running, self.history = [], None, {}
        self.submits, self.events, self.violations = [], [], []
        self.render_time = 0.5
        self.refuse_nth = None       # refuse this submit (1-based) with a validation error
        self.drop_nth = None         # (n, accepted): close the connection without answering
        self.interrupt = threading.Event()
        self.stop = threading.Event()
        self.load_delay = 0.3
        threading.Thread(target=self._worker, daemon=True).start()

    def log(self, what, **kw):
        with self.lock:
            self.events.append(dict(what=what, t=time.time(), **kw))

    def busy(self):
        return bool(self.running or self.queue)

    def llm_used(self):
        return sum(self.llm_need for v in self.llm_loaded.values() if v is True)

    def free(self):
        return self.total - self.comfy_used - self.llm_used()

    def add(self, prompt, pid=None, extra=None, duration=None):
        pid = pid or str(uuid.uuid4())
        with self.lock:
            self.queue.append({"id": pid, "prompt": prompt, "extra": extra or {},
                               "duration": self.render_time if duration is None else duration})
        return pid

    def _worker(self):
        while not self.stop.is_set():
            with self.lock:
                job = self.queue.pop(0) if self.queue and not self.running else None
                if job:
                    if self.llm_used() and self.free() < self.comfy_need:
                        self.violations.append(("comfyui-oom", job["id"]))
                    self.running = job
                    self.comfy_used = self.comfy_need
                    self.interrupt.clear()
            if not job:
                time.sleep(0.02)
                continue
            self.log("start", id=job["id"])
            ok = not self.interrupt.wait(job["duration"])
            with self.lock:
                status = ({"status_str": "success", "completed": True, "messages": []} if ok else
                          {"status_str": "error", "completed": False, "messages": [["execution_interrupted", {}]]})
                self.history[job["id"]] = {
                    "prompt": [0, job["id"], job["prompt"], job["extra"], []], "status": status,
                    "outputs": {"9": {"images": [{"filename": f"{job['id']}.png", "subfolder": "", "type": "output"}]}}
                    if ok else {}}
                self.running = None
            self.log("end", id=job["id"], ok=ok)

    def free_comfy(self):
        def later():
            time.sleep(self.comfy_free_delay)
            with self.lock:
                self.comfy_used = 0
            self.log("comfy-freed")
        if self.comfy_free_delay is not None:
            threading.Thread(target=later, daemon=True).start()


def make_handler(world):
    class Comfy(BaseHTTPRequestHandler):
        def reply(self, obj, status=200, raw=None):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            with world.lock:
                if path.startswith("/object_info/"):
                    cls = path.rsplit("/", 1)[1]
                    return self.reply({cls: OBJECT_INFO[cls]} if cls in OBJECT_INFO else {})
                if path.startswith("/history/"):
                    pid = path.rsplit("/", 1)[1]
                    return self.reply({pid: world.history[pid]} if pid in world.history else {})
                if path == "/history":
                    n = int(parse_qs(u.query).get("max_items", ["10000"])[0])
                    return self.reply(dict(list(world.history.items())[-n:]))
                if path == "/view":
                    return self.reply(None, raw=PNG)
                if path == "/queue":
                    item = lambda j: [0, j["id"], j["prompt"], j["extra"], []]
                    return self.reply({"queue_running": [item(world.running)] if world.running else [],
                                       "queue_pending": [item(j) for j in world.queue]})
                if path == "/system_stats":
                    return self.reply({"system": {"comfyui_version": "fake"}, "devices": [{
                        "name": "cuda:0 Fake GPU", "type": "cuda", "vram_total": world.total * MiB,
                        "vram_free": world.free() * MiB, "torch_vram_total": 0, "torch_vram_free": 0}]})
            self.reply({}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/prompt":
                world.submits.append(body.get("prompt_id"))
                n = len(world.submits)
                if world.refuse_nth == n:
                    return self.reply({"error": {"message": "Prompt outputs failed validation"},
                                       "node_errors": {"7": {"class_type": "KSampler",
                                                             "errors": [{"message": "Value not in list"}]}}}, 400)
                if world.drop_nth and world.drop_nth[0] == n:
                    if world.drop_nth[1]:
                        world.add(body["prompt"], body.get("prompt_id"), body.get("extra_data"))
                    self.close_connection = True
                    return
                pid = world.add(body["prompt"], body.get("prompt_id"), body.get("extra_data"))
                world.log("submit", id=pid)
                return self.reply({"prompt_id": pid, "number": n, "node_errors": {}})
            if self.path == "/free":
                world.log("free")
                world.free_comfy()
                return self.reply({})
            if self.path == "/queue":
                with world.lock:
                    gone = set(body.get("delete", []))
                    world.queue = [j for j in world.queue if j["id"] not in gone]
                world.log("delete", ids=sorted(gone))
                return self.reply({})
            if self.path == "/interrupt":
                with world.lock:
                    if world.running and world.running["id"] == body.get("prompt_id"):
                        world.interrupt.set()
                world.log("interrupt", id=body.get("prompt_id"))
                return self.reply({})
            self.reply({}, 404)

        def log_message(self, *a):
            pass

    class Router(BaseHTTPRequestHandler):
        """llama-server in router mode with --no-models-autoload."""
        reply = Comfy.reply

        def do_GET(self):
            if self.path.split("?")[0] == "/models":
                with world.lock:
                    data = [{"id": m, "status": {"value": "loaded" if v is True else ("loading" if v == "loading"
                                                                                       else "unloaded")}}
                            for m, v in world.llm_loaded.items()]
                return self.reply({"data": data})
            self.reply({}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            model = body.get("model")
            if self.path == "/models/unload":
                with world.lock:
                    world.llm_loaded[model] = False
                world.log("llm-unload", model=model)
                return self.reply({"success": True})
            if self.path == "/models/load":
                load(model)
                return self.reply({"success": True})
            self.reply({}, 404)

        def log_message(self, *a):
            pass

    def load(model):
        with world.lock:
            if world.busy():
                world.violations.append(("load-while-comfyui-busy", model))
            if world.free() < world.llm_need:
                world.violations.append(("llm-oom", model, world.free()))
            world.llm_loaded[model] = "loading"
        world.log("llm-load", model=model)

        def done():
            time.sleep(world.load_delay)
            with world.lock:
                world.llm_loaded[model] = True
        threading.Thread(target=done, daemon=True).start()

    class Swap(BaseHTTPRequestHandler):
        """llama-swap; set Swap.has_pause = False for an older build."""
        reply = Comfy.reply
        has_pause = True
        paused = False

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/running":
                with world.lock:
                    run = [{"model": m, "state": "ready" if v is True else "starting"}
                           for m, v in world.llm_loaded.items() if v]
                return self.reply({"running": run})
            if path.startswith("/upstream/") and path.endswith("/health"):
                if Swap.paused:
                    return self.reply({"error": "paused"}, 503)
                load(path.split("/")[2])
                return self.reply("OK")
            self.reply({}, 404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/api/models/pause" and Swap.has_pause:
                Swap.paused = True
            elif self.path == "/api/models/resume" and Swap.has_pause:
                Swap.paused = False
                world.log("swap-resume")
                return self.reply({})
            elif self.path != "/api/models/unload":
                return self.reply({}, 404)
            with world.lock:
                for m in world.llm_loaded:
                    world.llm_loaded[m] = False
            world.log("llm-unload", model="*", via=self.path)
            return self.reply({})

        def log_message(self, *a):
            pass

    return Comfy, Router, Swap


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
