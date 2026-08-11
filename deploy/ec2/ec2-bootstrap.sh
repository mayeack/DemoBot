#!/bin/bash
# DemoBot EC2 bootstrap — run as the service user (default 'splunk') on Ubuntu
# 22.04. Builds a complete, RUNNING replica: prereqs, Ollama + every model the
# .env asks for (including the locally-built poisoned model), DemoBot, the OTel
# collector, the Cloudflare tunnel, and the systemd units.
#
#   ./ec2-bootstrap.sh                      # payload at ~/demobot-payload
#   ./ec2-bootstrap.sh --replica 2          # deployment.environment=demobot-ec2-2
#   ./ec2-bootstrap.sh --env-name demobot-ec2-lab
#   ./ec2-bootstrap.sh --gpu require        # abort unless inference lands on the GPU
#   ./ec2-bootstrap.sh --num-parallel 4     # Ollama concurrent request slots
#   ./ec2-bootstrap.sh --set OLLAMA_MODEL=mistral-nemo:12b-poisoned   # .env override
#   ./ec2-bootstrap.sh --no-start           # install everything, don't start
#
# PER-REPLICA .env OVERRIDES: the .env shipped from the Mac is identical on every
# box; a few keys must differ. Overrides come from $PAYLOAD/overrides.env (one
# KEY=VALUE per line) and from repeatable --set flags, with --set winning.
# Secrets belong in overrides.env, NOT in --set: argv is visible to every user on
# the box via `ps`.
#
# GPU: --gpu auto (default) uses the GPU when nvidia-smi is present and warns if
# a detected GPU is not actually used; --gpu require turns that warning into a
# hard failure; --gpu off skips the checks for a CPU box. On a g5/g6 instance
# ALWAYS pass --gpu require: a silent CPU fallback still passes every health
# check and is ~5x slower, which is the single most likely way to arrive at a
# workshop with a box that looks fine and is not.
#
# SECRETS: this script does NOT invent secrets. It consumes a payload directory
# staged by deploy/ec2/push-replica.sh (run from the Mac), containing:
#     .env                 the Mac's .env, verbatim
#     config.yml           the Mac's ~/.cloudflared/config.yml, verbatim
#     <tunnel-id>.json     the named tunnel's credentials
#     Modelfile.poisoned   recipe for the guardrail-failure demo model
# The Cloudflare account cert.pem is deliberately NOT part of the payload and
# must never be copied to a replica (see step 6).
#
# Idempotent: safe to re-run. Re-running picks up an updated payload.
#
# NOTE: no `set -x`. The original bootstrap used `set -euxo pipefail`, which
# echoed every line — including .env contents — to the console and to any
# capturing log. Tracing is opt-in via DEBUG=1.
set -euo pipefail
[ "${DEBUG:-0}" = "1" ] && set -x

PAYLOAD="$HOME/demobot-payload"
ENV_NAME=""
REPLICA=""
START=true
GPU_MODE="auto"          # auto | require | off
NUM_PARALLEL=""
SETS=()
REPO_URL="https://github.com/mayeack/DemoBot.git"
REPO="$HOME/DemoBot"

while [ $# -gt 0 ]; do
  case "$1" in
    --payload)   PAYLOAD="$2"; shift 2 ;;
    --env-name)  ENV_NAME="$2"; shift 2 ;;
    --replica)   REPLICA="$2";  shift 2 ;;
    --gpu)       GPU_MODE="$2"; shift 2 ;;
    --num-parallel) NUM_PARALLEL="$2"; shift 2 ;;
    --set)       SETS+=("$2");  shift 2 ;;
    --no-start)  START=false;   shift ;;
    -h|--help)
      sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done
case "$GPU_MODE" in auto|require|off) ;; *) echo "--gpu must be auto|require|off" >&2; exit 2 ;; esac

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  CF_ARCH=amd64; OTEL_ARCH=amd64 ;;
  aarch64) CF_ARCH=arm64; OTEL_ARCH=arm64 ;;
  *) die "unsupported arch $ARCH" ;;
esac

# --- 0. payload preflight --------------------------------------------------
# Fail here, loudly, rather than three minutes later with half a replica up.
[ -d "$PAYLOAD" ] || die "payload dir $PAYLOAD not found — run deploy/ec2/push-replica.sh from the Mac first"
for f in .env config.yml Modelfile.poisoned; do
  [ -f "$PAYLOAD/$f" ] || die "payload incomplete: $PAYLOAD/$f missing"
