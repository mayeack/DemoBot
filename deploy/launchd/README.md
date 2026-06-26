# launchd service definitions (macOS)

Per-user **LaunchAgents** that keep the DemoBot stack running across logout-free
reboots and sleep/wake, with auto-restart on crash (`KeepAlive`). These are copies
of what gets installed under `~/Library/LaunchAgents/`.

| Plist | Runs | Listens / serves |
|-------|------|------------------|
| `com.yeack.medadvice-collector.plist` | `run-collector.sh` (OTel collector) | OTLP `:4317`/`:4318` → Splunk O11y + Galileo |
| `com.yeack.medadvice-app.plist` | `run.sh` (FastAPI app) | `0.0.0.0:8001` |
| `com.yeack.medadvice-tunnel.plist` | `cloudflared tunnel run medadvice` | public `https://medadvice.yeackbot.com` |
| `com.yeack.ollama-env.plist` | `launchctl setenv OLLAMA_*` (one-shot) | sets `OLLAMA_KEEP_ALIVE=30m` + `OLLAMA_MAX_LOADED_MODELS=2` for the GUI session |

> `com.yeack.ollama-env.plist` is a **one-shot** (`KeepAlive=false`) — it exits after
> setting the vars, so `launchctl print` shows it "exited". That is expected. Install it
> (and set the vars for the current session) with `../../setup-ollama-env.sh`, then restart
> Ollama.app once so the menu-bar daemon inherits them. These vars are read by `ollama
> serve`, not by the app.

> Paths are machine-specific: they assume the repo at `/Applications/medadvice_v3`,
> user `myeack`, Homebrew `cloudflared` at `/opt/homebrew/bin`, and the named-tunnel
> config at `~/.cloudflared/config.yml` (created by `../../setup-named-tunnel.sh`).
> Adjust the absolute paths if your layout differs. The tunnel credentials live in
> `~/.cloudflared/` and are **not** in this repo.

## Install / load

```bash
cp deploy/launchd/com.yeack.medadvice-*.plist ~/Library/LaunchAgents/
UID_N=$(id -u)
for svc in collector app tunnel; do
  launchctl bootstrap gui/$UID_N ~/Library/LaunchAgents/com.yeack.medadvice-$svc.plist
done
```

## Status / restart / uninstall

```bash
UID_N=$(id -u)
launchctl print    gui/$UID_N/com.yeack.medadvice-app        # status + pid
launchctl kickstart -k gui/$UID_N/com.yeack.medadvice-tunnel # restart (e.g. after upgrading cloudflared)
launchctl bootout  gui/$UID_N/com.yeack.medadvice-collector  # stop + unload
```

Logs: `logs/launchd-app.log`, `logs/launchd-collector.log` (in-repo, gitignored),
and `~/.cloudflared/medadvice-tunnel.log`.

> Scope: a LaunchAgent runs only while the user is logged in — correct for a laptop
> demo. For boot-before-login startup, install the tunnel as a system LaunchDaemon
> with `sudo cloudflared service install` instead.
