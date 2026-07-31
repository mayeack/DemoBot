#!/usr/bin/env python3
"""Wire DemoBot's logs straight into a Splunk ES demo box's gen_ai_log index.

WHY THIS EXISTS (separate from patch-demobot-gen-ai-cim.py):
that script adds a SECOND destination alongside an already-installed O11y
workshop log path -- it assumes the filelog receiver, the file_storage
extension, and the WORKSHOP_* export loop are all present, and fails with
"could not locate the WORKSHOP_* export loop" on a stock box.

When every assigned Splunk instance is an ES demo box (serverName
show-demo-i-*), there is no o11y workshop instance to point splunk_hec/workshop
at, so installing the workshop path first is pointless -- and leaves an exporter
with an empty endpoint that fails config validation. This script installs a
single, self-contained logs pipeline instead.

Idempotent, and it resets the two tracked files to pristine first, because
`git pull --ff-only` in the bootstrap cannot overwrite locally-modified tracked
files -- a re-deployed box silently keeps a previous session's collector config.

Usage:  patch-demobot-es-only.py <hec_url> <hec_token> <index> <environment>
"""
import re
import shutil
import subprocess
import sys
import time

HOME = "/home/ubuntu/DemoBot"
CFG = f"{HOME}/otel-collector-config.yaml"
RUN = f"{HOME}/run-collector.sh"
ENV = f"{HOME}/.env"
STAMP = time.strftime("%Y%m%d%H%M%S")
VARS = ["GENAI_HEC_URL", "GENAI_HEC_TOKEN", "GENAI_HEC_INDEX", "WORKSHOP_ENVIRONMENT"]

EXTENSIONS = """extensions:
  # Checkpoints filelog read offsets so a collector restart resumes where it
  # left off instead of re-shipping every log file from the top.
  file_storage/checkpoints:
    directory: /home/ubuntu/DemoBot/.otel-storage
    create_directory: true

"""

RECEIVER = """  # DemoBot writes newline-delimited JSON governance/audit logs to logs/*.json.
  filelog/demobot:
    include: [/home/ubuntu/DemoBot/logs/*.json]
    start_at: beginning
    include_file_name: true
    include_file_path: false
    storage: file_storage/checkpoints
    operators:
      - type: json_parser
        parse_from: body
        parse_to: body
        on_error: send_quiet
      - type: add
        field: resource["com.splunk.sourcetype"]
        value: EXPR("demobot:" + attributes["log.file.name"])

"""

PROCESSORS = """  # Stamps identity onto every log record. com.splunk.* become HEC metadata and
  # override the exporter's own index:/source: settings.
  resource/gen_ai_cim:
    attributes:
      - key: deployment.environment
        value: "${env:WORKSHOP_ENVIRONMENT}"
        action: upsert
      - key: service.name
        value: demobot
        action: upsert
      - key: host.name
        value: "${env:WORKSHOP_ENVIRONMENT}.yeackbot.com"
        action: upsert
      - key: com.splunk.index
        value: "${env:GENAI_HEC_INDEX}"
        action: upsert
      - key: com.splunk.source
        value: "${env:WORKSHOP_ENVIRONMENT}"
        action: upsert
  # TA-gen_ai_cim only extracts gen_ai.* for sourcetypes it DEFINES. Its
  # [index::gen_ai_log] stanza is inert -- props.conf has no index:: scope -- so
  # ai_governance.json must arrive as medadvice3:json, the stanza whose
  # FIELDALIASes match its flat field names, or every dashboard reads empty.
  transform/gen_ai_cim_sourcetype:
    error_mode: ignore
    log_statements:
      - context: resource
        statements:
          - set(attributes["com.splunk.sourcetype"], "medadvice3:json") where attributes["com.splunk.sourcetype"] == "demobot:ai_governance.json"

"""

EXPORTER = """  # Logs -> the ES demo box's own HEC, feeding the gen_ai_log index that
  # TA-gen_ai_cim defines. TLS is a publicly valid cert, so no insecure skip.
  splunk_hec/gen_ai_cim:
    token: "${env:GENAI_HEC_TOKEN}"
    endpoint: "${env:GENAI_HEC_URL}"
    index: "${env:GENAI_HEC_INDEX}"
    source: "${env:WORKSHOP_ENVIRONMENT}"
    sourcetype: "demobot:json"
    retry_on_failure:
      enabled: true

"""

