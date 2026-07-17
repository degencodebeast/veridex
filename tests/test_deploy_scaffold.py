"""D-0 (Deployment lane, Wave 0) — static Coolify/VPS packaging scaffold tests.

RED-first rows from the D-0 packet:
1. ``compose.coolify.yml`` PARSES (``yaml.safe_load``) and NAMES every required
   volume (Postgres data, ``WAL_DIR`` spool, ReplayPack capture root) plus the
   three services (api-runtime, web, postgres) and the read-only curated
   seed-pack mount (``REPLAY_PACK_ROOT``).
2. ``.dockerignore`` excludes ``**/*.pem`` / ``**/*.key`` / wallet material /
   env files for EVERY build context the compose file defines (a built image
   would contain none of them).
3. ``deploy/coolify/runbook.md`` enumerates every service, port, domain, and
   volume; ``deploy/coolify/provisioning-inventory.md`` exists, references the
   pem-relocation record, and records NO secret values.

Static-only discipline: no docker, no network, no running containers — pure
file/YAML assertions.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.coolify.yml"
RUNBOOK = ROOT / "deploy" / "coolify" / "runbook.md"
INVENTORY = ROOT / "deploy" / "coolify" / "provisioning-inventory.md"

REQUIRED_SERVICES = {"api-runtime", "web", "postgres"}
# Named volumes the packet requires: Postgres data, WAL_DIR spool (I-4/AC-13),
# writable ReplayPack capture root (R-0b/R-2).
REQUIRED_VOLUMES = {"postgres-data", "wal-spool", "replay-capture"}
# Ports are Coolify-owned ("Ports Exposes"), documented in the runbook — the
# compose skeleton publishes nothing to real domains in D-0.
REQUIRED_PORTS = {"3000", "8000", "5432"}
# Placeholder domains only — D-0 performs NO real DNS mutation.
REQUIRED_DOMAINS = {"arena.veridex.example", "api.veridex.example"}

# Exact-line exclusions every build context's .dockerignore must carry.
SECRET_EXCLUSION_LINES = {"**/*.pem", "**/*.key"}
# Marker substrings that must appear on at least one exclusion line each.
WALLET_MARKER = "wallet"
ENV_MARKER = ".env"


def _load_compose() -> dict:
    assert COMPOSE.is_file(), f"missing {COMPOSE.relative_to(ROOT)}"
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "compose.coolify.yml must parse to a mapping"
    return data


def _dockerignore_lines(context_dir: Path) -> list[str]:
    path = context_dir / ".dockerignore"
    assert path.is_file(), f"build context {context_dir} has no .dockerignore"
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _build_contexts() -> dict[str, Path]:
    """Map service name -> resolved build-context directory for every service
    that builds an image (image-only services like postgres are skipped)."""
    services = _load_compose()["services"]
    contexts: dict[str, Path] = {}
    for name, svc in services.items():
        build = (svc or {}).get("build")
        if build is None:
            continue
        context = build.get("context", ".") if isinstance(build, dict) else build
        contexts[name] = (ROOT / context).resolve()
    return contexts


# ── RED row 1: compose skeleton ────────────────────────────────────────────


class TestComposeSkeleton:
    def test_compose_parses_as_yaml(self) -> None:
        _load_compose()

    def test_compose_defines_required_services(self) -> None:
        services = set(_load_compose().get("services", {}))
        missing = REQUIRED_SERVICES - services
        assert not missing, f"compose missing services: {sorted(missing)}"

    def test_compose_names_required_volumes(self) -> None:
        volumes = set(_load_compose().get("volumes", {}) or {})
        missing = REQUIRED_VOLUMES - volumes
        assert not missing, f"compose missing named volumes: {sorted(missing)}"

    def test_postgres_uses_named_data_volume(self) -> None:
        svc = _load_compose()["services"]["postgres"]
        sources = {str(v).split(":", 1)[0] for v in svc.get("volumes", [])}
        assert "postgres-data" in sources

    def test_postgres_image_is_pinned(self) -> None:
        image = _load_compose()["services"]["postgres"].get("image", "")
        assert image and ":" in image and not image.endswith(":latest"), (
            f"postgres image must be pinned to an explicit tag, got {image!r}"
        )

    def test_api_runtime_mounts_wal_and_capture_volumes(self) -> None:
        svc = _load_compose()["services"]["api-runtime"]
        sources = {str(v).split(":", 1)[0] for v in svc.get("volumes", [])}
        missing = {"wal-spool", "replay-capture"} - sources
        assert not missing, f"api-runtime missing volume mounts: {sorted(missing)}"

    def test_curated_seed_packs_mounted_read_only(self) -> None:
        svc = _load_compose()["services"]["api-runtime"]
        ro_mounts = [str(v) for v in svc.get("volumes", []) if str(v).endswith(":ro")]
        assert ro_mounts, "api-runtime must mount curated seed packs read-only (:ro)"

    def test_api_runtime_references_i5_owned_dockerfile_placeholder(self) -> None:
        build = _load_compose()["services"]["api-runtime"]["build"]
        assert build["dockerfile"] == "Dockerfile.api", (
            "api-runtime must reference the I-5-owned Dockerfile.api placeholder"
        )

    def test_no_env_values_injected(self) -> None:
        # Env INJECTION (+ :? required-var guards) is D-1; D-0 stays structural.
        for name, svc in _load_compose()["services"].items():
            assert "environment" not in (svc or {}), (
                f"service {name!r} injects environment values — that is D-1 scope"
            )


# ── RED row 2: secret exclusions for every build context ───────────────────


class TestDockerignoreSecretExclusions:
    def test_every_build_context_excludes_secret_material(self) -> None:
        contexts = _build_contexts()
        assert contexts, "compose defines no build contexts"
        for name, context_dir in contexts.items():
            lines = _dockerignore_lines(context_dir)
            missing = SECRET_EXCLUSION_LINES - set(lines)
            assert not missing, (
                f"{name} context {context_dir}/.dockerignore missing exclusions: "
                f"{sorted(missing)}"
            )
            assert any(WALLET_MARKER in line for line in lines), (
                f"{name} context .dockerignore excludes no wallet material"
            )
            assert any(ENV_MARKER in line for line in lines), (
                f"{name} context .dockerignore excludes no env files"
            )

    def test_root_context_covers_existing_dockerfile_agent(self) -> None:
        # Dockerfile.agent already builds from the repo root; the root
        # .dockerignore must carry the same secret exclusions.
        lines = _dockerignore_lines(ROOT)
        assert SECRET_EXCLUSION_LINES <= set(lines)


# ── RED row 3: runbook + provisioning inventory ────────────────────────────


class TestRunbook:
    def test_runbook_exists(self) -> None:
        assert RUNBOOK.is_file(), f"missing {RUNBOOK.relative_to(ROOT)}"

    def test_runbook_enumerates_services_ports_domains_volumes(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        compose = _load_compose()
        for svc in set(compose["services"]) | REQUIRED_SERVICES:
            assert svc in text, f"runbook does not mention service {svc!r}"
        for vol in set(compose.get("volumes", {}) or {}) | REQUIRED_VOLUMES:
            assert vol in text, f"runbook does not mention volume {vol!r}"
        for port in REQUIRED_PORTS:
            assert port in text, f"runbook does not mention port {port}"
        for domain in REQUIRED_DOMAINS:
            assert domain in text, f"runbook does not mention domain {domain!r}"


class TestProvisioningInventory:
    def test_inventory_exists(self) -> None:
        assert INVENTORY.is_file(), f"missing {INVENTORY.relative_to(ROOT)}"

    def test_inventory_references_pem_relocation_record_and_rotation(self) -> None:
        text = INVENTORY.read_text(encoding="utf-8")
        assert "pem-relocation-record.md" in text, (
            "inventory must reference the controller's pem relocation record"
        )
        assert "rotation" in text.lower() and "privy" in text.lower(), (
            "inventory must note key ROTATION as an operator-only Privy follow-up"
        )

    def test_no_secret_values_committed(self) -> None:
        # Names/ownership only — never VALUES. Applies to every D-0 doc artifact.
        for path in (INVENTORY, RUNBOOK, COMPOSE):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA", "BEGIN EC"):
                assert marker not in text, f"{path.name} contains key material"
