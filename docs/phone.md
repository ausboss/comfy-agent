# Results on your phone or another computer

Three ways, from most private to most convenient. All of them work while you're away from home, because
Tailscale connects your phone and your other computers to this one from anywhere.

## Taildrop (Tailscale)

Files go straight from this computer to your phone or another computer over your tailnet. Nothing passes
through anyone else's server.

1. Install Tailscale on this computer and the other device, signed in to the same account.
2. Find the device's name: `tailscale status`.
3. Save it once: `python3 comfy_agent.py config phone=tailscale:iphone-15-pro-max` (or `laptop=tailscale:my-laptop`)
4. Add `--send` (or `--send laptop`) to a run, or send the last results: `python3 comfy_agent.py send last --to laptop`.

On an iPhone the files wait in the Tailscale app: open it and save them to Files or Photos. On Android, Windows
and macOS they land in Downloads; on Linux, run `tailscale file get ~/Downloads` on the receiving computer.
Taildrop only works between devices of the same user. Docs:
[tailscale.com/docs/features/taildrop](https://tailscale.com/docs/features/taildrop).

## Gallery (Tailscale Serve)

`python3 comfy_agent.py gallery` writes `outputs/index.html` (newest first, pictures and videos), starts a small
web server for the `outputs` folder on 127.0.0.1:8766, and points Tailscale Serve's `/comfy-agent/` at it. It
prints the address, something like `https://my-pc.tail1234.ts.net/comfy-agent/`. Open it in a browser on your
phone or any of your computers with Tailscale on. Only devices on your tailnet can reach it. The page updates
after every run, and videos play on iPhones too. `gallery --off` takes it down. (Tailscale only lets root serve
a folder directly, which is why the kit runs its own server; `config gallery_port=N` moves it.)

This adds one path to your Tailscale Serve settings and leaves the rest alone. Check them with
`tailscale serve status`.

## ntfy push notifications

[ntfy](https://ntfy.sh) sends each file as a push notification with the picture in it.

1. Install the ntfy app and subscribe to a topic with a long random name, like `comfy-7Hq2v9xLpR4mK1`.
2. `python3 comfy_agent.py config phone=ntfy:comfy-7Hq2v9xLpR4mK1`

On the public ntfy.sh server, anyone who knows the topic name can read it, and attachments are kept there
for a few hours. For anything private, run your own ntfy server (it can sit on your tailnet), and use
`phone=ntfy:https://ntfy.your-tailnet.ts.net/topic`. Big videos may be over ntfy.sh's attachment limit; send
those with Taildrop or open them in the gallery.

## More than one target

Any name works, not only `phone`:

```bash
python3 comfy_agent.py config phone=tailscale:iphone-15-pro-max tablet=tailscale:ipad push=ntfy:comfy-7Hq2v9x
python3 comfy_agent.py run zimage prompt="..." --count 4 --send tablet
```
