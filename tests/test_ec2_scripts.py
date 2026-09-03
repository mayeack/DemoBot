"""Static regression checks for the EC2 fleet scripts (deploy/ec2/*.sh).

These scripts only ever run for real on a GPU box that costs money per hour, so
the cheap invariants are asserted here, offline, with nothing but bash and the
files in git:

* every script parses (``bash -n``) and advertises the NVIDIA flags in --help;
* the bootstrap decides ``provider=nvidia`` (the ``SETS`` prepend) BEFORE it
  rewrites ``.env`` — the 4.2.0 bootstrap prepended after the rewrite, so a
  ``--with-nim`` box silently booted on the Mac's ``AI_PROVIDER=ollama``;
* a NIM box never lets Ollama keep VRAM (``OLLAMA_KEEP_ALIVE=0``) and unloads
  the warm-up model before the NIM unit starts;
* the NIM comes up before the app and is part of the final health gate;
* ``push-replica.sh --with-nim`` / ``--with-nemoclaw`` refuse to ship without
  the matching ``nvapi-`` key in ``.env`` and never print its value;
* ``fleet.sh`` forwards ``FLEET_NIM`` / ``FLEET_NEMOCLAW`` to push-replica.

Run:  venv/bin/python tests/test_ec2_scripts.py   (any python3 works; stdlib only)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EC2 = ROOT / "deploy" / "ec2"
BOOTSTRAP = EC2 / "ec2-bootstrap.sh"
PUSH = EC2 / "push-replica.sh"
FLEET = EC2 / "fleet.sh"

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        _failures.append(msg)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --------------------------------------------------------------------------- 1
def test_syntax_and_help() -> None:
    print("== syntax + --help ==")
    for script in (BOOTSTRAP, PUSH, FLEET):
        r = run(["bash", "-n", str(script)])
        check(r.returncode == 0, f"bash -n {script.relative_to(ROOT)}: {r.stderr.strip()[:120]}")
    for script in (BOOTSTRAP, PUSH):
        helptext = run(["bash", str(script), "--help"]).stdout
        for flag in ("--with-nim", "--with-nemoclaw"):
            check(flag in helptext, f"{script.name} --help documents {flag}")
    fleet_head = FLEET.read_text().split("set -euo pipefail", 1)[0]
    for var in ("FLEET_NIM", "FLEET_NIM_MODEL", "FLEET_NEMOCLAW"):
        check(var in fleet_head, f"fleet.sh header documents {var}")


# --------------------------------------------------------------------------- 2
def test_bootstrap_ordering() -> None:
    print("== bootstrap: provider decision precedes the .env rewrite ==")
    text = BOOTSTRAP.read_text()
    prepend = text.find('SETS=("AI_PROVIDER=nvidia"')
    rewrite = text.find('python3 - "$REPO/.env"')
    check(prepend > 0 and rewrite > 0, "both the SETS prepend and the .env rewrite exist")
    check(prepend < rewrite, "AI_PROVIDER=nvidia is prepended to SETS BEFORE the .env rewrite (4.2.0 regression)")
    check(text.count('SETS=("AI_PROVIDER=nvidia"') == 1, "the prepend happens exactly once")
    # The GPU requirement for a NIM is enforced at the same early point.
    gpu_die = text.find('die "--with-nim needs an NVIDIA GPU')
    check(0 < gpu_die < rewrite, "--with-nim dies without a GPU before any install work")


def test_bootstrap_vram_handling() -> None:
    print("== bootstrap: Ollama holds no VRAM on a NIM box ==")
    text = BOOTSTRAP.read_text()
    m = re.search(r'if \[ "\$WITH_NIM" = true \]; then\n(?:.*\n){0,8}?\s*LOADED=1; KEEP="0"', text)
    check(m is not None, "WITH_NIM sets OLLAMA_MAX_LOADED_MODELS=1 and OLLAMA_KEEP_ALIVE=0")
    check('"keep_alive":"5m"' in text.replace("\\", ""), "warm-up request pins its own keep_alive (daemon default may be 0)")
    flat = text.replace("\\", "")          # the JSON is inside a double-quoted bash string
    unload = flat.find('"keep_alive":0')
    nim_start = flat.find("sudo systemctl enable --now demobot-nim")
    check(unload > 0 and nim_start > 0 and unload < nim_start, "the warm-up model is unloaded before demobot-nim starts")


def test_bootstrap_start_order_and_gate() -> None:
    print("== bootstrap: NIM before the app, and in the health gate ==")
    text = BOOTSTRAP.read_text()
    nim_start = text.find("sudo systemctl enable --now demobot-nim")
    app_start = text.find("sudo systemctl enable --now demobot-collector demobot-app demobot-tunnel")
    check(0 < nim_start < app_start, "demobot-nim is started (and waited for) before the app stack")
    check('check "nim      /v1/health/ready"' in text, "final verify gates on /v1/health/ready when WITH_NIM")
    check('check "nim      /v1/models"' in text, "final verify gates on /v1/models when WITH_NIM")
    # Credentials are checked in the payload preflight (step 0), not 20 min later.
    step0 = text.find("# --- 0. payload preflight")
    step1 = text.find("# --- 1. base packages")
    seg = text[step0:step1]
    check("^NGC_API_KEY=" in seg, "step 0 requires NGC_API_KEY for --with-nim")
    check("^NVIDIA_INFERENCE_API_KEY=" in seg, "step 0 requires NVIDIA_INFERENCE_API_KEY for --with-nemoclaw")
    check("deb.nodesource.com/setup_22.x" in text and "binutils" in text,
          "--with-nemoclaw installs Node 22 + binutils (NemoClaw prerequisites)")
    # The sandbox's guard URL must be the host's IP, never loopback (inside the
    # sandbox 127.0.0.1 is the container — verified live 2026-09-02).
    check("run-nemoclaw.sh --host=127.0.0.1" not in text,
          "NemoClaw never runs with --host=127.0.0.1 (inside the sandbox that is the sandbox)")
    rn = (ROOT / "run-nemoclaw.sh").read_text()
    check("hostname -I" in rn and "HOST=127.0.0.1" not in rn and 'LOCAL_GUARD="http://127.0.0.1:8001"' in rn,
          "run-nemoclaw.sh defaults the sandbox's guard host to this host's private IP and checks the app locally")
    check("openshell sandbox list" in rn and "Ready" in rn, "run-nemoclaw.sh waits for the sandbox phase Ready before writing the plugin config")
    check("exists and is Ready" in rn, "run-nemoclaw.sh does not re-provision a sandbox that is already Ready")
    vn = (ROOT / "tests/observability/verify_nemoclaw_observability.sh").read_text()
    bare = [l for l in (rn + vn).splitlines() if "openshell sandbox exec" in l and "timeout " not in l and not l.lstrip().startswith("#")]
    check(not bare, f"every openshell sandbox exec is bounded by timeout ({len(bare)} unbounded)")
    pr = "\n".join(l for l in (ROOT / "nemoclaw/policies/demobot-guard.yaml").read_text().splitlines()
                   if not l.lstrip().startswith("#"))     # YAML keys only, not the commentary
    check("preset:" in pr and "network_policies:" in pr and "allowed_ips" not in pr and "__DEMOBOT_HOST__" in pr,
          "demobot-guard preset uses NemoClaw's preset schema (preset header, network_policies) without allowed_ips")
    # NemoClaw onboarding needs the app up and the inference key in its env.
    onb = text[text.find('onboarding the NemoClaw sandbox'):text.find('sudo systemctl enable demobot-nemoclaw')]
    check("localhost:8001/health" in onb and "/etc/demobot-nemoclaw.env" in onb and "set -a" in onb,
          "NemoClaw onboarding waits for /health and exports /etc/demobot-nemoclaw.env first")
    # The NIM must not start at its 131072-token default on a 24 GB card (CUDA
    # OOM in vLLM's profiling pass, restart loop, never ready — 2026-09-02).
    check("NIM_MAX_MODEL_LEN=" in text and "-e NIM_MAX_MODEL_LEN" in text,
          "the NIM unit caps the context via NIM_MAX_MODEL_LEN (default 8192)")
    check("NIM_MAX_NUM_SEQS=" in text and "-e NIM_MAX_NUM_SEQS" in text,
          "the NIM unit caps concurrency via NIM_MAX_NUM_SEQS (Mamba state cache; default 8)")
    check("NIM_KVCACHE_PERCENT=" in text and "-e NIM_KVCACHE_PERCENT" in text,
          "the NIM unit sets NIM_KVCACHE_PERCENT (default 0.95) so KV blocks fit next to the weights")
    # apt must wait for the dpkg lock (unattended-upgrades killed a deploy at
    # step 9b on 2026-09-02): every apt-get goes through apt_get(), which does.
    bare = [l for l in text.splitlines() if "apt-get " in l and "apt_get()" not in l
            and not l.lstrip().startswith("#") and "sudo apt-get install -y nvidia-driver" not in l]
    check(not bare, f"no bare apt-get outside the apt_get() helper ({len(bare)} found)")
    check("DPkg::Lock::Timeout" in text and "wait_dpkg_lock" in text, "apt_get() waits for the dpkg lock")
    # The key never reaches argv: only stdin login, the env file, and -e NGC_API_KEY.
    argv_leak = re.search(r"docker login[^\n]*-p\s", text)
    check(argv_leak is None, "docker login uses --password-stdin, never -p on argv")


# --------------------------------------------------------------------------- 3
def _fake_repo(env_lines: list[str]) -> Path:
    """A throwaway repo root with deploy/ec2/push-replica.sh and a controlled .env."""
    tmp = Path(tempfile.mkdtemp(prefix="ec2test-"))
    (tmp / "deploy" / "ec2").mkdir(parents=True)
    (tmp / "deploy" / "ec2" / "push-replica.sh").write_text(PUSH.read_text())
    os.chmod(tmp / "deploy" / "ec2" / "push-replica.sh", 0o755)
    (tmp / ".env").write_text("\n".join(env_lines) + "\n")
    return tmp


def test_push_replica_refuses_without_keys() -> None:
    print("== push-replica: refuses NVIDIA options without the matching key ==")
    fake_key = "nvapi-TESTKEY0000000000000000"
    cases = [
        (["AI_PROVIDER=ollama"], ["--with-nim"], "NGC_API_KEY"),
        (["AI_PROVIDER=ollama"], ["--with-nemoclaw"], "NVIDIA_INFERENCE_API_KEY"),
        # Too short to be any NGC key shape is refused too.
        (["NGC_API_KEY=not-a-key"], ["--with-nim"], "NGC_API_KEY"),
    ]
    for env_lines, flags, want in cases:
        repo = _fake_repo(env_lines)
        r = run(["bash", "deploy/ec2/push-replica.sh", "--host", "127.0.0.1", "--replica", "99", *flags], cwd=repo)
        check(r.returncode != 0, f"{' '.join(flags)} without {want}: non-zero exit")
        check(want in r.stderr, f"{' '.join(flags)} without {want}: names the missing key")
    # With the key present the preflight passes this gate and fails LATER (no
    # tunnel config here) — and the key value is never echoed.
    repo = _fake_repo([f"NGC_API_KEY={fake_key}", f"NVIDIA_INFERENCE_API_KEY={fake_key}"])
    r = run(["bash", "deploy/ec2/push-replica.sh", "--host", "127.0.0.1", "--replica", "99",
             "--with-nim", "--with-nemoclaw"], cwd=repo, env={**os.environ, "HOME": str(repo)})
    check(r.returncode != 0 and "NGC_API_KEY" not in r.stderr and "NVIDIA_INFERENCE_API_KEY" not in r.stderr,
          "with both keys the NVIDIA gate passes (failure moves on to the tunnel preflight)")
    check(fake_key not in r.stdout + r.stderr, "the key value is never printed")
    # A legacy NGC key (84 alphanumerics, no nvapi- prefix) is accepted too — it
    # works for nvcr.io pulls and the hosted API; docker login is the real check.
    legacy = "L" * 84
    repo = _fake_repo([f"NGC_API_KEY={legacy}"])
    r = run(["bash", "deploy/ec2/push-replica.sh", "--host", "127.0.0.1", "--replica", "99", "--with-nim"],
            cwd=repo, env={**os.environ, "HOME": str(repo)})
    check("NGC_API_KEY" not in r.stderr, "a legacy (non-nvapi) NGC key passes the shape gate")


def test_push_replica_forwards_flags() -> None:
    print("== push-replica: forwards the flags to the bootstrap ==")
    text = PUSH.read_text()
    check("ARGS+=(--with-nim)" in text, "--with-nim is appended to the bootstrap args")
    check('ARGS+=("$NIM_MODEL")' in text, "an explicit NIM model id is forwarded")
    check("ARGS+=(--with-nemoclaw)" in text, "--with-nemoclaw is appended to the bootstrap args")


def test_fleet_claimed_replicas_tolerates_nothing_claimed() -> None:
    """First box / after a teardown: no instances and no demobot-N tunnels.
    `grep` then matches nothing and exits 1; under `set -euo pipefail` that
    used to abort next-replica AND provision silently (seen 2026-09-02)."""
    print("== fleet: claimed_replicas with nothing claimed ==")
    text = FLEET.read_text()
    start = text.find("claimed_replicas() {")
    end = text.find("\n}\n", start) + 3
    fn = text[start:end]
    script = (
        "set -euo pipefail\nTAG_KEY=demobot-fleet\nfake_aws() { :; }\nAWS=(fake_aws)\n"
        "cloudflared() { :; }\n" + fn + "\nout=$(claimed_replicas)\necho \"ok:[$out]\"\n"
    )
    r = run(["bash", "-c", script])
    check(r.returncode == 0 and "ok:[]" in r.stdout,
          f"claimed_replicas exits 0 with empty output when nothing is claimed (rc={r.returncode} {r.stderr.strip()[:80]})")


def test_fleet_forwards_env() -> None:
    print("== fleet: FLEET_NIM / FLEET_NEMOCLAW reach push-replica ==")
    text = FLEET.read_text()
    fn = text[text.find("deploy_one() {"):text.find("cmd_deploy() {")]
    check("extra+=(--with-nim)" in fn and "extra+=(--with-nemoclaw)" in fn,
          "deploy_one builds --with-nim / --with-nemoclaw from FLEET_NIM / FLEET_NEMOCLAW")
    check('--gpu require "${extra[@]}"' in fn, "the extra flags are passed on the push-replica command line")
    pre = text[text.find("cmd_preflight() {"):text.find("cmd_provision() {")]
    check("NGC_API_KEY" in pre and "NVIDIA_INFERENCE_API_KEY" in pre and "FLEET_VOLUME_GB" in pre,
          "preflight checks the keys and the volume size for a NIM box")


if __name__ == "__main__":
    for fn in (test_syntax_and_help, test_bootstrap_ordering, test_bootstrap_vram_handling,
               test_bootstrap_start_order_and_gate, test_push_replica_refuses_without_keys,
               test_push_replica_forwards_flags, test_fleet_claimed_replicas_tolerates_nothing_claimed,
               test_fleet_forwards_env):
        fn()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")
