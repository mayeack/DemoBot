#!/bin/bash
# Start/stop the DemoBot GPU fleet on a schedule with EventBridge Scheduler.
#
#   ./deploy/ec2/schedule.sh install       # create start, stop, and safety-stop schedules
#   ./deploy/ec2/schedule.sh show          # list schedules and next run times
#   ./deploy/ec2/schedule.sh remove        # delete them
#
# Config via environment (defaults shown):
#   SCHED_START_CRON='cron(30 7 ? * MON-FRI *)'   07:30 weekdays
#   SCHED_STOP_CRON='cron(0 18 ? * MON-FRI *)'    18:00 weekdays
#   SCHED_SAFETY_CRON='cron(0 23 * * ? *)'        23:00 every day — backstop
#   SCHED_TZ=America/New_York
#   FLEET_REGION=us-east-1  FLEET_PROFILE=default
#
# EventBridge Scheduler calls ec2:StartInstances / ec2:StopInstances directly via
# universal targets — no Lambda to write, deploy, or keep patched.
#
# WHY A SAFETY STOP: six g5.xlarge left running is ~$145/day. The nightly stop is
# a backstop for the case where the evening stop silently fails or someone starts
# the fleet by hand and forgets. It is the cheapest insurance in this setup.
#
# NOTE: stopped instances still bill for EBS (~$8/month per 100 GB volume), so
# the floor is roughly $48/month for six boxes, not zero.
set -euo pipefail

REGION="${FLEET_REGION:-us-east-1}"
PROFILE="${FLEET_PROFILE:-default}"
TZ_="${SCHED_TZ:-America/New_York}"
START_CRON="${SCHED_START_CRON:-cron(30 7 ? * MON-FRI *)}"
STOP_CRON="${SCHED_STOP_CRON:-cron(0 18 ? * MON-FRI *)}"
SAFETY_CRON="${SCHED_SAFETY_CRON:-cron(0 23 * * ? *)}"
GROUP="demobot-fleet"
ROLE_NAME="DemoBotSchedulerRole"
TAG_KEY="demobot-fleet"

AWS=(aws --region "$REGION" --profile "$PROFILE")
log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

ACCOUNT=$("${AWS[@]}" sts get-caller-identity --query Account --output text 2>/dev/null) \
  || die "AWS credentials for profile '$PROFILE' are not valid"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"

ensure_role() {
  if "${AWS[@]}" iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "  role $ROLE_NAME exists"; return
  fi
  log "creating IAM role $ROLE_NAME"
  "${AWS[@]}" iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  # Scoped to instances tagged as fleet members — the schedule cannot touch
  # anything else in the account.
  "${AWS[@]}" iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name DemoBotFleetPower \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{
        \"Effect\":\"Allow\",
        \"Action\":[\"ec2:StartInstances\",\"ec2:StopInstances\"],
        \"Resource\":\"*\",
        \"Condition\":{\"StringEquals\":{\"aws:ResourceTag/${TAG_KEY}\":\"true\"}}
      },{
        \"Effect\":\"Allow\",\"Action\":[\"ec2:DescribeInstances\"],\"Resource\":\"*\"
      }]}" >/dev/null
  echo "  waiting for role propagation"; sleep 12
}

# EventBridge Scheduler universal targets need explicit instance IDs, so the
# schedules are rebuilt from whatever is tagged right now. Re-run 'install'
# after provisioning or terminating instances.
fleet_ids_json() {
  local ids
  ids=$("${AWS[@]}" ec2 describe-instances \
        --filters "Name=tag:$TAG_KEY,Values=true" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text)
  [ -n "$ids" ] || die "no instances tagged $TAG_KEY=true — provision the fleet first"
  printf '%s' "$ids" | tr '\t' '\n' | python3 -c "
import json,sys
print(json.dumps({'InstanceIds':[l.strip() for l in sys.stdin if l.strip()]}))"
}

put_schedule() {
  local name="$1" cron="$2" action="$3" payload="$4"
  "${AWS[@]}" scheduler create-schedule --name "$name" --group-name "$GROUP" \
    --schedule-expression "$cron" --schedule-expression-timezone "$TZ_" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "{
      \"Arn\":\"arn:aws:scheduler:::aws-sdk:ec2:${action}\",
      \"RoleArn\":\"${ROLE_ARN}\",
      \"Input\":$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    }" >/dev/null 2>&1 \
  || "${AWS[@]}" scheduler update-schedule --name "$name" --group-name "$GROUP" \
    --schedule-expression "$cron" --schedule-expression-timezone "$TZ_" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target "{
      \"Arn\":\"arn:aws:scheduler:::aws-sdk:ec2:${action}\",
      \"RoleArn\":\"${ROLE_ARN}\",
      \"Input\":$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    }" >/dev/null
  echo "  $name  $cron  ($TZ_)"
}

cmd_install() {
  ensure_role
  "${AWS[@]}" scheduler create-schedule-group --name "$GROUP" >/dev/null 2>&1 || true
  local payload; payload=$(fleet_ids_json)
  echo "  targets: $payload"
  log "schedules"
  put_schedule demobot-start       "$START_CRON"  startInstances "$payload"
  put_schedule demobot-stop        "$STOP_CRON"   stopInstances  "$payload"
  put_schedule demobot-safety-stop "$SAFETY_CRON" stopInstances  "$payload"
  echo
  warn "re-run 'install' after provisioning or terminating instances — the
      instance IDs are baked into each schedule's payload."
}

cmd_show() {
  log "schedules in group $GROUP"
  "${AWS[@]}" scheduler list-schedules --group-name "$GROUP" \
    --query 'Schedules[].[Name,State]' --output table 2>/dev/null \
    || warn "no schedule group '$GROUP' yet — run 'install'"
  local n
  for n in demobot-start demobot-stop demobot-safety-stop; do
    "${AWS[@]}" scheduler get-schedule --name "$n" --group-name "$GROUP" \
      --query "{name:Name,cron:ScheduleExpression,tz:ScheduleExpressionTimezone,target:Target.Input}" \
      --output json 2>/dev/null || true
  done
}

cmd_remove() {
  log "deleting schedules"
  local n
  for n in demobot-start demobot-stop demobot-safety-stop; do
    "${AWS[@]}" scheduler delete-schedule --name "$n" --group-name "$GROUP" 2>/dev/null \
      && echo "  deleted $n" || echo "  $n not present"
  done
  "${AWS[@]}" scheduler delete-schedule-group --name "$GROUP" >/dev/null 2>&1 || true
  warn "IAM role $ROLE_NAME left in place; remove manually if you are done:
      aws iam delete-role-policy --role-name $ROLE_NAME --policy-name DemoBotFleetPower
      aws iam delete-role --role-name $ROLE_NAME"
}

case "${1:-}" in
  install) cmd_install ;;
  show)    cmd_show ;;
  remove)  cmd_remove ;;
  -h|--help|"") sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: $1 (try --help)" ;;
esac
