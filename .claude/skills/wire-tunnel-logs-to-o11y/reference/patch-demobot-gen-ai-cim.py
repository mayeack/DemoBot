#!/usr/bin/env python3
"""Add a second logs fan-out from DemoBot into the TA-gen_ai_cim index on a
Splunk ES demo box, alongside the existing O11y Cloud workshop path.

Needs its own pipeline, not just another exporter: resource/workshop_logs
upserts com.splunk.index=<workshop index>, and the splunk_hec exporter honours
that resource attribute over its own `index:` setting. A gen_ai_log-scoped HEC
token would reject those records.
"""
import re
import shutil
import sys
import time

HOME = "/home/ubuntu/DemoBot"
CFG = f"{HOME}/otel-collector-config.yaml"
RUN = f"{HOME}/run-collector.sh"
ENV = f"{HOME}/.env"
STAMP = time.strftime("%Y%m%d%H%M%S")

NEW_VARS = ["GENAI_HEC_URL", "GENAI_HEC_TOKEN", "GENAI_HEC_INDEX"]

PROCESSOR = """  # Second log destination: the TA-gen_ai_cim index on the Splunk ES demo box.
  # Separate from resource/workshop_logs because com.splunk.index must differ --
  # the splunk_hec exporter honours this resource attribute over its own index:.
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

"""

EXPORTER = """  # Direct HEC into the ES demo box's gen_ai_log index (TA-gen_ai_cim).
  # Cert is publicly valid, so no tls.insecure_skip_verify needed.
  splunk_hec/gen_ai_cim:
    token: "${env:GENAI_HEC_TOKEN}"
    endpoint: "${env:GENAI_HEC_URL}"
    index: "${env:GENAI_HEC_INDEX}"
    source: "${env:WORKSHOP_ENVIRONMENT}"
    sourcetype: "demobot:governance"
    retry_on_failure:
      enabled: true

"""

PIPELINE = """    # DemoBot logs -> ES demo box HEC -> gen_ai_log (TA-gen_ai_cim).
    # Shares the filelog receiver instance with the workshop pipeline.
    logs/gen_ai_cim:
      receivers: [filelog/demobot, otlp]
      processors: [resource/gen_ai_cim, batch]
      exporters: [splunk_hec/gen_ai_cim]
"""


def patch_config():
    src = open(CFG).read()
    if "splunk_hec/gen_ai_cim" in src:
        print("config: already patched, skipping")
        return
    shutil.copy2(CFG, f"{CFG}.bak.{STAMP}")

    # Processor goes at the end of the processors: block (just before exporters:).
    src, n = re.subn(r"(?m)^exporters:$", PROCESSOR + "exporters:", src, count=1)
    if n != 1:
        sys.exit("config: could not locate 'exporters:'")

    # Exporter goes at the end of the exporters: block (just before service:).
    src, n = re.subn(r"(?m)^service:$", EXPORTER + "service:", src, count=1)
    if n != 1:
        sys.exit("config: could not locate 'service:'")

    # New pipeline goes after the existing logs: pipeline's exporters line.
    src, n = re.subn(
        r"(?m)^(      exporters: \[splunk_hec/workshop\]\n)",
        r"\1" + PIPELINE,
        src,
        count=1,
    )
    if n != 1:
        sys.exit("config: could not locate the logs pipeline exporters line")

    open(CFG, "w").write(src)
    print(f"config: patched (backup {CFG}.bak.{STAMP})")


def patch_runner():
    src = open(RUN).read()
    if "GENAI_HEC_URL" in src:
        print("runner: already patched, skipping")
        return
    shutil.copy2(RUN, f"{RUN}.bak.{STAMP}")

    # Extend the WORKSHOP_* export loop.
    src, n = re.subn(
        r"(?m)^(for v in WORKSHOP_ENVIRONMENT .*?WORKSHOP_HEC_INDEX)( ; |; )?(do)$",
        lambda m: m.group(1) + " " + " ".join(NEW_VARS) + "; do",
        src,
        count=1,
    )
    if n != 1:
        src, n = re.subn(
            r"(?m)^(for v in WORKSHOP_ENVIRONMENT[^\n]*WORKSHOP_HEC_INDEX)([^\n]*)$",
            lambda m: m.group(1) + " " + " ".join(NEW_VARS) + m.group(2),
            src,
            count=1,
        )
    if n != 1:
        sys.exit("runner: could not locate the WORKSHOP_* export loop")

    # Extend the container fallback's -e list so both launch paths match.
    src, n = re.subn(
        r"(?m)^(  -e WORKSHOP_ENVIRONMENT[^\n]*WORKSHOP_HEC_INDEX)( \\)$",
        lambda m: m.group(1) + " " + " ".join("-e " + v for v in NEW_VARS) + m.group(2),
        src,
        count=1,
    )
    if n != 1:
        print("runner: WARNING - container -e line not found; native path still fine")

    open(RUN, "w").write(src)
    print(f"runner: patched (backup {RUN}.bak.{STAMP})")


def patch_env(url, token, index):
    src = open(ENV).read()
    shutil.copy2(ENV, f"{ENV}.bak.{STAMP}")
    if not src.endswith("\n"):
        src += "\n"
    wanted = {"GENAI_HEC_URL": url, "GENAI_HEC_TOKEN": token, "GENAI_HEC_INDEX": index}
    for k, v in wanted.items():
        if re.search(rf"(?m)^{k}=", src):
            src = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", src)
        else:
            src += f"{k}={v}\n"
    open(ENV, "w").write(src)
    print(f"env: set {', '.join(wanted)} (backup {ENV}.bak.{STAMP})")


if __name__ == "__main__":
    patch_env(sys.argv[1], sys.argv[2], sys.argv[3])
    patch_runner()
    patch_config()
