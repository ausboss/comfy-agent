---
name: comfy-agent
description: Make pictures and videos with the person's ComfyUI from plain English, using ready recipes (their workflows). Queues many at once, saves the results and can send them to their phone or another computer. Use when they ask for an image, picture, photo edit, video or clip made with ComfyUI or with one of their workflows.
---

# Make pictures and videos with ComfyUI

Everything goes through one script in the folder this file is in. Run it from that folder:
`python3 comfy_agent.py ...` (on Windows: `python comfy_agent.py ...`). It prints short plain text.
Don't open the recipe files or call ComfyUI's API yourself: the script does that, and the files are large.

## Make something

1. `python3 comfy_agent.py list` shows the recipes: one line each, with their settings (`*` = required).
   Pick the one that fits. If none does, say so and stop.
2. `python3 comfy_agent.py show RECIPE` shows its settings, defaults and tips. Follow the tips.
3. `python3 comfy_agent.py run RECIPE setting=value ... --count 4` queues 4, waits, saves them and prints
   each file's path and seed. Quote values with spaces: `prompt="a red fox in the snow"`.
   - Different versions: give a setting more than once, one job each: `prompt="a cat" prompt="a dog"`.
   - The person's own pictures or clips: give the path, `image=/path/to/photo.jpg`. It is uploaded for you.
   - Leave every setting they didn't ask about at its default.
   - Long jobs (video, many results): add `--no-wait`, then run `python3 comfy_agent.py wait` until it
     reports every job done.
4. Look at the results before you report: open each picture with your file or image tool, one at a time.
   For a video, take one frame: `ffmpeg -y -ss 1 -i CLIP.mp4 -frames:v 1 frame.jpg`. Check it shows what was
   asked.
5. Report: what you made, the file paths, and the seed of each. To change one that's close, keep its seed
   (`seed=N`) and change one setting.

## Send it to their phone or another computer

Add `--send` to `run` or `wait`, or afterwards: `python3 comfy_agent.py send last`.
If it says `phone` isn't set up: run `tailscale status`; if exactly one device is a phone (iOS or
android), save it with `python3 comfy_agent.py config phone=tailscale:DEVICE-NAME`. Otherwise ask which one.
For another device, give its name: `--send laptop` (after `config laptop=tailscale:DEVICE-NAME`).
`python3 comfy_agent.py gallery` prints a private web page of every result, for any of their devices.

## When something is missing

`run` and `python3 comfy_agent.py doctor` say when a recipe can't run yet.
`python3 comfy_agent.py install RECIPE` then prints what it would add: node packs (code from GitHub) and
model files with their sizes. Installing is the one thing that needs the person's OK: show them that list
once. With their yes, run `python3 comfy_agent.py install RECIPE --yes` and carry on without asking again.
New node packs need a restart: `python3 comfy_agent.py restart`.
If ComfyUI isn't answering, tell them to start it; don't start or stop it yourself.

## A workflow that isn't a recipe yet

`python3 comfy_agent.py add WORKFLOW.json --name NAME` makes `recipes/NAME/`. Then edit its `card.json`:
give each setting a plain name and a help line, mark the ones that must be given `"required": true`,
delete settings nobody should change, and fill in `title`, `does` and `tips` (the author's notes are in
`notes.md`). Test with `--count 1` and look at the result.

## Rules

- Ask before installing anything and before deleting anything. Nothing else needs a question.
- Don't change ComfyUI's settings, its other workflows, or files outside this folder's `outputs/`.
- Never say a result is good without looking at it.
- If a run prints `BLOCKED`, tell the person what it says and don't run it again.
