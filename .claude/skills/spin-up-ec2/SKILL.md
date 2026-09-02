---
name: spin-up-ec2
description: Provision / spin up / launch / deploy DemoBot EC2 GPU instances (g5.xlarge fleet) in the personal AWS account, each serving its own medadviceN.yeackbot.com subdomain via a dedicated Cloudflare tunnel. Use when asked to spin up, provision, launch, scale, add, or tear down EC2 instances, boxes, replicas, or the GPU fleet — and also when asked where the DemoBot app is, what its public URL or tunnel is, or for a link to the running demo. ALWAYS ask how many instances before provisioning — never assume a count. ALWAYS return the public URL as a markdown link with its access key.
---

# Spin up DemoBot EC2 GPU instances

Provisions N × `g5.xlarge` (NVIDIA A10G 24 GB) in the personal AWS account
(`177835492378`, profile `default`, `us-east-1`), each running the full DemoBot
stack behind its own Cloudflare tunnel and its own subdomain.

## RULE 0 — ask how many instances. Every time. No exceptions.

**Never assume the instance count.** Not from a previous run, not from how many
subdomains or tunnels already exist, not from a number mentioned earlier in the
conversation, not from documentation. Ask the user:

1. **How many instances?** (also confirm: type `g5.xlarge`, region `us-east-1`)
2. Then quote the cost for that count **before** provisioning:
   - ~$1.006/hour per box running
   - ~$742/month per box always-on
   - ~$185/month per box at 8 h weekdays (schedule installed)
3. Only proceed after the user confirms the count and cost.

`fleet.sh` enforces this at the tool level: `FLEET_COUNT` has **no default**, and
`preflight`/`provision` refuse to run without it. Do not work around that guard;
it exists so a stale shell variable can't launch a fleet.

## RULE 1 — always hand back the public URL, as a markdown link

Any time you spin up, deploy, restart, or report on a box — and whenever the user
asks where their app is — give them the clickable URL. Never make them ask twice,
and never make them assemble the hostname themselves.

**Required format** (label is the bare host, link target is `/app`, the chat UI):

```
[https://medadvice1.yeackbot.com/](https://medadvice1.yeackbot.com/app)
```

Include the box's access key alongside it — the URL is useless without it, since
every path except `/health` is gated.

`fleet.sh urls` prints exactly this, per box, already filled in:

```bash
FLEET_SG=sg-0a8482dbc4653403d ./deploy/ec2/fleet.sh urls
```

### Which X?

**Never guess, and never assume 1.** A replica number is claimed in four
independent places, and they can disagree — an instance can be terminated while
its tunnel, access key, and DNS record all survive. Reusing that number then
collides with the leftovers.

```bash
FLEET_SG=sg-0a8482dbc4653403d ./deploy/ec2/fleet.sh next-replica
#   claimed: 1
#   next:    2  ->  https://medadvice2.yeackbot.com/
```

`next-replica` unions all four sources — tagged EC2 instances, `demobot-N`
Cloudflare tunnels, `access-keys.env` entries — and returns the lowest unused
number. `provision` uses it automatically, so new boxes continue from what is
already claimed instead of restarting at 1.

## Architecture (why per-box subdomains)

```
medadviceN.yeackbot.com ──CNAME──> <tunnel-uuid-N>.cfargotunnel.com ──> box N
```

- One named tunnel (`demobot-N`) and one subdomain per box. The URL is the
  session pin — affinity is structural, so no load balancer, no affinity
  cookie, no paid Cloudflare add-on, and per-box SQLite is fine.
- Distinct `deployment.environment=demobot-ec2-N` per box separates replicas in
  Splunk O11y and Galileo. A collision silently merges two boxes into one
  apparent service and hides a sick box behind a healthy one.
- Distinct four-word `ACCESS_KEY` per box (Basic-auth gate), from
  `deploy/ec2/gen-access-keys.sh`.
- `medadvice.yeackbot.com` (no digit) stays on the Mac replica. Do not touch it.
- Failover is manual by design: a dead box takes down only its own subdomain
  (Cloudflare 1033); its group switches to a spare URL.

## Procedure

All from the repo root on the Mac. `N` below is the count the user gave.