done
TUNNEL_ID=$(awk '/^tunnel:/{print $2; exit}' "$PAYLOAD/config.yml")
[ -n "$TUNNEL_ID" ] || die "no 'tunnel:' line in $PAYLOAD/config.yml"
[ -f "$PAYLOAD/$TUNNEL_ID.json" ] || die "payload incomplete: tunnel credentials $TUNNEL_ID.json missing"

# --- 1. base packages + Python 3.11 ---------------------------------------
# 3.11 from deadsnakes: Ubuntu 22.04 ships 3.10, on which LangChain 1.x fails.
log "base packages + Python 3.11"
export DEBIAN_FRONTEND=noninteractive
sudo -E apt-get update -y
sudo -E apt-get install -y git lsof curl jq software-properties-common
sudo -E add-apt-repository -y ppa:deadsnakes/ppa
sudo -E apt-get install -y python3.11 python3.11-venv python3.11-dev

# --- 2. cloudflared --------------------------------------------------------
log "cloudflared"
if ! command -v cloudflared >/dev/null; then
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}.deb" -o /tmp/cloudflared.deb
  sudo dpkg -i /tmp/cloudflared.deb
fi
cloudflared --version

# --- 3. GPU detection ------------------------------------------------------
# Decide GPU vs CPU BEFORE writing the Ollama drop-in, because the tuning
# differs: on a 24 GB A10G every model co-resides and requests can run in
# parallel; on CPU they contend for a handful of physical cores.
GPU=false
if [ "$GPU_MODE" != "off" ] && command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null; then
    GPU=true
  else
    warn "nvidia-smi present but returned an error — driver not loaded?"
  fi
fi
if [ "$GPU_MODE" = "require" ] && [ "$GPU" != true ]; then
  die "--gpu require but no working GPU found.
      On a Deep Learning AMI the driver is preinstalled; on plain Ubuntu run:
        sudo apt-get install -y nvidia-driver-535 && sudo reboot
      then re-run this script."
fi
log "GPU: $([ "$GPU" = true ] && echo yes || echo 'no — CPU inference')"

# --- 4. Ollama daemon ------------------------------------------------------
log "Ollama daemon"
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh   # installs + enables systemd unit
fi
# Keep the 3B internal model and the 8B synthesizer resident so a turn never
# pays a cold reload. On GPU there is VRAM for all three models at once and
# concurrency is cheap; on CPU, extra parallelism only causes core contention.
if [ "$GPU" = true ]; then
  LOADED=3; KEEP="60m"; PARALLEL="${NUM_PARALLEL:-4}"
else
  LOADED=2; KEEP="30m"; PARALLEL="${NUM_PARALLEL:-1}"
fi
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/keepalive.conf >/dev/null <<DROPIN
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=$LOADED"
Environment="OLLAMA_KEEP_ALIVE=$KEEP"
Environment="OLLAMA_NUM_PARALLEL=$PARALLEL"
DROPIN
sudo systemctl daemon-reload
sudo systemctl restart ollama       # restart, not start: pick up a changed drop-in
sudo systemctl enable ollama
for _ in $(seq 1 30); do
  curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://localhost:11434/api/tags >/dev/null || die "Ollama did not come up on :11434"

# Ollama logs its detected compute backend at startup. This catches the case
# where the driver exists but Ollama built/loaded a CPU-only runner.
if [ "$GPU" = true ]; then
  sleep 2
  if sudo journalctl -u ollama --since '2 min ago' --no-pager 2>/dev/null \
       | grep -qiE 'library=cuda|inference compute.*cuda'; then
    log "Ollama reports a CUDA backend"
  elif [ "$GPU_MODE" = "require" ]; then
    die "GPU present but Ollama did not report a CUDA backend.
      Inspect: sudo journalctl -u ollama -n 100 --no-pager | grep -i 'compute\|cuda\|rocm'"
  else
    warn "GPU present but Ollama did not report a CUDA backend — inference may be on CPU"
  fi
fi