PIPELINE = """    # Governance/audit logs -> Splunk ES demo box (gen_ai_log).
    logs:
      receivers: [filelog/demobot, otlp]
      processors: [resource/gen_ai_cim, transform/gen_ai_cim_sourcetype, batch]
      exporters: [splunk_hec/gen_ai_cim]
"""


def reset_tracked():
    """The bootstrap's `git pull --ff-only` cannot overwrite modified tracked
    files, so a redeployed box keeps stale config. Start from pristine."""
    for f in ("otel-collector-config.yaml", "run-collector.sh"):
        r = subprocess.run(["git", "-C", HOME, "checkout", "--", f],
                           capture_output=True, text=True)
        print(f"  reset {f}: {'ok' if r.returncode == 0 else r.stderr.strip()[:60]}")


def patch_config():
    src = open(CFG).read()
    if "splunk_hec/gen_ai_cim" in src:
        print("  config: already patched")
        return
    shutil.copy2(CFG, f"{CFG}.bak.{STAMP}")

    if not re.search(r"(?m)^extensions:$", src):
        src, n = re.subn(r"(?m)^receivers:$", EXTENSIONS + "receivers:", src, count=1)
        if n != 1:
            sys.exit("could not locate 'receivers:'")

    src, n = re.subn(r"(?m)^processors:$", RECEIVER + "processors:", src, count=1)
    if n != 1:
        sys.exit("could not locate 'processors:'")

    src, n = re.subn(r"(?m)^exporters:$", PROCESSORS + "exporters:", src, count=1)
    if n != 1:
        sys.exit("could not locate 'exporters:'")

    src, n = re.subn(r"(?m)^service:$", EXPORTER + "service:", src, count=1)
    if n != 1:
        sys.exit("could not locate 'service:'")

    if not re.search(r"(?m)^  extensions:", src):
        src, n = re.subn(r"(?m)^service:$", "service:\n  extensions: [file_storage/checkpoints]", src, count=1)
        if n != 1:
            sys.exit("could not add service.extensions")

    src, n = re.subn(r"(?m)^  telemetry:$", PIPELINE + "  telemetry:", src, count=1)
    if n != 1:
        sys.exit("could not locate 'telemetry:' to insert the logs pipeline")

    open(CFG, "w").write(src)
    print(f"  config: patched (backup .bak.{STAMP})")


def patch_runner():
    src = open(RUN).read()
    if "GENAI_HEC_URL" in src:
        print("  runner: already patched")
        return
    shutil.copy2(RUN, f"{RUN}.bak.{STAMP}")
    loop = ('for v in ' + " ".join(VARS) + '; do\n'
            '  export "$v=$(grep "^$v=" .env 2>/dev/null | cut -d= -f2- || true)"\n'
            'done\n')
    # Insert before the SPLUNK_REALM guard so the vars exist by the time the
    # collector starts, on both the native and container launch paths.
    src, n = re.subn(r'(?m)^if \[ -z "\$\{SPLUNK_REALM:-\}" \]', loop + 'if [ -z "${SPLUNK_REALM:-}" ]', src, count=1)
    if n != 1:
        src, n = re.subn(r'(?m)^echo "Starting OTel Collector', loop + 'echo "Starting OTel Collector', src, count=1)
        if n != 1:
            sys.exit("could not find an anchor in run-collector.sh")
    src = src.replace('-e GALILEO_LOG_STREAM \\',
                      '-e GALILEO_LOG_STREAM \\\n  ' + " ".join(f"-e {v}" for v in VARS) + ' \\')
    open(RUN, "w").write(src)
    print(f"  runner: patched (backup .bak.{STAMP})")


def patch_env(url, token, index, env):
    src = open(ENV).read()
    shutil.copy2(ENV, f"{ENV}.bak.{STAMP}")
    for k, v in (("GENAI_HEC_URL", url), ("GENAI_HEC_TOKEN", token),
                 ("GENAI_HEC_INDEX", index), ("WORKSHOP_ENVIRONMENT", env)):
        if re.search(rf"(?m)^{k}=", src):
            src = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", src)
        else:
            if not src.endswith("\n"):
                src += "\n"
            src += f"{k}={v}\n"
    open(ENV, "w").write(src)
    print(f"  env: set {', '.join(VARS)}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    reset_tracked()
    patch_env(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    patch_config()
    patch_runner()