```bash
# 0. session config (every new shell)
export FLEET_PROFILE=default FLEET_REGION=us-east-1 FLEET_TYPE=g5.xlarge
export FLEET_KEY_NAME=demobot FLEET_SSH_KEY=~/.ssh/demobot_ec2
export FLEET_SG=sg-0a8482dbc4653403d FLEET_HOSTNAME=medadvice.yeackbot.com
export FLEET_COUNT=N          # <- the user's answer, never a guess

# 0. which replica numbers are free? provision continues from here automatically
./deploy/ec2/fleet.sh next-replica

# 1. access keys (idempotent; never regenerates an existing replica's key).
#    Pass the HIGHEST replica number you will end up with, not the count.
#    Order no longer matters: claimed_replicas() ignores access-keys.env (see
#    the trap below), so this is safe before or after provision.
./deploy/ec2/gen-access-keys.sh <highest-replica>

# 2. checks: quota vs N, AMI, SG, your current IP vs the SG, key pair, cert.pem
./deploy/ec2/fleet.sh preflight

# 3. launch (prints cost + the exact URLs, asks for "yes")
./deploy/ec2/fleet.sh provision

# 4. bootstrap every box (parallel, ~10-20 min each; logs per replica)
./deploy/ec2/fleet.sh deploy
#    single box:  ./deploy/ec2/fleet.sh deploy 3

# 4b. NVIDIA variant — a box whose chat provider is a LOCAL NIM, plus the NemoClaw runtime.
#     Set BEFORE preflight/provision/deploy (deploy forwards --with-nim/--with-nemoclaw):
#       export FLEET_NIM=1 FLEET_NEMOCLAW=1 FLEET_VOLUME_GB=150
#     Needs NGC_API_KEY=nvapi-… (nvcr.io image pull) and NVIDIA_INFERENCE_API_KEY=nvapi-…
#     (the NemoClaw sandbox's own inference) in this Mac's .env — they ride in the
#     payload; push-replica refuses without them. Deploy takes 25-45 min (NIM pull).
#     Every agent then runs on nvidia/nvidia-nemotron-nano-9b-v2; Ollama stays installed
#     but holds no VRAM. Details: deploy/ec2/README.md "NVIDIA options".

# 5. start/stop schedule — Los Angeles time, NOT the script default (New York)
SCHED_TZ=America/Los_Angeles ./deploy/ec2/schedule.sh install

# 6. REQUIRED final step — report the URL to the user as a markdown link
#    (see RULE 1). Never end a spin-up without this.
./deploy/ec2/fleet.sh urls
```

Step 6 is not optional bookkeeping — it is the deliverable. The user's next
action is always "open the thing," so end every spin-up, restart, or status
report with the link and its access key:

