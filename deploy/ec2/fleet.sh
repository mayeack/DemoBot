#!/bin/bash
# Manage the DemoBot GPU fleet: provision, deploy, schedule, and tear down N
# g5.xlarge replicas in your own AWS account.
#
#   ./deploy/ec2/fleet.sh preflight          # quota + credentials + AMI checks
#   ./deploy/ec2/fleet.sh provision          # launch the instances (asks first)
#   ./deploy/ec2/fleet.sh deploy [replica]   # bootstrap all, or just one box
#   ./deploy/ec2/fleet.sh urls               # per-box public URLs + access keys
#   ./deploy/ec2/fleet.sh next-replica       # lowest unused replica number
#   ./deploy/ec2/fleet.sh status             # instance + tunnel state
#   ./deploy/ec2/fleet.sh start | stop       # manual lifecycle
#   ./deploy/ec2/fleet.sh terminate          # destroy (asks twice)
#
# FLEET_COUNT HAS NO DEFAULT — ON PURPOSE. Provisioning GPU instances spends real
# money, and a default is how you end up launching six because a previous run
# did. `preflight` and `provision` refuse to run without an explicit count.
#
# Config via environment (defaults shown):
#   FLEET_COUNT=<required>  FLEET_TYPE=g5.xlarge  FLEET_REGION=us-east-1
#   FLEET_PROFILE=default  FLEET_KEY_NAME=demobot  FLEET_SG=<required>
#   FLEET_SUBNET=<optional>  FLEET_AMI=<optional; resolved from SSM if unset>
#   FLEET_VOLUME_GB=100  FLEET_SSH_KEY=~/.ssh/demobot_ec2  FLEET_USER=ubuntu
#   FLEET_HOSTNAME=medadvice.yeackbot.com   (box N serves medadviceN.yeackbot.com)
#   FLEET_PARALLEL=4                        (concurrent bootstraps during deploy)
#   FLEET_NIM=0        1 = every box runs a LOCAL NVIDIA NIM and serves provider=nvidia
#                      (bootstrap --with-nim; needs NGC_API_KEY in this Mac's .env)
#   FLEET_NIM_MODEL=   optional NIM image id (default nvidia/nvidia-nemotron-nano-9b-v2)
#   FLEET_NEMOCLAW=0   1 = also run the NVIDIA NemoClaw sandbox runtime
#                      (bootstrap --with-nemoclaw; needs NVIDIA_INFERENCE_API_KEY in .env)
#   A NIM box wants FLEET_VOLUME_GB=150: image + weights (~30 GB) on top of Ollama's models.
#
# COST: g5.xlarge is ~$1.006/hr each in us-east-1 — about $742/month per box if
# left running. Use `stop` (or deploy/ec2/schedule.sh) aggressively; stopped
# instances still bill for EBS, roughly $8/month per 100 GB volume.
set -euo pipefail

COUNT="${FLEET_COUNT:-}"
TYPE="${FLEET_TYPE:-g5.xlarge}"
REGION="${FLEET_REGION:-us-east-1}"
PROFILE="${FLEET_PROFILE:-default}"
KEY_NAME="${FLEET_KEY_NAME:-demobot}"
SG="${FLEET_SG:-}"
SUBNET="${FLEET_SUBNET:-}"
AMI="${FLEET_AMI:-}"
VOLUME_GB="${FLEET_VOLUME_GB:-100}"
SSH_KEY="${FLEET_SSH_KEY:-$HOME/.ssh/demobot_ec2}"
SSH_USER="${FLEET_USER:-ubuntu}"
HOSTNAME_="${FLEET_HOSTNAME:-medadvice.yeackbot.com}"
PARALLEL="${FLEET_PARALLEL:-4}"
NIM="${FLEET_NIM:-0}"
NIM_MODEL="${FLEET_NIM_MODEL:-}"
NEMOCLAW="${FLEET_NEMOCLAW:-0}"
TAG_KEY="demobot-fleet"

# G and VT On-Demand vCPU quota. g5.xlarge is 4 vCPU each.
QUOTA_CODE="L-DB2E81BA"
VCPU_PER_INSTANCE=4
PRICE_PER_HOUR=1.006

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AWS=(aws --region "$REGION" --profile "$PROFILE")

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }
confirm() {
  printf '\033[1m%s\033[0m [type yes to continue]: ' "$1"
  read -r a; [ "$a" = "yes" ] || die "aborted"
}

