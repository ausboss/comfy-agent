## 🆎 AusBoss

Klein 9B Outpaint

Extend a picture, and Klein 9B paints the new area.

[▶ AusBoss on YouTube](https://www.youtube.com/@ausbossdev) · videos on these nodes and workflows

---

1. Load your image.
2. Pick a ratio, or drag the orange handles out.
3. Optional: describe the new area in **What goes there**, after the first sentence.
4. Press **Run**.

Keep that first sentence. Without it the new area can come out blank.

A big pad can grow a second hand or face near your picture's edge. If it does, extend in two runs.

The result is about 1.6 MP, whatever size your picture is.

Needs AusBoss nodes newer than 2.4.0 (update first).

## Note

## These two boxes write the prompt from your image, then paint and put your image back. Double-click one to see inside.

## Note

Qwen3-VL 8B describes the setting behind your subject, not the person. Describing the person made Klein paint a second face under tight crops.

## Note

Your line from What goes there comes first, then the description. Show Text shows the prompt Klein gets.

## Note

The canvas becomes the starting latent. Its mask marks the gray areas, so the sampler paints only there.

## Note

Klein reads the prompt with the canvas as a reference. At CFG 1 the negative side is not used.

## Note

4 steps, CFG 1, euler and simple: the settings for the distilled Klein 9B.

## Note

Your image goes back on top. Blend in fades the new area into your image just inside the edge, and Tone match evens out its colour.