# --- 5. DemoBot repo + venv ------------------------------------------------
log "DemoBot repo + venv"
if [ ! -d "$REPO/.git" ]; then git clone "$REPO_URL" "$REPO"; fi
cd "$REPO"
git pull --ff-only || warn "git pull failed (local changes?) — continuing with the current checkout"
python3.11 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# --- 6. OTel collector binary ---------------------------------------------
log "OTel collector binary"
mkdir -p bin
if [ ! -x bin/otelcol-contrib ]; then
  OTEL_VER=$(curl -fsSL https://api.github.com/repos/open-telemetry/opentelemetry-collector-releases/releases/latest | grep -oP '"tag_name":\s*"v\K[0-9.]+' | head -1)
  curl -fsSL "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTEL_VER}/otelcol-contrib_${OTEL_VER}_linux_${OTEL_ARCH}.tar.gz" -o /tmp/otelcol.tgz
  tar -xzf /tmp/otelcol.tgz -C bin otelcol-contrib
fi
bin/otelcol-contrib --version
# The apt splunk-otel-collector (present on the Splunk lab AMI) binds the same
# :4317 and silently steals the app's telemetry. Ours is the only collector.
if systemctl list-unit-files 2>/dev/null | grep -q '^splunk-otel-collector\.service'; then
  sudo systemctl mask --now splunk-otel-collector 2>/dev/null || true
  warn "masked the apt splunk-otel-collector (port :4317 collision)"
fi

# --- 7. secrets: .env, tunnel credentials, tunnel config -------------------
log "installing secrets from $PAYLOAD"
install -m 600 "$PAYLOAD/.env" "$REPO/.env"

mkdir -p "$HOME/.cloudflared"; chmod 700 "$HOME/.cloudflared"
install -m 600 "$PAYLOAD/$TUNNEL_ID.json" "$HOME/.cloudflared/$TUNNEL_ID.json"

# The Mac's config.yml carries Mac paths (/Users/<you>/.cloudflared/...). Copy it
# verbatim EXCEPT credentials-file, which is rewritten to this host's $HOME —
# the single line that makes a Mac config valid on Linux.
sed -E "s|^([[:space:]]*credentials-file:[[:space:]]*).*|\1$HOME/.cloudflared/$TUNNEL_ID.json|" \
    "$PAYLOAD/config.yml" > "$HOME/.cloudflared/config.yml"
chmod 600 "$HOME/.cloudflared/config.yml"
grep -q "^credentials-file: $HOME/" "$HOME/.cloudflared/config.yml" \
  || die "credentials-file rewrite failed — check $PAYLOAD/config.yml formatting"

# A replica runs `tunnel run` only, which needs the credentials JSON and nothing
# else. cert.pem is the ACCOUNT credential (it can create/delete/route tunnels
# org-wide) and belongs on the Mac alone.
if [ -f "$HOME/.cloudflared/cert.pem" ]; then
  warn "cert.pem is present on this replica — it should not be. Remove it: rm ~/.cloudflared/cert.pem"
fi

# --- 8. per-replica .env overrides ----------------------------------------
# The .env arrives from the Mac identical for every box. A handful of keys must
# differ per replica; this applies them as a general override map rather than
# special-casing one key.
#
#   deployment.environment  what separates this replica from every other one in
#                           Splunk O11y and Galileo. Two boxes sharing a value
#                           silently merge into one apparent service.
#   ACCESS_KEY              per-box Basic-auth gate.
#   OLLAMA_MODEL            lets one box run the poisoned model and another the
#                           clean one — impossible under a load balancer, which
#                           is why the subdomain topology enables it.
#
# Secret-bearing overrides arrive in $PAYLOAD/overrides.env, NOT on the command
# line: argv is world-readable via /proc and `ps`, so an ACCESS_KEY passed as an
# argument would leak to every user on the box for the life of the process.
if [ -z "$ENV_NAME" ]; then
  if [ -n "$REPLICA" ]; then
    ENV_NAME="demobot-ec2-$REPLICA"
  else
    # Collision-free default derived from the instance id.
    TOK=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
          -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
    IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOK" \
          http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)
    ENV_NAME="demobot-ec2-${IID##i-}"
    [ "$ENV_NAME" = "demobot-ec2-" ] && ENV_NAME="demobot-ec2-$(hostname -s)"
  fi
fi
log "deployment.environment = $ENV_NAME"

OVERRIDES=$(mktemp); chmod 600 "$OVERRIDES"
trap 'rm -f "$OVERRIDES"' EXIT
[ -f "$PAYLOAD/overrides.env" ] && cat "$PAYLOAD/overrides.env" >> "$OVERRIDES"
if [ ${#SETS[@]} -gt 0 ]; then
  printf '%s\n' "${SETS[@]}" >> "$OVERRIDES"
fi

python3 - "$REPO/.env" "$ENV_NAME" "$OVERRIDES" <<'PY'
import sys

path, envname, ovr_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Load overrides: KEY=VALUE per line, blanks and #comments ignored. Later
# entries win, so a --set can deliberately beat a payload value.
overrides = {}
try:
    for raw in open(ovr_path):
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        overrides[k.strip()] = v
except FileNotFoundError:
    pass


def rewrite_resource_attrs(value, envname):
    """Set deployment.environment inside the comma-separated attribute list,
    preserving every sibling attribute."""
    attrs, hit = [], False
    for pair in [p for p in value.split(",") if p.strip()]:
        k, _, _v = pair.partition("=")
        if k.strip() == "deployment.environment":
            attrs.append(f"deployment.environment={envname}")
            hit = True
        else:
            attrs.append(pair.strip())
    if not hit:
        attrs.insert(0, f"deployment.environment={envname}")
    return ",".join(attrs)


lines = open(path).read().splitlines(keepends=True)
out, applied, seen_ra = [], [], False

for line in lines:
    key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
    if key == "OTEL_RESOURCE_ATTRIBUTES":
        seen_ra = True
        new = rewrite_resource_attrs(line.split("=", 1)[1].strip(), envname)
        out.append(f"OTEL_RESOURCE_ATTRIBUTES={new}\n")
        applied.append(("deployment.environment", envname))
    elif key in overrides:
        out.append(f"{key}={overrides[key]}\n")
        applied.append((key, overrides.pop(key)))
    else:
        out.append(line)

# Keys not already present get appended.
if out and not out[-1].endswith("\n"):
    out[-1] += "\n"
if not seen_ra:
    out.append(f"OTEL_RESOURCE_ATTRIBUTES=deployment.environment={envname}\n")
    applied.append(("deployment.environment", envname))
for k, v in overrides.items():
    out.append(f"{k}={v}\n")
    applied.append((k, v))

open(path, "w").writelines(out)

# Report what changed, masking anything that looks like a credential.
SECRETISH = ("KEY", "TOKEN", "SECRET", "PASSWORD")
for k, v in applied:
    shown = "********" if any(s in k.upper() for s in SECRETISH) else v
    print(f"  {k} = {shown}")
PY
rm -f "$OVERRIDES"; trap - EXIT

# --- 9. models: pull every model .env references, build the poisoned one ---
# The model set is DERIVED FROM .env (OLLAMA_MODEL, OLLAMA_MODEL_INTERNAL, and
# any other OLLAMA_MODEL_* key) rather than hard-coded, so adding an internal
# agent to .env is enough — no bootstrap edit, no hand-pull on each replica.
log "Ollama models"
mapfile -t WANTED < <(
  grep -E '^OLLAMA_MODEL(_[A-Z0-9_]+)?=' "$REPO/.env" \
    | cut -d= -f2- | tr -d '"'\''' | sed 's/[[:space:]]*$//' | grep -v '^$' | sort -u
)
[ ${#WANTED[@]} -gt 0 ] || warn "no OLLAMA_MODEL* keys in .env — nothing to pull"

# The poisoned model is BUILT, never pulled: it does not exist in any registry.
# Its tag is whatever .env asks for; fall back to the conventional name.
POISONED_TAG=""
for m in "${WANTED[@]}"; do
  case "$m" in *poisoned*) POISONED_TAG="$m" ;; esac
done
[ -n "$POISONED_TAG" ] || POISONED_TAG="mistral-nemo:12b-poisoned"

# `ollama create` needs the FROM base resident first.
BASE=$(awk '/^[[:space:]]*FROM[[:space:]]+/{print $2; exit}' "$PAYLOAD/Modelfile.poisoned")
[ -n "$BASE" ] || die "no FROM line in $PAYLOAD/Modelfile.poisoned"
case "$BASE" in
  /*) die "Modelfile.poisoned has a blob path as its FROM ($BASE), not a tag.
      Re-export it on the Mac with an explicit base:
        sed -i '' \"s|^FROM .*|FROM mistral-nemo:12b|\" Modelfile.poisoned" ;;
esac

for m in "${WANTED[@]}" "$BASE"; do
  case "$m" in *poisoned*) continue ;; esac        # built below, not pullable
  log "ollama pull $m"
  ollama pull "$m"
done

log "ollama create $POISONED_TAG (from $BASE)"
install -m 644 "$PAYLOAD/Modelfile.poisoned" "$HOME/Modelfile.poisoned"
ollama create "$POISONED_TAG" -f "$HOME/Modelfile.poisoned"
ollama list

# The decisive GPU check. Backend logs can say CUDA while a specific model still
# lands on CPU (VRAM pressure, unsupported quant). `ollama ps` reports where the
# weights actually are, so warm one model and read the PROCESSOR column.
if [ "$GPU" = true ]; then
  WARM="${WANTED[0]:-$BASE}"
  log "warming $WARM to confirm GPU placement"
  curl -sf http://localhost:11434/api/generate \
       -d "{\"model\":\"$WARM\",\"prompt\":\"hi\",\"stream\":false}" >/dev/null || \
    warn "warm-up request failed for $WARM"
  PLACEMENT=$(ollama ps 2>/dev/null | awk 'NR==2{for(i=1;i<=NF;i++) if($i ~ /%$/) {print $i" "$(i+1); exit}}')
  echo "  placement: ${PLACEMENT:-unknown}"
  # Order matters. Ollama reports a partial offload as "48%/52% CPU/GPU", which
  # contains "GPU" but means the model did NOT fit in VRAM and is running split
  # — nearly as slow as pure CPU. Test for any CPU share BEFORE accepting GPU.
  case "$PLACEMENT" in
    "100% GPU")
      log "confirmed: $WARM is fully resident on the GPU" ;;
    *CPU*)
      MSG="$WARM is not fully on the GPU (${PLACEMENT}).
      A CPU share means the model did not fit in VRAM, or the runner is CPU-only.
      Check VRAM:   nvidia-smi --query-gpu=memory.used,memory.total --format=csv
      Check runner: sudo journalctl -u ollama -n 100 --no-pager | grep -i cuda
      Check config: OLLAMA_MAX_LOADED_MODELS=$LOADED may be too many for this GPU"
      [ "$GPU_MODE" = "require" ] && die "$MSG" || warn "$MSG" ;;
    *GPU*)
      log "confirmed: $WARM is on the GPU (${PLACEMENT})" ;;
    *) warn "could not read model placement from 'ollama ps' — verify manually with: ollama ps" ;;
  esac
  nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader || true
fi

# --- 10. systemd units ------------------------------------------------------
log "systemd units"
SVC_USER=$(id -un)

sudo tee /etc/systemd/system/demobot-collector.service >/dev/null <<UNIT
[Unit]
Description=DemoBot OTel Collector (-> Splunk O11y + Galileo)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$REPO
ExecStart=$REPO/run-collector.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/demobot-app.service >/dev/null <<UNIT
[Unit]
Description=DemoBot app (uvicorn :8001)
After=network-online.target ollama.service demobot-collector.service
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$REPO
Environment=PATH=$REPO/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash $REPO/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/demobot-tunnel.service >/dev/null <<UNIT
[Unit]
Description=DemoBot Cloudflare named tunnel (medadvice replica)
After=network-online.target demobot-app.service
Wants=network-online.target

[Service]
User=$SVC_USER
ExecStart=/usr/bin/cloudflared --config $HOME/.cloudflared/config.yml tunnel run $TUNNEL_ID
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload

# --- 11. start + verify ----------------------------------------------------
if [ "$START" != true ]; then
  echo; echo "BOOTSTRAP_OK (units installed, not started — --no-start)"
  exit 0
fi

log "starting services"
sudo systemctl enable --now demobot-collector demobot-app demobot-tunnel
# Collector must own :4317 before the app's first spans; units encode the
# ordering, this is just the settling time.
sleep 10

log "verify"
rc=0
check() { c=$(curl -s -o /dev/null -w '%{http_code}' "$2" 2>/dev/null || echo 000)
          printf '  %-34s %s\n' "$1" "$c"; [ "$c" = "200" ] || rc=1; }
check "app      /health"        http://localhost:8001/health
check "ollama   /api/tags"      http://localhost:11434/api/tags
check "otelcol  :8888/metrics"  http://localhost:8888/metrics
HOSTN=$(awk '/hostname:/{print $3; exit}' "$HOME/.cloudflared/config.yml")
[ -n "$HOSTN" ] && check "public   https://$HOSTN" "https://$HOSTN/health"

echo
if [ $rc -eq 0 ]; then
  echo "BOOTSTRAP_OK — replica live as deployment.environment=$ENV_NAME"
  echo
  echo "Confirm telemetry is landing under this replica's own environment:"
  echo "  cd $REPO && python3 tests/observability/check_o11y_metadata.py \\"
  echo "      us1 \"\$(grep '^SPLUNK_API_TOKEN=' .env | cut -d= -f2-)\" demobot-v3 $ENV_NAME"
  echo "  (verify_observability.sh hard-codes demobot-local, so it is wrong on a replica)"
else
  echo "BOOTSTRAP_INCOMPLETE — a health check failed. Logs:"
  echo "  journalctl -u demobot-app -u demobot-collector -u demobot-tunnel -n 50 --no-pager"
  exit 1
fi