# No default count: the caller must state it. See the header for why.
require_count() {
  [ -n "$COUNT" ] || die "FLEET_COUNT is not set.
      This has no default on purpose — provisioning GPU instances costs real money.
      State it explicitly, e.g.:  FLEET_COUNT=1 $0 ${1:-preflight}"
  case "$COUNT" in ''|*[!0-9]*) die "FLEET_COUNT must be a positive integer (got '$COUNT')" ;; esac
  [ "$COUNT" -ge 1 ] || die "FLEET_COUNT must be at least 1"
}

need_aws() {
  command -v aws >/dev/null || die "aws CLI not installed"
  "${AWS[@]}" sts get-caller-identity >/dev/null 2>&1 \
    || die "AWS credentials for profile '$PROFILE' are not valid.
      Configure the personal account first:  aws configure --profile $PROFILE"
}

resolve_ami() {
  [ -n "$AMI" ] && { echo "$AMI"; return; }
  # Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04) — drivers and
  # CUDA preinstalled, so the bootstrap never has to install a driver + reboot.
  "${AWS[@]}" ssm get-parameter \
    --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
    --query 'Parameter.Value' --output text 2>/dev/null
}

instance_ids() {
  "${AWS[@]}" ec2 describe-instances \
    --filters "Name=tag:$TAG_KEY,Values=true" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

# Every replica number claimed by real INFRASTRUCTURE — a live instance or a
# Cloudflare tunnel. Those are the things that actually collide.
#
# access-keys.env is deliberately NOT consulted. A key is just a generated
# string; having one for an unused replica is harmless and idempotent. Counting
# keys as claims caused a real failure on 2026-07-29: running
# `gen-access-keys.sh 8` before `provision` claimed 1-8, so the next 7 boxes were
# numbered 9-15 instead of 2-8. Generate keys AFTER provisioning, or in any
# order — with this definition it no longer matters.
claimed_replicas() {
  {
    "${AWS[@]}" ec2 describe-instances \
      --filters "Name=tag:$TAG_KEY,Values=true" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query "Reservations[].Instances[].Tags[?Key=='Replica'].Value" --output text 2>/dev/null | tr '\t' '\n'
    cloudflared tunnel list 2>/dev/null | grep -oE 'demobot-[0-9]+' | grep -oE '[0-9]+$'
  } | grep -E '^[0-9]+$' | sort -un
}

# Lowest unused replica number, counting up from 1.
next_replica() {
  local claimed n=1
  claimed=$(claimed_replicas)
  while printf '%s\n' "$claimed" | grep -qx "$n"; do n=$((n + 1)); done
  echo "$n"
}

# The one canonical place the public URL string is built.
box_url() { printf 'https://%s%s.%s' "${HOSTNAME_%%.*}" "$1" "${HOSTNAME_#*.}"; }

cmd_next_replica() {
  need_aws
  local claimed; claimed=$(claimed_replicas | tr '\n' ' ')
  echo "  claimed: ${claimed:-none}"
  echo "  next:    $(next_replica)  ->  $(box_url "$(next_replica)")/"
}

# Emits: <replica> <instance-id> <state> <public-ip>
fleet_rows() {
  "${AWS[@]}" ec2 describe-instances \
    --filters "Name=tag:$TAG_KEY,Values=true" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query "Reservations[].Instances[].[Tags[?Key=='Replica']|[0].Value,InstanceId,State.Name,PublicIpAddress]" \
    --output text | sort -n
}

cmd_preflight() {
  require_count preflight
  need_aws
  log "identity"
  "${AWS[@]}" sts get-caller-identity --output table

  log "GPU quota (G and VT On-Demand vCPUs)"
  local need have
  need=$((COUNT * VCPU_PER_INSTANCE))
  have=$("${AWS[@]}" service-quotas get-service-quota \
          --service-code ec2 --quota-code "$QUOTA_CODE" \
          --query 'Quota.Value' --output text 2>/dev/null || echo "unknown")
  echo "  need: $need vCPUs ($COUNT x $TYPE)"
  echo "  have: $have"
  if [ "$have" = "unknown" ]; then
    warn "could not read quota $QUOTA_CODE — check the Service Quotas console"
  elif [ "${have%.*}" -lt "$need" ] 2>/dev/null; then
    warn "QUOTA TOO LOW. Request an increase before provisioning:
      ${AWS[*]} service-quotas request-service-quota-increase \\
        --service-code ec2 --quota-code $QUOTA_CODE --desired-value $need
      New accounts default to 0 and approval can take hours to days."
  else
    echo "  quota is sufficient"
  fi

  log "AMI"
  local ami; ami=$(resolve_ami)
  [ -n "$ami" ] && [ "$ami" != "None" ] && echo "  $ami" || warn "could not resolve the DLAMI; set FLEET_AMI"

  log "other prerequisites"
  if [ -n "$SG" ]; then
    echo "  security group: $SG"
    # Your public IP moves — home vs office vs VPN vs travel. A stale CIDR does
    # not fail loudly: the instance launches fine, then every ssh attempt times
    # out and deploy burns its full retry budget before reporting "ssh never came
    # up". Cost a failed deploy on 2026-07-28. Check it here instead.
    local myip authorized
    myip=$(curl -s --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]')
    if [ -n "$myip" ]; then
      authorized=$("${AWS[@]}" ec2 describe-security-groups --group-ids "$SG" \
        --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`].IpRanges[].CidrIp" \
        --output text 2>/dev/null)
      if printf '%s' "$authorized" | tr '\t' '\n' | grep -qx "$myip/32"; then
        echo "  ssh access: $myip/32 authorized"
      else
        warn "YOUR IP IS NOT AUTHORIZED FOR SSH on $SG.
      current IP:  $myip
      authorized:  ${authorized:-<none>}
      Deploy will fail with 'ssh never came up'. Fix first:
        aws ec2 authorize-security-group-ingress --group-id $SG \\
          --protocol tcp --port 22 --cidr $myip/32 --profile $PROFILE --region $REGION"
      fi
    else
      warn "could not determine your public IP — verify SSH access to $SG by hand"
    fi
  else
    warn "FLEET_SG is not set (required to provision)"
  fi
  "${AWS[@]}" ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1 \
    && echo "  key pair: $KEY_NAME" || warn "key pair '$KEY_NAME' not found in $REGION"
  [ -f "$SSH_KEY" ] && echo "  ssh key: $SSH_KEY" || warn "ssh private key $SSH_KEY not found"
  [ -f "$HOME/.cloudflared/cert.pem" ] && echo "  cloudflare cert.pem present" \
    || warn "no ~/.cloudflared/cert.pem — 'cloudflared tunnel login' is needed to create per-box tunnels"

  if [ "$NIM" = 1 ] || [ "$NEMOCLAW" = 1 ]; then
    log "NVIDIA options"
    if [ "$NIM" = 1 ]; then
      echo "  local NIM: ${NIM_MODEL:-nvidia/nvidia-nemotron-nano-9b-v2} (provider=nvidia on every box)"
      grep -qE '^NGC_API_KEY=nvapi-' "$REPO/.env" && echo "  NGC_API_KEY: present in .env" \
        || warn "NGC_API_KEY=nvapi-… missing from $REPO/.env — deploy will refuse (free key: https://org.ngc.nvidia.com/setup/api-key)"
      [ "$VOLUME_GB" -ge 150 ] 2>/dev/null || warn "FLEET_VOLUME_GB=$VOLUME_GB — a NIM box wants 150 (image + weights ~30 GB on top of Ollama's models)"
    fi
    if [ "$NEMOCLAW" = 1 ]; then
      echo "  NemoClaw runtime: yes"
      grep -qE '^NVIDIA_INFERENCE_API_KEY=nvapi-' "$REPO/.env" && echo "  NVIDIA_INFERENCE_API_KEY: present in .env" \
        || warn "NVIDIA_INFERENCE_API_KEY=nvapi-… missing from $REPO/.env — deploy will refuse"
    fi
  fi
}

cmd_provision() {
  require_count provision
  need_aws
  [ -n "$SG" ] || die "FLEET_SG must be set (security group allowing SSH from your IP)"
  local ami; ami=$(resolve_ami)
  [ -n "$ami" ] && [ "$ami" != "None" ] || die "could not resolve an AMI; set FLEET_AMI"

  # Replica numbers continue from whatever is already claimed rather than
  # restarting at 1 — otherwise adding a box to a live fleet collides with the
  # existing tunnel, access key, and subdomain of the same number.
  local start; start=$(next_replica)
  local last=$((start + COUNT - 1))

  printf '\nAbout to launch \033[1m%s x %s\033[0m in %s (AMI %s, %s GB gp3).\n' \
    "$COUNT" "$TYPE" "$REGION" "$ami" "$VOLUME_GB"
  printf 'Replicas: %s..%s  (continuing from what is already claimed)\n' "$start" "$last"
  printf 'Cost: ~$%.2f/hour  |  ~$%.0f/month always-on  |  ~$%.0f/month at 8h weekdays\n' \
    "$(echo "$COUNT * $PRICE_PER_HOUR" | bc -l)" \
    "$(echo "$COUNT * $PRICE_PER_HOUR * 730" | bc -l)" \
    "$(echo "$COUNT * ($PRICE_PER_HOUR * 176 + 8)" | bc -l)"
  printf 'Public URLs will be: %s\n' \
    "$(seq "$start" "$last" | while read -r n; do printf '%s/ ' "$(box_url "$n")"; done)"
  confirm "Launch $COUNT instances?"

  local n
  for n in $(seq "$start" "$last"); do
    log "launching demobot-$n"
    local args=(ec2 run-instances
      --image-id "$ami" --instance-type "$TYPE" --key-name "$KEY_NAME"
      --security-group-ids "$SG" --count 1
      --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true}"
      --tag-specifications
        "ResourceType=instance,Tags=[{Key=Name,Value=demobot-$n},{Key=$TAG_KEY,Value=true},{Key=Replica,Value=$n},{Key=Project,Value=DemoBot}]"
      --query 'Instances[0].InstanceId' --output text)
    [ -n "$SUBNET" ] && args+=(--subnet-id "$SUBNET")
    "${AWS[@]}" "${args[@]}"
  done

  log "waiting for all instances to reach 'running'"
  # shellcheck disable=SC2046
  "${AWS[@]}" ec2 wait instance-running --instance-ids $(instance_ids)
  cmd_status
  echo
  echo "Next: ./deploy/ec2/fleet.sh deploy"
}

# Deploy one box. Runs in a subshell during parallel deploys, so it must not
# rely on shared state and must return a meaningful exit code.
deploy_one() {
  local replica="$1" ip="$2" logfile="$3"
  {
    echo "=== replica $replica -> $ip ==="
    local ok=false i
    for i in $(seq 1 30); do
      ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
          -o BatchMode=yes "$SSH_USER@$ip" true 2>/dev/null && { ok=true; break; }
      sleep 10
    done
    [ "$ok" = true ] || { echo "ssh never came up on $ip"; exit 1; }

    local -a extra=()
    if [ "$NIM" = 1 ]; then
      extra+=(--with-nim); [ -n "$NIM_MODEL" ] && extra+=("$NIM_MODEL")
    fi
    [ "$NEMOCLAW" = 1 ] && extra+=(--with-nemoclaw)
    "$REPO/deploy/ec2/push-replica.sh" \
        --host "$ip" --user "$SSH_USER" --port 22 --key "$SSH_KEY" \
        --replica "$replica" --own-tunnel --hostname "$HOSTNAME_" \
        --gpu require "${extra[@]}"
  } >"$logfile" 2>&1
}

cmd_deploy() {
  need_aws
  local only="${1:-}"
  local rows; rows=$(fleet_rows)
  [ -n "$rows" ] || die "no fleet instances found — run 'provision' first"

  local logdir; logdir=$(mktemp -d)
  local -a pids=() reps=() logs=()
  local running=0

  while read -r replica id state ip; do
    [ -n "$id" ] || continue
    [ -n "$only" ] && [ "$replica" != "$only" ] && continue
    if [ "$state" != "running" ]; then warn "skipping $id (replica $replica): state=$state"; continue; fi
    if [ "$ip" = "None" ] || [ -z "$ip" ]; then warn "skipping $id: no public IP"; continue; fi

    # Throttle: each bootstrap pulls ~7 GB of models, so unbounded parallelism
    # just saturates the link and slows every box down.
    #
    # Poll `jobs -pr` rather than `wait -n`: this script's shebang is /bin/bash,
    # which on macOS is 3.2, where `wait -n` does not exist. Its error went to
    # /dev/null and `|| true` swallowed the non-zero exit, so `running` was
    # decremented without any child having finished — the loop drained instantly
    # and every replica bootstrapped at once, which is exactly the link
    # saturation this throttle exists to prevent.
    while [ "$(jobs -pr | wc -l | tr -d ' ')" -ge "$PARALLEL" ]; do
      sleep 5
    done
    running=$(jobs -pr | wc -l | tr -d ' ')

    log "deploying replica $replica -> $ip  (log: $logdir/$replica.log)$([ "$NIM" = 1 ] && echo '  [+NIM]')$([ "$NEMOCLAW" = 1 ] && echo '  [+NemoClaw]')"
    deploy_one "$replica" "$ip" "$logdir/$replica.log" &
    pids+=($!); reps+=("$replica"); logs+=("$logdir/$replica.log")
    running=$((running + 1))
  done <<< "$rows"

  [ ${#pids[@]} -gt 0 ] || die "nothing to deploy${only:+ (no replica $only found)}"

  log "waiting for ${#pids[@]} bootstrap(s) — each takes 10-20 min"
  local i failed=0
  for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then
      printf '  \033[32mOK\033[0m    replica %s\n' "${reps[$i]}"
    else
      printf '  \033[31mFAIL\033[0m  replica %s — see %s\n' "${reps[$i]}" "${logs[$i]}"
      tail -15 "${logs[$i]}" | sed 's/^/        /'
      failed=$((failed + 1))
    fi
  done

  echo
  [ "$failed" -eq 0 ] || die "$failed replica(s) failed. Logs in $logdir
      Re-run a single box with:  $0 deploy <replica>"
  cmd_urls
}

cmd_urls() {
  log "public URLs"
  local rows; rows=$(fleet_rows)
  [ -n "$rows" ] || { echo "  (no instances)"; return; }
  local key
  while read -r replica id state ip; do
    [ -n "$replica" ] && [ "$replica" != "None" ] || continue
    key=$(grep -E "^ACCESS_KEY_$replica=" "$REPO/deploy/ec2/access-keys.env" 2>/dev/null | cut -d= -f2-)
    printf '  replica %-3s %-10s %s/\n' "$replica" "$state" "$(box_url "$replica")"
    [ -n "$key" ] && printf '  %-22s access key: %s\n' "" "$key"
    # Markdown form, ready to paste into chat/docs: label is the bare host,
    # link target is /app (the chat UI, which is what people actually want).
    printf '  %-22s markdown:   [%s/](%s/app)\n\n' "" "$(box_url "$replica")" "$(box_url "$replica")"
  done <<< "$rows"
}

cmd_status() {
  need_aws
  log "fleet"
  printf '  %-8s %-21s %-10s %s\n' REPLICA INSTANCE STATE IP
  fleet_rows | while read -r replica id state ip; do
    printf '  %-8s %-21s %-10s %s\n' "${replica:-?}" "$id" "$state" "${ip:-—}"
  done
  local running; running=$("${AWS[@]}" ec2 describe-instances \
    --filters "Name=tag:$TAG_KEY,Values=true" "Name=instance-state-name,Values=running" \
    --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo 0)
  printf '\n  %s running — ~$%.2f/hour\n' "$running" "$(echo "$running * 1.006" | bc -l)"
}

cmd_start() {
  need_aws; local ids; ids=$(instance_ids); [ -n "$ids" ] || die "no fleet instances"
  # shellcheck disable=SC2086
  "${AWS[@]}" ec2 start-instances --instance-ids $ids --output table
  # Public IPs change on stop/start unless Elastic IPs are attached; re-deploy is
  # not needed (the tunnel dials out), but 'status' will show the new addresses.
  warn "public IPs change across stop/start — re-read them with 'status' before ssh"
}

cmd_stop() {
  need_aws; local ids; ids=$(instance_ids); [ -n "$ids" ] || die "no fleet instances"
  # shellcheck disable=SC2086
  "${AWS[@]}" ec2 stop-instances --instance-ids $ids --output table
}

cmd_terminate() {
  need_aws; local ids; ids=$(instance_ids); [ -n "$ids" ] || die "no fleet instances"
  echo "Will PERMANENTLY terminate:"; echo "  $ids"
  confirm "Terminate these instances?"
  confirm "This destroys their EBS volumes and all local demo state. Really?"
  # Capture the replica numbers BEFORE terminating, so the cleanup list is
  # accurate — after termination the tags become unreadable.
  local reps; reps=$(fleet_rows | awk '{print $1}' | grep -E '^[0-9]+$' | sort -n | tr '\n' ' ')

  # shellcheck disable=SC2086
  "${AWS[@]}" ec2 terminate-instances --instance-ids $ids --output table

  log "MANUAL CLEANUP STILL REQUIRED"
  echo "Terminating instances does NOT remove their Cloudflare objects. A leftover"
  echo "CNAME pointing at a dead tunnel serves a Cloudflare 1033 error page rather"
  echo "than failing cleanly, which is confusing to debug months later."
  echo
  echo "  DNS records to delete (Cloudflare dashboard -> DNS):"
  for n in $reps; do echo "     ${HOSTNAME_%%.*}$n.${HOSTNAME_#*.}"; done
  echo
  echo "  Tunnels to delete:"
  for n in $reps; do echo "     cloudflared tunnel delete demobot-$n"; done
  echo
  echo "  Schedules, if installed:"
  echo "     ./deploy/ec2/schedule.sh remove"
}

case "${1:-}" in
  preflight)    cmd_preflight ;;
  provision)    cmd_provision ;;
  deploy)       cmd_deploy "${2:-}" ;;
  urls)         cmd_urls ;;
  next-replica) cmd_next_replica ;;
  status)       cmd_status ;;
  start)        cmd_start ;;
  stop)         cmd_stop ;;
  terminate)    cmd_terminate ;;
  -h|--help|"") sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: $1 (try --help)" ;;
esac