> **[https://medadvice1.yeackbot.com/](https://medadvice1.yeackbot.com/app)** — access key `vine-grove-sledge-cabin`

Quota: G/VT On-Demand vCPUs (`L-DB2E81BA`), granted at 32 = max 8 × g5.xlarge.
`preflight` compares the quota against the requested N; if too low it prints the
increase command (approval takes hours to days — file it and wait).

## Validation (after any deploy — do not skip)

The bootstrap already enforces GPU placement (`--gpu require`) and health-checks
`https://medadviceN.yeackbot.com/health`. Additionally verify:

```bash
IP=$(./deploy/ec2/fleet.sh status | awk '/running/{print $4; exit}')
ssh -i ~/.ssh/demobot_ec2 ubuntu@$IP 'nvidia-smi --query-gpu=name --format=csv,noheader; ollama ps'
#   expect: NVIDIA A10G, and models at "100% GPU" — never a CPU split

# access key gates the box (expect 401 then 200)
curl -s -o /dev/null -w '%{http_code}\n' https://medadviceN.yeackbot.com/app
curl -su "x:$KEY" -o /dev/null -w '%{http_code}\n' https://medadviceN.yeackbot.com/app

# telemetry lands under this box's own environment
python3 tests/observability/check_o11y_metadata.py us1 "$O11Y_API" demobot-v3 demobot-ec2-N
```

**Measured on a real g5.xlarge, 2026-07-28** (not estimated — this is what a
healthy box looks like):

| | c6i.2xlarge (old) | g5.xlarge A10G | gain |
|---|---|---|---|
| decode | 13.6 tok/s | **85.1 tok/s** | 6.3× |
| prefill | ~28 tok/s (26 s for 729 tok) | **3852 tok/s** (650 tok in 0.17 s) | 138× |
| full app turn, via tunnel | ~40–60 s | **3 s** | ~15× |
| VRAM, both models resident | n/a | 11.1 GB of 23 GB | — |

A box materially below these numbers has silently fallen back to CPU — check
`sudo journalctl -u ollama | grep -i cuda`. Note the headroom: 11 GB of 23 GB
used means a third model, or a much larger context, fits without swapping.

## Traps (each has burned someone)

- **Silent CPU fallback.** A box with a broken CUDA runner passes every health
  check and is ~5× slower. This is why `deploy` passes `--gpu require` — never
  remove it on a GPU instance.
- **`git pull --ff-only` in the bootstrap cannot overwrite modified tracked
  files.** A re-deployed box silently keeps a previous session's
  `otel-collector-config.yaml` and `run-collector.sh` while `.env` IS replaced
  from the payload — so the config references env vars that no longer exist and
  the collector fails validation with `requires a non-empty "endpoint"`. Seen on
  box 1, 2026-07-29. Reset with `git -C ~/DemoBot checkout -- <file>` before
  re-patching; `patch-demobot-es-only.py` does this automatically.
- **Replica numbering came from access keys, not infrastructure.** Running
  `gen-access-keys.sh 8` before `provision` once made the next 7 boxes come up
  as replicas 9-15 instead of 2-8, because `claimed_replicas()` counted key
  entries as claims. Fixed — it now consults only live instances and tunnels.
  If numbering ever looks wrong, retagging is enough: the `Replica` tag drives
  the tunnel name, subdomain, and access key, so
  `aws ec2 create-tags --tags Key=Replica,Value=N Key=Name,Value=demobot-N`
  renumbers a box without relaunching it.
- **Never `cloudflared tunnel route dns <name>` — always pass the UUID.**
  Resolving a tunnel by *name* uses the default `~/.cloudflared/config.yml`,
  which pins `tunnel:` to the Mac's shared tunnel. Passing a name therefore
  binds the subdomain to the WRONG tunnel, and the symptom is a baffling empty
  404 from the other tunnel's catch-all while the box itself tests perfectly.
  Cost ~40 minutes on 2026-07-28. `push-replica.sh` now passes `$TUNNEL_ID`.
- **Don't pipe `fleet.sh deploy` through `tee`.** `tee`'s exit code masks the
  script's, so a failed deploy reports success. Use `set -o pipefail`, check
  `${PIPESTATUS[0]}`, or redirect instead of piping.
- **Free-tier account plans cannot launch GPU instances at all.** The error is
  `InvalidParameterCombination: not eligible for Free Tier`, which reads like a
  bad instance type but is an account-plan gate — unrelated to vCPU quota.
  Upgrade to the paid plan in Billing → Account.
- **`verify_observability.sh` hard-codes `demobot-local`.** Its tier 3 always
  fails on EC2 boxes. Call `check_o11y_metadata.py` directly with the box's
  environment name.
- **Stale DNS after teardown.** `terminate` kills instances but NOT Cloudflare
  objects. It prints the exact CNAMEs and tunnels to delete — do it, or dead
  subdomains serve 1033 error pages indefinitely.
- **Stopped ≠ free.** Stopped instances bill for EBS (~$8/mo per box). Fully
  gone requires `terminate`.
- **Public IPs change on stop/start.** Harmless for serving (tunnels dial out);
  re-read with `fleet.sh status` before ssh.
- **Schedule payloads bake in instance IDs.** Re-run `schedule.sh install` after
  any provision or terminate, or the schedule operates on dead IDs.
- **`ACCESS_KEY` is read at app startup.** Rotation
  (`gen-access-keys.sh --rotate N` + re-deploy) restarts the app and logs out
  every browser session on that box (cookie = sha256 of key).
- **Never copy `~/.cloudflared/cert.pem` to a box.** It is the Cloudflare
  account credential. `push-replica.sh` refuses; don't bypass it.
- **A NIM box cannot also keep Ollama models resident.** The Nemotron Nano NIM
  takes ~20 GB of the A10G's 23 GB; the fleet drop-in's 60-minute keep-alive
  leaves ~11 GB of Ollama in VRAM and the NIM fails to allocate. `--with-nim`
  sets `OLLAMA_KEEP_ALIVE=0` and unloads after the GPU check. Switching a NIM box
  back to `provider=ollama` in the UI while `demobot-nim` runs gives a slow
  CPU/GPU split, not a crash — `sudo systemctl stop demobot-nim` first.
- **`--with-nim` on 4.2.0 never set `AI_PROVIDER=nvidia`.** The override was
  prepended after the `.env` rewrite, so the box booted on the Mac's Ollama
  provider while the NIM sat idle. Fixed in 4.2.1 (decided in step 3);
  `tests/test_ec2_scripts.py` guards the order. If a NIM box answers with
  `provider_name=ollama`, check `grep ^AI_PROVIDER ~/DemoBot/.env` on the box.
- **First NIM start is a 30-40 GB download** (image + weights into
  `/opt/nim-cache`). `/v1/health/ready` stays non-200 until the engine is
  loaded; the bootstrap waits up to 40 min and prints the container's last log
  line every 30 s. A `BOOTSTRAP_INCOMPLETE` from the NIM check is re-runnable —
  the cache persists, the second run is fast.

## Teardown

```bash
./deploy/ec2/schedule.sh remove
./deploy/ec2/fleet.sh terminate     # asks twice, then prints the DNS + tunnel cleanup list
```

Then delete the printed CNAMEs (Cloudflare dashboard → DNS) and tunnels
(`cloudflared tunnel delete demobot-N`). `access-keys.env` keeps its entries so
a future fleet reuses the same keys; delete lines by hand if retiring replicas
permanently.
