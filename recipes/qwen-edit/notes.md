## Note

## These two boxes write the full instruction, then make the edit and line it up with your image. Double-click one to see inside.

## Note

Your picture at about 1 MP, the size Qwen Image 2.1 edits at. The edit, Realign and the compare all use this copy.

## Note

The writer's instructions come first, then your edit. Change them to change how every instruction is written.

## Note

Qwen3-VL 8B, the edit's own text encoder, looks at your picture and turns your edit into a full instruction.

## Note

Load Image + Pad mirrors 64 px of your picture onto every side, so the edit has room to move without leaving an empty edge.

## Note

The padded picture goes in as the reference. Keep the encoder's latent on the sampler: a canvas of another size adds its own zoom.

## Note

25 steps, CFG 1, euler and simple. The seed comes from the Seed node on the top level.

## Note

Realign to Source measures how far the edit moved, moves it back and cuts the margin off. The slider shows the raw edit.
