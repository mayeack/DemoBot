#!/bin/bash
# Stage this Mac's secrets onto an EC2 host and bootstrap it into a running
# DemoBot replica. Run from the Mac, inside the repo:
#
#   ./deploy/ec2/push-replica.sh --host 35.175.173.5 --replica 2
#   ./deploy/ec2/push-replica.sh --host new.box --env-name demobot-ec2-lab
#   ./deploy/ec2/push-replica.sh --host new.box --stage-only     # copy, don't build
#
#   # fleet mode: give this box its OWN tunnel, on a GPU instance
#   ./deploy/ec2/push-replica.sh --host 1.2.3.4 --replica 3 --own-tunnel \
#       --user ubuntu --port 22 --gpu require
#
# TUNNEL MODES
#   default      all hosts run replicas of the ONE tunnel in ~/.cloudflared/
#                config.yml. Fine for a single host. Cloudflare treats every
#                replica of a UUID as one endpoint and does no steering, so with
#                two or more hosts a user lands on an arbitrary box each request
#                — which breaks conversation history, because state is per-box
#                SQLite.
#   --own-tunnel this box gets its own named tunnel ("demobot-<replica>") AND its
#                own subdomain ("medadvice<N>.yeackbot.com"), created as a CNAME
#                to <uuid>.cfargotunnel.com. The URL is then the session pin: a
#                user on medadvice3 cannot reach any other box, so affinity is
#                structural and needs no load balancer, no cookie, and no paid
#                Cloudflare add-on.
#
# Secrets cannot be fetched on the target — they exist only here. This script is
# the bridge: it assembles a payload directory, ships it over ssh, and runs
# deploy/ec2/ec2-bootstrap.sh against it. Payload contents:
#     .env                 this Mac's .env
#     config.yml           this Mac's ~/.cloudflared/config.yml (paths rewritten
#                          on the target, not here)
#     <tunnel-id>.json     the named tunnel's credentials
#     Modelfile.poisoned   the poisoned-model recipe, exported from local Ollama
#                          if there is no file on disk
#
# NEVER copied: ~/.cloudflared/cert.pem. That is the Cloudflare ACCOUNT
# credential — it can create, route, and delete tunnels across the whole zone.
# A replica only ever runs `tunnel run`, which needs the per-tunnel credentials
# JSON and nothing more. One compromised demo box should not equal a
# compromised Cloudflare account.
set -euo pipefail

HOST=""; USER_="splunk"; PORT="2222"; KEY="$HOME/.ssh/demobot_ec2"
ENV_NAME=""; REPLICA=""; STAGE_ONLY=false; NO_START=false
POISONED_TAG="mistral-nemo:12b-poisoned"
BASE_TAG="mistral-nemo:12b"
TUNNEL_NAME=""; OWN_TUNNEL=false; HOSTNAME_=""; GPU_MODE="auto"; NUM_PARALLEL=""
BOX_HOST=""            # this replica's own subdomain; derived from --hostname
NO_DNS=false           # skip the CNAME (record already managed elsewhere)

while [ $# -gt 0 ]; do
  case "$1" in
    --host)       HOST="$2"; shift 2 ;;
    --user)       USER_="$2"; shift 2 ;;
    --port)       PORT="$2"; shift 2 ;;
    --key)        KEY="$2"; shift 2 ;;
    --env-name)   ENV_NAME="$2"; shift 2 ;;
    --replica)    REPLICA="$2"; shift 2 ;;
    --own-tunnel) OWN_TUNNEL=true; shift ;;
    --tunnel-name) TUNNEL_NAME="$2"; OWN_TUNNEL=true; shift 2 ;;
    --hostname)   HOSTNAME_="$2"; shift 2 ;;
    --box-host)   BOX_HOST="$2"; shift 2 ;;
    --no-dns)     NO_DNS=true; shift ;;
    --gpu)        GPU_MODE="$2"; shift 2 ;;
    --num-parallel) NUM_PARALLEL="$2"; shift 2 ;;
    --poisoned-tag) POISONED_TAG="$2"; shift 2 ;;
    --stage-only) STAGE_ONLY=true; shift ;;
    --no-start)   NO_START=true; shift ;;
    -h|--help)    sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done
