import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from generation_receipt import graph_receipt


RECIPES = Path(__file__).resolve().parents[1] / "recipes"


def recipe(name):
    folder = RECIPES / name
    return folder, json.loads((folder / "card.json").read_bytes()), json.loads((folder / "prompt.json").read_bytes())


class GenerationReceiptTests(unittest.TestCase):
    def test_requested_text_vs_raw_overrides_and_file_hashes(self):
        folder, card, graph = recipe("zimage")
        original = graph_receipt(folder, card, graph, {})
        request = "a 🌻 in snow\n" * 90
        actual = "different from the named prompt\n" * 80
        graph["57:27"]["inputs"]["text"] = actual
        graph["57:13"]["inputs"].update(width=768, height=512)
        graph["57:3"]["inputs"].update(seed=18446744073709551615, steps=13, sampler_name="euler", scheduler="karras")
        card["inputs"]["negative_prompt"] = {"default": "named negative default"}
        r = graph_receipt(folder, card, graph, {"prompt": request})
        self.assertEqual(r["requested"]["positive"], {"name": "prompt", "source": "given", "text": request})
        self.assertEqual(r["requested"]["negative"], {"name": "negative_prompt", "source": "default",
                                                      "text": "named negative default"})
        sampler, = r["samplers"]
        self.assertEqual(sampler["positive"], {"status": "literal", "text": actual})
        self.assertEqual(sampler["negative"], {"status": "zeroed", "text": None})
        self.assertEqual([sampler[k] for k in ("seed", "steps", "width", "height", "sampler", "scheduler")],
                         ["18446744073709551615", 13, 768, 512, "euler", "karras"])
        self.assertEqual(r["workflow"]["name"], card["title"])
        for key, filename in (("api", "prompt.json"), ("source", "workflow.json")):
            self.assertEqual(r["workflow"][key + "_file"], "recipes/zimage/" + filename)
            self.assertEqual(r["workflow"][key + "_sha256"], hashlib.sha256((folder / filename).read_bytes()).hexdigest())
        canonical = json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(r["workflow"]["effective_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertNotEqual(r["workflow"]["effective_sha256"], original["workflow"]["effective_sha256"])
        self.assertEqual(original["requested"]["positive"]["source"], "absent")
        self.assertEqual(original["requested"]["negative"], {"name": None, "source": "absent", "text": None})

    def test_dynamic_edits_and_absent_or_empty_are_not_observed_prompts(self):
        folder, card, graph = recipe("qwen-edit")
        r = graph_receipt(folder, card, graph, {"edit": "only change the hat", "image": "/private/input.png"})
        self.assertEqual(r["requested"]["positive"], {"name": "edit", "source": "given", "text": "only change the hat"})
        s, = r["samplers"]
        self.assertEqual(s["positive"], {"status": "dynamic", "text": None})
        self.assertEqual(s["negative"], {"status": "literal", "text": ""})
        self.assertEqual((s["width"], s["height"]), (None, None))
        self.assertEqual(r["loras"], [])
        self.assertNotIn("/private/", json.dumps(r))
        folder, card, graph = recipe("klein-outpaint")
        r = graph_receipt(folder, card, graph, {})
        self.assertEqual(r["requested"]["positive"]["source"], "absent")
        self.assertEqual(r["samplers"][0]["positive"], {"status": "dynamic", "text": None})
        self.assertEqual(r["samplers"][0]["negative"], {"status": "zeroed", "text": None})
        self.assertEqual(r["loras"], [{"node": "4", "file": "pixaoutpaint.safetensors", "strength_model": 1,
                                       "strength_clip": 0}])

    def test_wan_resolves_two_sampler_stages_and_the_active_fast_switch(self):
        folder, card, graph = recipe("wan-i2v")
        graph["129:89"]["inputs"]["text"] = "no flicker"
        for fast, total, split in ((False, 20, 10), (True, 4, 2)):
            with self.subTest(fast=fast):
                graph["129:131"]["inputs"]["value"] = fast
                r = graph_receipt(folder, card, graph, {})
                self.assertEqual([(s["steps"], s["start_step"], s["end_step"], s["add_noise"]) for s in r["samplers"]],
                                 [(total, 0, split, "enable"), (total, split, total, "disable")])
                self.assertEqual([s["negative"] for s in r["samplers"]], [{"status": "literal", "text": "no flicker"}] * 2)
                self.assertEqual([(s["width"], s["height"]) for s in r["samplers"]], [(640, 640), (640, 640)])
                self.assertEqual(len(r["loras"]), 2 if fast else 0)
                if fast:
                    self.assertTrue(all(x["strength_clip"] == 0 and x["strength_model"] > 0 for x in r["loras"]))
                self.assertEqual({m["file"] for m in r["models"] if m["kind"] == "diffusion"},
                                 {graph[n]["inputs"]["unet_name"] for n in ("129:95", "129:96")})

    def test_custom_lora_clip_strengths_and_known_fields_only(self):
        folder, card, graph = recipe("klein-outpaint")
        graph["4"]["inputs"].update(clip=["2", 0], loras=json.dumps([
            {"name": "skipped.safetensors", "group_off": True}, {"name": "inactive.gguf", "enabled": False},
            {"name": "C:\\private\\test.safetensors", "strength": 0.75, "strength_clip": 0.5},
            {"name": "styles/other.safetensors", "id": "r2", "strength": 2, "strength_clip": 1}]),
            strength_r2=["slider", 0])
        graph["slider"] = {"class_type": "PrimitiveFloat", "inputs": {"value": 0.5}}
        graph["1"]["inputs"].update(unet_name="/private/base.gguf", api_token="do-not-copy")
        graph["2"]["inputs"]["clip_name"] = "https://invalid.example/base.gguf?token=do-not-copy"
        graph["unused"] = {"class_type": "LoraLoader", "inputs": {"lora_name": "unused.gguf"}}
        r = graph_receipt(folder, card, graph, {"api_token": "do-not-copy"})
        self.assertEqual(r["loras"], [
            {"node": "4", "file": "test.safetensors", "strength_model": 0.75, "strength_clip": 0.5},
            {"node": "4", "file": "styles/other.safetensors", "strength_model": 0.5, "strength_clip": 0.25}])
        self.assertIn({"node": "1", "kind": "diffusion", "file": "base.gguf"}, r["models"])
        for private in ("private", "do-not-copy", "unused.gguf", "https:"):
            self.assertNotIn(private, json.dumps(r))

    def test_missing_sources_dangling_links_and_cycles(self):
        graph = {"s": {"class_type": "KSampler", "inputs": {"positive": ["s", 0], "negative": ["missing", 0],
                                                            "latent_image": ["s", 0], "steps": ["val", 0]}},
                 "val": {"class_type": "PrimitiveInt", "inputs": {"value": ["val", 0]}}, "broken": "not a node"}
        with tempfile.TemporaryDirectory() as tmp:
            r = graph_receipt(Path(tmp), {}, graph, {})
        self.assertIsNone(r["workflow"]["source_file"])
        self.assertIsNone(r["workflow"]["api_sha256"])
        s, = r["samplers"]
        self.assertEqual(s["positive"], {"status": "unknown", "text": None})
        self.assertEqual(s["negative"], s["positive"])
        self.assertEqual((s["width"], s["height"], s["steps"]), (None, None, None))
