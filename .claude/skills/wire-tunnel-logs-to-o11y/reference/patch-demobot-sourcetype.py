#!/usr/bin/env python3
"""Map ai_governance.json to the sourcetype TA-gen_ai_cim actually extracts.

The filelog `add` operator stamps com.splunk.sourcetype = "demobot:<file>" at
the RECEIVER level, so it is shared by every pipeline reading that receiver.
Rewriting it there would change the O11y workshop path too. Instead, remap it
inside the gen_ai_cim pipeline only, with a transform processor.

Why medadvice3:json: TA-gen_ai_cim's [index::gen_ai_log] stanza is inert --
props.conf cannot be scoped by index -- so its gen_ai.* FIELDALIASes never
fire. [medadvice3:json] carries the identical alias set and matches DemoBot's
flat ai_governance.json shape exactly.
"""
import re
import shutil
import sys
import time

CFG = "/home/ubuntu/DemoBot/otel-collector-config.yaml"
STAMP = time.strftime("%Y%m%d%H%M%S")

PROCESSOR = """  # TA-gen_ai_cim only extracts gen_ai.* for sourcetypes it defines. Its
  # [index::gen_ai_log] stanza is a no-op (props.conf has no index:: scope), so
  # ai_governance.json must arrive as medadvice3:json -- the stanza whose
  # FIELDALIASes match its flat field names -- or every dashboard reads empty.
  # Scoped to this pipeline so the workshop path keeps demobot:<file> types.
  transform/gen_ai_cim_sourcetype:
    error_mode: ignore
    log_statements:
      - context: resource
        statements:
          - set(attributes["com.splunk.sourcetype"], "medadvice3:json") where attributes["com.splunk.sourcetype"] == "demobot:ai_governance.json"

"""


def main():
    src = open(CFG).read()
    if "transform/gen_ai_cim_sourcetype" in src:
        print("already patched, skipping")
        return
    shutil.copy2(CFG, f"{CFG}.bak.{STAMP}")

    src, n = re.subn(r"(?m)^exporters:$", PROCESSOR + "exporters:", src, count=1)
    if n != 1:
        sys.exit("could not locate 'exporters:'")

    src, n = re.subn(
        r"(?m)^(      processors: \[resource/gen_ai_cim), (batch\]);?$",
        r"\1, transform/gen_ai_cim_sourcetype, \2",
        src,
        count=1,
    )
    if n != 1:
        sys.exit("could not locate the logs/gen_ai_cim processors line")

    open(CFG, "w").write(src)
    print(f"patched (backup {CFG}.bak.{STAMP})")


if __name__ == "__main__":
    main()