[ -n "$HOST" ] || { echo "error: --host is required (try --help)" >&2; exit 2; }
if [ "$OWN_TUNNEL" = true ] && [ -z "$TUNNEL_NAME" ]; then
  [ -n "$REPLICA" ] || { echo "error: --own-tunnel needs --replica N or --tunnel-name NAME" >&2; exit 2; }
  TUNNEL_NAME="demobot-$REPLICA"
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ServerAlive*: a mid-transfer network stall must FAIL (and be retried by the
# caller) rather than hang the deploy forever — observed in the wild 2026-07-28,
# a stage-only push sat 16 minutes on a stalled scp of five small files.
SSH_OPTS=(-o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
SSH=(ssh "${SSH_OPTS[@]}" -i "$KEY" -p "$PORT" "$USER_@$HOST")
SCP=(scp "${SSH_OPTS[@]}" -i "$KEY" -P "$PORT")

# --- preflight -------------------------------------------------------------
[ -f "$REPO/.env" ]                 || die "$REPO/.env not found"
CF="$HOME/.cloudflared/config.yml"
[ -f "$CF" ]                        || die "$CF not found — set up the named tunnel first (./setup-named-tunnel.sh)"
"${SSH[@]}" true || die "cannot ssh to $USER_@$HOST:$PORT with $KEY"

# Ingress hostname: taken from the Mac's config unless overridden. Every replica
# advertises the SAME hostname — that is what lets one load balancer front them.
[ -n "$HOSTNAME_" ] || HOSTNAME_=$(awk '/hostname:/{print $3; exit}' "$CF")
[ -n "$HOSTNAME_" ] || die "could not determine ingress hostname; pass --hostname"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [ "$OWN_TUNNEL" = true ]; then
  # --- per-box tunnel + subdomain (fleet mode) ------------------------------
  # Each box gets its own tunnel UUID and its own hostname. The hostname is what
  # pins a session to a box, so no load balancer and no affinity cookie are
  # involved. See deploy/ec2/README.md.
  command -v cloudflared >/dev/null || die "cloudflared not on PATH (needed to create the tunnel)"
  [ -f "$HOME/.cloudflared/cert.pem" ] || die "no ~/.cloudflared/cert.pem — run 'cloudflared tunnel login' first.
      cert.pem is the account credential and stays on this Mac; it is never shipped."

  TUNNEL_ID=$(cloudflared tunnel list --output json 2>/dev/null \
    | python3 -c "import json,sys
try: t=json.load(sys.stdin)
except Exception: t=[]
print(next((x['id'] for x in t if x.get('name')=='$TUNNEL_NAME'), ''))")

  if [ -z "$TUNNEL_ID" ]; then
    log "creating Cloudflare tunnel '$TUNNEL_NAME'"
    cloudflared tunnel create "$TUNNEL_NAME" >/dev/null || die "failed to create tunnel $TUNNEL_NAME"
    TUNNEL_ID=$(cloudflared tunnel list --output json 2>/dev/null \
      | python3 -c "import json,sys
print(next((x['id'] for x in json.load(sys.stdin) if x.get('name')=='$TUNNEL_NAME'), ''))")
    [ -n "$TUNNEL_ID" ] || die "tunnel $TUNNEL_NAME created but its UUID could not be read"
  else
    log "reusing existing tunnel '$TUNNEL_NAME'"
  fi
  echo "  tunnel: $TUNNEL_NAME = $TUNNEL_ID"

  CREDS="$HOME/.cloudflared/$TUNNEL_ID.json"
  [ -f "$CREDS" ] || die "credentials $CREDS not found for tunnel $TUNNEL_NAME"

  # Per-box hostname: medadvice.yeackbot.com + replica 3 -> medadvice3.yeackbot.com
  if [ -z "$BOX_HOST" ]; then
    _label="${HOSTNAME_%%.*}"          # medadvice
    _domain="${HOSTNAME_#*.}"          # yeackbot.com
    [ "$_label" = "$_domain" ] && die "cannot derive a subdomain from '$HOSTNAME_'; pass --box-host"
    BOX_HOST="${_label}${REPLICA}.${_domain}"
  fi
  echo "  hostname: $BOX_HOST"

  # Bind the subdomain to this tunnel. Writes a CNAME to <uuid>.cfargotunnel.com
  # on the existing zone — ordinary DNS, no Load Balancing add-on. Re-running for
  # a record that already points here is a no-op; a record pointing somewhere
  # ELSE is a real conflict and must be resolved by hand, so warn rather than
  # silently continue with a box nobody can reach.
  # Route by UUID, never by name. `cloudflared tunnel route dns <name> …`
  # resolves the name against the DEFAULT config file (~/.cloudflared/config.yml),
  # which pins `tunnel:` to the Mac's shared tunnel — so passing a name silently
  # binds the subdomain to the WRONG tunnel and the box answers an empty 404 from
  # the other tunnel's catch-all. The UUID is unambiguous.
  if [ "$NO_DNS" = true ]; then
    echo "  dns: skipped (--no-dns)"
  # --overwrite-dns because medadviceN is owned by replica N by definition, and a
  # redeploy always mints a fresh tunnel UUID. Without it, a leftover record from
  # a previous instance still points at a DELETED tunnel: route dns fails with
  # "already exists", the box comes up healthy, and every request gets a
  # Cloudflare 530. Seen for real after the 2026-07-28 teardown.
  elif out=$(cloudflared tunnel route dns --overwrite-dns "$TUNNEL_ID" "$BOX_HOST" 2>&1); then
    echo "  dns: $BOX_HOST -> $TUNNEL_ID.cfargotunnel.com"
  elif printf '%s' "$out" | grep -qi 'already exists\|record with that host'; then
    warn "a DNS record for $BOX_HOST already exists.
      If it points at a DIFFERENT tunnel this box will be unreachable. Verify:
        dig +short CNAME $BOX_HOST     (expect $TUNNEL_ID.cfargotunnel.com)"
  else
    die "could not route $BOX_HOST to tunnel $TUNNEL_NAME:
      $out"
  fi

  # Purpose-built config for THIS box, advertising only its own hostname.
  cat > "$STAGE/config.yml" <<CFG
tunnel: $TUNNEL_ID
credentials-file: $HOME/.cloudflared/$TUNNEL_ID.json
protocol: http2

ingress:
  - hostname: $BOX_HOST
    service: http://localhost:8001
  - service: http_status:404
CFG
else
  BOX_HOST="$HOSTNAME_"
  # --- shared tunnel (legacy 1-2 replica mode) ------------------------------
  TUNNEL_ID=$(awk '/^tunnel:/{print $2; exit}' "$CF")
  [ -n "$TUNNEL_ID" ]               || die "no 'tunnel:' line in $CF"
  CREDS="$HOME/.cloudflared/$TUNNEL_ID.json"
  [ -f "$CREDS" ]                   || die "tunnel credentials $CREDS not found"
  install -m 600 "$CF" "$STAGE/config.yml"
  [ -n "$REPLICA" ] && [ "$REPLICA" -gt 2 ] 2>/dev/null && \
    warn "replica $REPLICA on a SHARED tunnel: no session affinity is possible and
      a user will land on a different box most requests. Use --own-tunnel."
fi

# --- Modelfile: prefer the repo's canonical recipe, else on-disk, else export --
# Exporting guarantees every replica builds the SAME poisoned model. Pulling is
# impossible (it is in no registry) and hand-copying is how the Mac and EC2
# drifted to different model IDs in the first place.
#
# The canonical recipe is the one scripts/demo/build_poisoned_model.sh builds
# from: models/<tag-with-dashes>.Modelfile. It is searched FIRST so a stale
# Modelfile.poisoned cannot outrank it — a dolphin-era file survived the
# 2026-08-11 move to mistral-nemo in exactly these two legacy locations, and
# would have rebuilt the wrong base model onto an otherwise all-Mistral box.
CANONICAL_MODELFILE="$REPO/models/${POISONED_TAG//:/-}.Modelfile"
MODELFILE=""
for cand in "$CANONICAL_MODELFILE" "$REPO/deploy/ec2/Modelfile.poisoned" "$HOME/Modelfile.poisoned"; do
  [ -f "$cand" ] && { MODELFILE="$cand"; break; }
done

if [ -n "$MODELFILE" ]; then
  log "Modelfile: $MODELFILE"
  cp "$MODELFILE" "$STAGE/Modelfile.poisoned"
else
  command -v ollama >/dev/null || die "no Modelfile.poisoned on disk and no ollama CLI to export one"
  log "exporting Modelfile from local Ollama: $POISONED_TAG"
  ollama show --modelfile "$POISONED_TAG" > "$STAGE/Modelfile.poisoned" \
    || die "ollama show --modelfile $POISONED_TAG failed — is the model built on this Mac?"
fi

# `ollama show` sometimes emits `FROM /path/to/blob` instead of the parent tag.
# A blob path is meaningless on another host, so normalise it to the base tag.
FROM_LINE=$(awk '/^[[:space:]]*FROM[[:space:]]+/{print $2; exit}' "$STAGE/Modelfile.poisoned")
case "$FROM_LINE" in
  /*) echo "  normalising blob FROM ($FROM_LINE) -> $BASE_TAG"
      sed -i '' -E "s|^([[:space:]]*FROM[[:space:]]+).*|\1$BASE_TAG|" "$STAGE/Modelfile.poisoned" ;;
  "") die "no FROM line in the exported Modelfile" ;;
  *)  echo "  base model: $FROM_LINE" ;;
esac

# A recipe whose FROM is not $BASE_TAG builds a different model than the rest of
# the fleet runs — and does it silently, passing every health check. Fail loudly.
FROM_LINE=$(awk '/^[[:space:]]*FROM[[:space:]]+/{print $2; exit}' "$STAGE/Modelfile.poisoned")
[ "$FROM_LINE" = "$BASE_TAG" ] || die "poisoned recipe builds FROM '$FROM_LINE', not '$BASE_TAG'
  recipe used: ${MODELFILE:-<exported from local Ollama>}
  fix: use the repo copy at $CANONICAL_MODELFILE, or delete the stale recipe"

# --- per-replica .env overrides --------------------------------------------
# Keys that must differ per box. Written to a payload FILE rather than passed as
# --set arguments, because argv is readable by every user on the target via `ps`
# and ACCESS_KEY is a live credential.
: > "$STAGE/overrides.env"; chmod 600 "$STAGE/overrides.env"

KEYFILE="$REPO/deploy/ec2/access-keys.env"
if [ -n "$REPLICA" ]; then
  if [ -f "$KEYFILE" ] && ak=$(grep -E "^ACCESS_KEY_$REPLICA=" "$KEYFILE" | head -1 | cut -d= -f2-) \
     && [ -n "$ak" ]; then
    printf 'ACCESS_KEY=%s\n' "$ak" >> "$STAGE/overrides.env"
    echo "  access key: replica $REPLICA (from access-keys.env)"
  else
    warn "no ACCESS_KEY_$REPLICA in $KEYFILE — this box will inherit the Mac's shared key.
      Generate one:  ./deploy/ec2/gen-access-keys.sh $REPLICA"
  fi
fi

# --- assemble payload ------------------------------------------------------
# config.yml was already written above — per-box in fleet mode, copied from the
# Mac in legacy shared-tunnel mode.
log "assembling payload (tunnel $TUNNEL_ID)"
install -m 600 "$REPO/.env"  "$STAGE/.env"
chmod 600 "$STAGE/config.yml"
install -m 600 "$CREDS"      "$STAGE/$TUNNEL_ID.json"
chmod 644 "$STAGE/Modelfile.poisoned"
ls -lA "$STAGE"      # -A: .env is a dotfile and must show in this audit listing
# Belt and braces: the account cert must never reach a replica.
[ -e "$STAGE/cert.pem" ] && die "cert.pem must never be staged to a replica"

# --- ship ------------------------------------------------------------------
log "shipping to $USER_@$HOST"
"${SSH[@]}" 'rm -rf ~/demobot-payload && mkdir -m 700 -p ~/demobot-payload'

# Clear the remote payload on EVERY exit path, not just success. The payload
# holds .env, the tunnel credentials, and ACCESS_KEY; under `set -e` a failing
# bootstrap aborts this script, and a success-path-only cleanup would leave all
# of it sitting on the box. Observed for real on 2026-07-28.
remote_cleanup() { "${SSH[@]}" 'rm -rf ~/demobot-payload' 2>/dev/null || true; }
trap 'rm -rf "$STAGE"; remote_cleanup' EXIT
"${SCP[@]}" -q "$STAGE/.env" "$STAGE/config.yml" "$STAGE/$TUNNEL_ID.json" \
               "$STAGE/Modelfile.poisoned" "$STAGE/overrides.env" \
               "$USER_@$HOST:~/demobot-payload/"
"${SCP[@]}" -q "$REPO/deploy/ec2/ec2-bootstrap.sh" "$USER_@$HOST:~/ec2-bootstrap.sh"
"${SSH[@]}" 'chmod 700 ~/ec2-bootstrap.sh && chmod 600 ~/demobot-payload/*'

if [ "$STAGE_ONLY" = true ]; then
  # --stage-only deliberately LEAVES the payload on the box for a manual
  # finish, so drop the remote half of the EXIT cleanup before exiting.
  # Without this the trap wipes ~/demobot-payload on the way out and the
  # follow-up command below dies at ec2-bootstrap.sh's payload preflight.
  trap 'rm -rf "$STAGE"' EXIT
  echo; echo "Staged. Finish on the target with:"
  echo "  ssh -i $KEY -p $PORT $USER_@$HOST '~/ec2-bootstrap.sh'"
  exit 0
fi

# --- bootstrap -------------------------------------------------------------
ARGS=()
[ -n "$ENV_NAME" ] && ARGS+=(--env-name "$ENV_NAME")
[ -n "$REPLICA" ]  && ARGS+=(--replica  "$REPLICA")
[ -n "$GPU_MODE" ] && ARGS+=(--gpu "$GPU_MODE")
[ -n "$NUM_PARALLEL" ] && ARGS+=(--num-parallel "$NUM_PARALLEL")
[ "$NO_START" = true ] && ARGS+=(--no-start)

log "running bootstrap on $HOST"
"${SSH[@]}" "~/ec2-bootstrap.sh ${ARGS[*]}"

# The payload holds live secrets; it has served its purpose. (The EXIT trap also
# does this, so a failed bootstrap above still leaves nothing behind.)
log "clearing payload"
remote_cleanup

echo
echo "Replica ${REPLICA:-?} is live at:  https://$BOX_HOST"
[ -s "$STAGE/overrides.env" ] && grep -q '^ACCESS_KEY=' "$STAGE/overrides.env" && \
  echo "  access key: ./deploy/ec2/gen-access-keys.sh --show"
echo "Done."
