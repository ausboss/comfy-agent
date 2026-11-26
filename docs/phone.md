# Send files to your phone

## Taildrop (Tailscale)

Taildrop sends the original files between your devices over Tailscale.

1. Install Tailscale on this computer and the other device, signed in to the same account.
2. Find the device's name: `tailscale status`.
3. Save it once: `python3 comfy_agent.py config phone=tailscale:PHONE-NAME` (or `laptop=tailscale:LAPTOP-NAME`)
4. Add `--send` (or `--send laptop`) to a run, or send the last results: `python3 comfy_agent.py send last --to laptop`.

On an iPhone the files wait in the Tailscale app: open it and save them to Files or Photos. On Android, Windows
and macOS they land in Downloads; on Linux, run `tailscale file get ~/Downloads` on the receiving computer.
Taildrop only works between devices of the same user. Docs:
[tailscale.com/docs/features/taildrop](https://tailscale.com/docs/features/taildrop).

## ntfy push notifications

[ntfy](https://ntfy.sh) sends each file as a push notification with the picture in it.

1. Install the ntfy app and subscribe to a topic with a long random name of your own.
2. `python3 comfy_agent.py config phone=ntfy:YOUR-RANDOM-TOPIC`

On the public ntfy.sh server, anyone who knows the topic name can read it, and attachments are kept there
for a few hours. For anything private, run your own ntfy server (it can sit on your tailnet), and use
`phone=ntfy:https://ntfy.your-tailnet.ts.net/topic`. Big videos may be over ntfy.sh's attachment limit; send
those with Taildrop.

## More than one target

Any name works, not only `phone`:

```bash
python3 comfy_agent.py config phone=tailscale:PHONE-NAME tablet=tailscale:ipad push=ntfy:YOUR-RANDOM-TOPIC
python3 comfy_agent.py run zimage prompt="..." --count 4 --send tablet
```
