"""WD-3 — the deploy doc + Dockerfile exist, reference the real extension seams, and bake NO secrets."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_dockerfile_agent_exists_and_sets_entrypoint() -> None:
    text = (ROOT / "Dockerfile.agent").read_text()
    assert "veridex-agent" in text
    assert "python" in text.lower()


def test_deploy_doc_documents_extension_seams() -> None:
    doc = (ROOT / "docs" / "deploy-your-own-agent.md").read_text()
    for seam in ("veridex/policy/envelope.py", "veridex/venues/base.py", "veridex/strategies/"):
        assert seam in doc, f"deploy doc must document the {seam} seam"
    assert "veridex-agent run" in doc
    # COM-001 honesty: the doc must point creds at env/Settings, not the TOML.
    assert "veridex/.env" in doc or "Settings" in doc


def test_no_baked_secrets_in_dockerfile_or_doc() -> None:
    # COM-001: credentials are injected at RUNTIME (env / --env-file), never baked into the image,
    # the committed config, or the doc. No ENV line may ASSIGN a credential value, and the secrets
    # file must never be COPY'd into the image.
    dockerfile = (ROOT / "Dockerfile.agent").read_text()
    doc = (ROOT / "docs" / "deploy-your-own-agent.md").read_text()

    cred = re.compile(r"(JWT|TOKEN|SECRET|PRIVATE_KEY|KEYPAIR|API_KEY)\s*=", re.IGNORECASE)
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("ENV") or stripped.startswith("ARG"):
            assert not cred.search(stripped), f"Dockerfile must not bake a credential: {stripped!r}"
        # Never COPY/ADD the secrets file into the image (a comment mentioning .env is fine).
        if stripped.startswith("COPY") or stripped.startswith("ADD"):
            assert ".env" not in stripped, f"Dockerfile must not COPY the secrets file: {stripped!r}"

    # Runtime injection must be the documented path (not baked-in creds).
    assert "--env-file" in doc


def test_dockerignore_excludes_env_secrets() -> None:
    # COM-001 ENFORCED (not eyeballed): Docker honors .dockerignore, NOT .gitignore — so without a
    # root .dockerignore that excludes env files, `docker build .` (the documented command) would
    # bake veridex/.env (live creds) into an image layer via `COPY veridex ./veridex`. This test
    # FAILS if the .dockerignore is missing or stops excluding env files — i.e. it catches the leak.
    dockerignore_path = ROOT / ".dockerignore"
    assert dockerignore_path.exists(), "root .dockerignore is REQUIRED (Docker ignores .gitignore — COM-001)"
    patterns = [line.strip() for line in dockerignore_path.read_text().splitlines() if line.strip()]
    # An env-exclusion pattern must be present (any of the standard forms).
    env_excludes = {"**/.env", "*.env", "veridex/.env", ".env"}
    assert env_excludes & set(patterns), f".dockerignore must exclude env secrets; has {patterns}"
