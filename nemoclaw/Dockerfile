# =============================================================================
# NemoClaw sandbox image for the DemoBot agentic-risk demo
# =============================================================================
# NVIDIA NemoClaw runs OpenClaw inside an OpenShell sandbox. Custom OpenClaw
# plugins are baked into a custom sandbox image and the sandbox is created from
# it with `nemoclaw onboard --from nemoclaw/Dockerfile` (the Dockerfile's parent
# directory is the build context). This image = NemoClaw's pinned OpenClaw
# sandbox + DemoBot's governance seat (the demobot-toolguard plugin), so every
# tool call the sandboxed agent makes is inspected by /api/toolguard/inspect
# and every OpenShell denial is reported back (after_tool_call + OCSF tail).
#
# Build context: run-nemoclaw.sh materialises the plugin from git
# (`git archive HEAD:openclaw/plugins/demobot-toolguard`) into
# nemoclaw/plugins/demobot-toolguard/ for the duration of the build and removes
# it afterwards — openclaw/ is sparse-excluded on the Mac (EDR).
#
# Pin: the digest NemoClaw's blueprint.yaml pins (OpenShell 0.0.106, OpenClaw
# 2026.3.11+). Bump deliberately together with NEMOCLAW_INSTALL_REF in
# run-nemoclaw.sh; the plugin's peer range is checked at install time.
# =============================================================================
ARG NEMOCLAW_SANDBOX_IMAGE=ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest
FROM ${NEMOCLAW_SANDBOX_IMAGE}

# Plugin config (guardUrl / accessKey / agentSurface) is written at onboarding
# by run-nemoclaw.sh through build args, so the image itself holds no secret
# when built without them.
ARG DEMOBOT_GUARD_URL=http://127.0.0.1:8001
ARG DEMOBOT_ACCESS_KEY=
ARG DEMOBOT_FAIL_OPEN=false

USER root
COPY plugins/demobot-toolguard/ /opt/demobot-toolguard/
RUN test ! -e /opt/demobot-toolguard/node_modules/openclaw \
 && chown -R sandbox:sandbox /opt/demobot-toolguard || true

# Install + enable the plugin the way NemoClaw documents for custom images
# (docs/deployment/install-openclaw-plugins.mdx): the local install copies the
# staged plugin into OpenClaw's extensions tree, `enable` updates openclaw.json,
# and the managed runtime's config hash is refreshed afterwards.
USER sandbox
RUN HOME=/sandbox openclaw plugins install /opt/demobot-toolguard \
 && HOME=/sandbox openclaw plugins enable demobot-toolguard \
 && HOME=/sandbox node -e ' \
      const fs=require("fs"); const p="/sandbox/.openclaw/openclaw.json"; \
      const c=JSON.parse(fs.readFileSync(p,"utf8")); \
      c.plugins=c.plugins||{}; c.plugins.entries=c.plugins.entries||{}; \
      c.plugins.entries["demobot-toolguard"]={enabled:true,config:{ \
        guardUrl:process.env.DEMOBOT_GUARD_URL, accessKey:process.env.DEMOBOT_ACCESS_KEY, \
        failOpen:process.env.DEMOBOT_FAIL_OPEN==="true", timeoutMs:8000, agentSurface:"nemoclaw"}}; \
      fs.writeFileSync(p, JSON.stringify(c,null,2));' \
 && sha256sum /sandbox/.openclaw/openclaw.json > /sandbox/.openclaw/.config-hash
ENV DEMOBOT_GUARD_URL=${DEMOBOT_GUARD_URL} \
    DEMOBOT_ACCESS_KEY=${DEMOBOT_ACCESS_KEY} \
    DEMOBOT_FAIL_OPEN=${DEMOBOT_FAIL_OPEN}
