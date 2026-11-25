# Recipes

A recipe is a folder in `recipes/`:

| File | What it is | Who reads it |
|---|---|---|
| `card.json` | the settings a person would change, and what the recipe needs | the script; the agent sees it through `show` |
| `prompt.json` | the workflow in ComfyUI's API format, converted once | only the script |
| `notes.md` | the workflow author's notes, from its Note nodes | the agent, through `show --notes`, when it needs them |
| `workflow.json` | the original workflow, to convert again later | nobody at run time |

The agent never opens `prompt.json`. That's the point: a workflow file is 4,000 to 20,000 tokens, and its
card, as `show` prints it, is about 150.

## card.json

```json
{
 "title": "Z-Image Turbo: picture from words",
 "does": "A photo-real or illustrated picture from one plain sentence. A few seconds each.",
 "inputs": {
  "prompt": {"set": "57:27.text", "required": true, "help": "what the picture shows"},
  "seed":   {"set": ["57:3.seed"], "default": "random"},
  "width":  {"set": "57:13.width", "default": 1024, "help": "pixels, a multiple of 64"},
  "image":  {"set": "97.image", "type": "image", "required": true, "help": "the picture to animate"},
  "fast":   {"set": "129:131.value", "type": "bool", "default": true}
 },
 "tips": ["Describe the subject, what it does, the setting, the light and the camera."],
 "needs": {
  "nodes": [{"id": "ausboss-nodes", "repo": "https://github.com/ausboss/ComfyUI-AusBoss", "classes": ["AUSBOSS_NODES_Text"]}],
  "models": [{"file": "ae.safetensors", "folder": "vae", "url": "https://huggingface.co/.../ae.safetensors"}]
 }
}
```

- **`set`** is where the value goes: `NODE_ID.input`, or a list of them for one value used in several places.
  A node inside a subgraph has an id like `57:27`, exactly as in `prompt.json`.
- **`type`** is `text`, `number`, `bool`, `seed`, `image`, `video` or `audio`. Leave it out and it's
  worked out from the default. `image`, `video` and `audio` take a file path and upload it.
- **`default`** is used when the agent doesn't give the setting. A setting with no default keeps whatever the
  workflow had. `"random"` for a seed means a new one for every job.
- **`required`** makes `run` refuse to start without it.
- **`needs`** is what `install` uses. `classes` says which node types come from each pack, so only missing
  packs are installed. Model `url`s can be Hugging Face file links, Hugging Face repo pages (the file is found
  by name), or Civitai links with `modelVersionId`.

## Making one

```bash
python3 comfy_agent.py add "My Workflow.json" --name my-thing
```

`add` needs ComfyUI running, with the workflow's nodes installed, because it asks ComfyUI what each input is.

1. **Convert.** A saved workflow is the editor's format. `add` converts it with
   [comfy-cli](https://github.com/Comfy-Org/comfy-cli) (`comfy run --print-prompt`). Without comfy-cli,
   export it yourself: open it in ComfyUI, **Workflow > Export (API)**, then
   `add "My Workflow.json" --api "My Workflow_api.json" --name my-thing`.
2. **Find the settings.** Text boxes, seeds, sizes, picture/video/audio loaders, and named Primitive nodes
   (the ones template authors make for "Duration" or "Steps"). Big workflows give too many: that's expected.
3. **Find what it needs.** Node packs from each node's saved `cnr_id`/`aux_id` (or the pack's git remote on
   your machine), model files and their links from the workflow's own model list (the one ComfyUI uses for
   its "missing models" dialog), and the folder of any other model file from your ComfyUI.
4. **Tidy the card by hand.** Rename settings to plain words (`text_your_edit` becomes `edit`), write a help
   line for each, mark required ones, delete everything nobody should change, and write `title`, `does` and
   `tips`. Then test: `run my-thing --count 1`, and look at the result.

## What conversion gets wrong

We converted the 12 AusBoss example workflows both ways, with comfy-cli 1.23 and with ComfyUI's own
frontend (the code the Run button uses), and compared the jobs node by node:

- 7 were identical.
- 4 differed only by a Workflow Switches node that comfy-cli keeps and the frontend drops (the node's
  JavaScript marks it virtual). It has no inputs or outputs, so it doesn't change the result.
- 1 (Simple Video Watermark Remover) got two different settings: a SAM3 box and a mask hole size.

So check a converted recipe with one test run. When it's wrong, use the frontend's *Export (API)* file with
`--api`.

Things that only happen in the editor don't survive conversion at all: switches that bypass groups from a
JavaScript widget, buttons, and settings computed in the browser. The converted job has them fixed at the
state they were saved in. To offer both states, make two recipes.

Don't trust automatic dependency guessing either. For Klein 9B Outpaint, `comfy node deps-in-workflow` wanted
a third-party pack the workflow doesn't use. The workflow's own `cnr_id` fields were right.
