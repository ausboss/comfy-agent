"""Describe the submitted API graph, without guessing values computed at runtime."""
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath


_SAMPLERS = {"KSampler", "KSamplerAdvanced"}
_OUTPUTS = {"SaveImage", "SaveAnimatedPNG", "SaveAnimatedWEBP", "SaveVideo", "AUSBOSS_NODES_SaveImage"}
_PRIMITIVES = {"PrimitiveInt": "value", "PrimitiveFloat": "value", "PrimitiveBoolean": "value",
               "PrimitiveString": "value", "PrimitiveStringMultiline": "value", "AUSBOSS_NODES_Seed": "seed",
               "AUSBOSS_NODES_Integer": "value", "AUSBOSS_NODES_Float": "value", "AUSBOSS_NODES_Text": "text",
               "AUSBOSS_NODES_ShowText": "text"}
_LOADERS = {"UNETLoader": ("diffusion", ("unet_name",)), "CLIPLoader": ("text_encoder", ("clip_name",)),
            "DualCLIPLoader": ("text_encoder", ("clip_name1", "clip_name2")),
            "TripleCLIPLoader": ("text_encoder", ("clip_name1", "clip_name2", "clip_name3")),
            "VAELoader": ("vae", ("vae_name",)), "CheckpointLoaderSimple": ("checkpoint", ("ckpt_name",)),
            "UpscaleModelLoader": ("upscaler", ("model_name",))}


