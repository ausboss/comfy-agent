# Remote Control on the GPU machines

Goal: a Claude project can start a session on arch5090 or arch3090 and run GPU work with nobody at the
keyboard. One script does the setup: [`tools/setup-remote-control.sh`](../tools/setup-remote-control.sh).
Run it once per machine; running it again only fixes what drifted.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ausboss/comfy-agent/main/tools/setup-remote-control.sh)
```

Options: `--dir PATH` (default `~/code/ausboss`), `--name NAME`, `--no-rename`, `--dry-run`, `--uninstall`.

## What went wrong before (2026-11-26/27)

| Symptom | Cause | Fix in the script |
|---|---|---|
| Starting a session from the project failed with no detail | Remote Control depends on the folder being served by a live `claude` that sees no blocking variables | Service with the blockers unset; blockers commented out of profiles |
| `claude remote-control` errored on arch5090 | The server refuses a second server for the same folder on one device (HTTP 409, "This folder is already served by another Claude Code on this device") | One owner: the service. Terminal copies in that folder are stopped; the desktop app's copy has to be removed in its settings |
| arch3090 showed up as a second "arch5090" | Claude names a device after `os.hostname()` (lower-cased, `.local`/`.lan` stripped). The 3090 box's hostname said 5090 | Hostname set from the GPU when it follows `<prefix><4 digits>` and the digits disagree |
| Every command asked first | No allow rules, and the per-hostname "unattended serving" consent not given yet | Allow rules in `~/.claude/settings.json` |

## Verified facts (Claude Code 2.1.332, checked in the CLI and docs on 2026-11-27)

- Device name = hostname. There is no setting for it; `--remote-control-session-name-prefix` only names sessions.
- Blocking variables per the docs: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_DISABLE_FEATURE_FLAGS`.
  `DISABLE_TELEMETRY` / `DO_NOT_TRACK` only block when an org requires Trusted Devices, but the CLI classifies
  all three as "privacy level" settings, so the script clears them too. Also unusable: `ANTHROPIC_BASE_URL` pointing
  elsewhere, API-key or `CLAUDE_CODE_OAUTH_TOKEN` (inference-only) auth. The service unsets these; profiles keep them
  (local-model setups may need them) and the script warns.
- A headless server needs the folder trusted (`projects[DIR].hasTrustDialogAccepted` in `~/.claude.json`) and the
  one-time "Enable Remote Control?" answer (`remoteDialogSeen`). The script sets both.
- `claude daemon install` exists but service install is disabled in this version, so the script writes its own
  systemd user unit (`~/.config/systemd/user/claude-remote-control.service`) with `loginctl enable-linger`.
- `--settings FILE` on `remote-control` would scope rules to the service only, but it turns off auto-compact and
  auto mode for project sessions, so rules go in the user settings file instead.

## Tradeoffs

- The allow rules (`Bash`, `Read`, `Edit`, `Write`, ...) apply to every Claude session on the machine, not only
  remote ones. A small deny list blocks `rm -rf /` / `~`, force pushes, `mkfs` and `dd` to devices.
- Renaming the host also resets the per-hostname consents Claude stores, and may rename the machine in Tailscale.

## Status

- Script tested in a sandbox with stubbed `systemctl`, `hostnamectl`, `nvidia-smi` and `claude` (rename, profile
  edits, JSON merges, idempotent second run, conflict message). Not yet run on real hardware.