def _number(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def _file(value):
    if not isinstance(value, str) or re.search(r"[?#\x00-\x1f]|://", value):
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.suffix.lower() not in {".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".sft", ".bin", ".onnx"}:
        return None
    return path.name if path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts else path.as_posix()


def _strength(value):
    n = _number(value)
    return max(-10, min(10, n)) if n is not None else None


def _asset(folder, name):
    path = folder / name
    try:
        if path.is_symlink() or not path.is_file():
            return None, None
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(262144), b""):
                digest.update(chunk)
        label = f"recipes/{folder.name}/{name}" if folder.parent.name == "recipes" else name
        return label, digest.hexdigest()
    except OSError:
        return None, None


def _request(card, given, aliases):
    inputs = card.get("inputs")
    named = {key: spec for key in aliases if isinstance(inputs, dict) and isinstance(spec := inputs.get(key), dict)
             and spec.get("type") in (None, "text", "string")}
    for key in named:
        if key in given:
            return {"name": key, "source": "given", "text": given[key] if isinstance(given[key], str) else None}
    for key, spec in named.items():
        if isinstance(spec.get("default"), str):
            return {"name": key, "source": "default", "text": spec["default"]}
    return {"name": next(iter(named), None), "source": "absent", "text": None}


class _Graph:
    def __init__(self, graph):
        self.nodes = {str(nid): (node["class_type"], node["inputs"]) for nid, node in graph.items()
                      if isinstance(node, dict) and isinstance(node.get("class_type"), str)
                      and isinstance(node.get("inputs"), dict)}
        self.scalars = {}

    def link(self, value):
        if (isinstance(value, list) and len(value) == 2 and type(value[0]) in (str, int)
                and type(value[1]) is int and value[1] >= 0 and str(value[0]) in self.nodes):
            return str(value[0]), value[1]
        return None

    def branch(self, inputs, seen=frozenset()):
        switch = self.value(inputs.get("switch"), seen)
        return inputs.get("on_true" if switch else "on_false") if type(switch) is bool else None

    def value(self, value, seen=frozenset()):
        if type(value) in (bool, str, int, float):
            return value
        link = self.link(value)
        if not link or link[1] != 0 or link in seen or len(seen) >= 64:
            return None
        if link in self.scalars:
            return self.scalars[link]
        kind, inputs = self.nodes[link[0]]
        if kind == "ComfySwitchNode":
            value = self.branch(inputs, seen | {link})
        elif kind in _PRIMITIVES:
            value = inputs.get(_PRIMITIVES[kind])
        else:
            return None
        self.scalars[link] = self.value(value, seen | {link})
        return self.scalars[link]

    def active(self):
        pending = [nid for nid, (kind, _) in self.nodes.items() if kind in _OUTPUTS]
        if not pending:
            pending = [nid for nid, (kind, _) in self.nodes.items() if kind in _SAMPLERS]
        used = set()
        while pending:
            nid = pending.pop()
            if nid in used:
                continue
            used.add(nid)
            kind, inputs = self.nodes[nid]
            values = (inputs.get("switch"), self.branch(inputs)) if kind == "ComfySwitchNode" else inputs.values()
            pending.extend(link[0] for value in values if (link := self.link(value)))
        return used

    def conditioning(self, value):
        seen = set()
        while (link := self.link(value)) and link not in seen:
            seen.add(link)
            kind, inputs = self.nodes[link[0]]
            slot = link[1]
            if kind == "ConditioningZeroOut" and slot == 0:
                return {"status": "zeroed", "text": None}
            if (kind == "CLIPTextEncode" and slot == 0) or (kind == "TextEncodeQwenImage21" and slot in (0, 1)):
                field = "text" if kind == "CLIPTextEncode" else ("prompt", "negative_prompt")[slot]
                text = self.value(inputs.get(field))
                if isinstance(text, str):
                    return {"status": "literal", "text": text}
                if self.link(inputs.get(field)):
                    return {"status": "dynamic", "text": None}
                break
            if kind == "ReferenceLatent" and slot == 0:
                value = inputs.get("conditioning")
            elif kind == "WanImageToVideo" and slot in (0, 1):
                value = inputs.get(("positive", "negative")[slot])
            elif kind == "ComfySwitchNode" and slot == 0:
                value = self.branch(inputs)
            else:
                break
        return {"status": "unknown", "text": None}

    def dimensions(self, value):
        seen = set()
        while (link := self.link(value)) and link not in seen:
            seen.add(link)
            kind, inputs = self.nodes[link[0]]
            if ((kind in ("EmptyLatentImage", "EmptySD3LatentImage") and link[1] == 0)
                    or (kind == "WanImageToVideo" and link[1] == 2)):
                return tuple(_number(self.value(inputs.get(key))) for key in ("width", "height"))
            if kind in _SAMPLERS and link[1] == 0:
                value = inputs.get("latent_image")
            elif kind == "ComfySwitchNode" and link[1] == 0:
                value = self.branch(inputs)
            else:
                break
        return None, None

    def sampler(self, nid, kind, inputs):
        result = {"node": nid, "type": kind}
        for label, key in (("steps", "steps"), ("cfg", "cfg"), ("denoise", "denoise"),
                           ("start_step", "start_at_step"), ("end_step", "end_at_step")):
            result[label] = _number(self.value(inputs.get(key)))
        seed = self.value(inputs.get("noise_seed" if kind == "KSamplerAdvanced" else "seed"))
        result["seed"] = (str(seed) if abs(seed) >= 2**53 else seed) if type(seed) is int else None
        result["width"], result["height"] = self.dimensions(inputs.get("latent_image"))
        for label, key in (("sampler", "sampler_name"), ("scheduler", "scheduler")):
            value = self.value(inputs.get(key))
            result[label] = value if isinstance(value, str) and re.fullmatch(r"[\w.+-]{1,100}", value) else None
        noise = self.value(inputs.get("add_noise"))
        result["add_noise"] = noise if noise in ("enable", "disable") else None
        for side in ("positive", "negative"):
            result[side] = self.conditioning(inputs.get(side))
        return result

    def loras(self, nid, kind, inputs):
        if kind in {"LoraLoader", "LoraLoaderModelOnly"}:
            if file := _file(self.value(inputs.get("lora_name"))):
                yield {"node": nid, "file": file, "strength_model": _number(self.value(inputs.get("strength_model"))),
                       "strength_clip": 0 if kind == "LoraLoaderModelOnly" else _number(self.value(inputs.get("strength_clip")))}
        elif kind == "AUSBOSS_NODES_LoraLoader":
            try:
                rows = json.loads(self.value(inputs.get("loras")))
            except (TypeError, ValueError, RecursionError):
                return
            for row in rows if isinstance(rows, list) else ():
                if not isinstance(row, dict) or not row.get("enabled", True) or row.get("group_off") is True:
                    continue
                file = _file(row.get("name"))
                if not file:
                    continue
                model = _strength(row.get("strength", 1))
                clip = _strength(row.get("strength_clip", model))
                wire = f"strength_{row.get('id')}"
                if row.get("id") and wire in inputs:
                    linked = _strength(self.value(inputs[wire]))
                    ratio = None if model is None or clip is None else clip / model if model else 1
                    model = linked
                    clip = _strength(linked * ratio) if linked is not None and ratio is not None else None
                yield {"node": nid, "file": file, "strength_model": model,
                       "strength_clip": clip if self.link(inputs.get("clip")) else 0}


def graph_receipt(folder: Path, card: dict, graph: dict, given: dict) -> dict:
    """Capture only known metadata; a linked generator's runtime output is not in the graph."""
    source, source_hash = _asset(folder, "workflow.json")
    api, api_hash = _asset(folder, "prompt.json")
    try:
        canonical = json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        effective = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, RecursionError):
        effective = None
    title = card.get("title")
    if not isinstance(title, str) or not title.strip() or re.search(r"://|^(?:Authorization:|Bearer |Basic )", title, re.I):
        title = folder.name
    elif PureWindowsPath(title).is_absolute() or title.startswith("/"):
        title = PureWindowsPath(title).name or folder.name
    workflow = {"name": title, "source_file": source,
                "source_sha256": source_hash, "api_file": api, "api_sha256": api_hash, "effective_sha256": effective}
    requested = {"positive": _request(card, given, ("prompt", "positive", "positive_prompt", "edit")),
                 "negative": _request(card, given, ("negative", "negative_prompt"))}
    description = _Graph(graph)
    active = description.active()
    samplers, models, loras = [], [], []
    for nid, (kind, inputs) in description.nodes.items():
        if nid not in active:
            continue
        label = nid if re.fullmatch(r"[\w:-]{1,100}", nid) else None
        if kind in _SAMPLERS:
            samplers.append(description.sampler(label, kind, inputs))
        if kind in _LOADERS:
            role, keys = _LOADERS[kind]
            for key in keys:
                if file := _file(description.value(inputs.get(key))):
                    models.append({"node": label, "kind": role, "file": file})
        loras.extend(description.loras(label, kind, inputs))
    return {"workflow": workflow, "requested": requested, "samplers": samplers, "models": models, "loras": loras}
