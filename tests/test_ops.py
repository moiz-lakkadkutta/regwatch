"""Ops surface: Dockerfile, .dockerignore, ops/entrypoint.sh, the Serverless value files and the
daily-crawl workflow. Everything but `test_docker_build_and_healthz` runs offline; that one is
`@pytest.mark.live` and skips unless a Docker daemon answers `docker info`.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from importlib import resources
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO / "ops" / "entrypoint.sh"
DOCKERFILE = REPO / "ops" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
ENDPOINT_YAML = REPO / "ops" / "serverless-endpoint.yaml"
JOB_YAML = REPO / "ops" / "serverless-job.yaml"
DAILY_WORKFLOW = REPO / ".github" / "workflows" / "daily-crawl.yml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
ENV_EXAMPLE = REPO / ".env.example"

BASH = shutil.which("bash") or "bash"


def _run_entrypoint(
    mode: str, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "REGWATCH_DB_URL": f"sqlite:///{tmp_path}/t.db"}
    return subprocess.run(
        [BASH, "ops/entrypoint.sh", mode],
        env={**base, **env},
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --- Dockerfile / .dockerignore -------------------------------------------------------------


def test_dockerfile_basics() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.startswith("FROM python:3.12-slim")
    assert "EXPOSE 8000" in text
    assert "HEALTHCHECK" in text
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in text
    assert 'CMD ["serve"]' in text
    assert "/healthz" in text
    copies = [ln for ln in text.splitlines() if ln.startswith("COPY")]
    assert not any(".env" in ln for ln in copies), "the image must never contain .env"
    assert "COPY examples ./examples" in text, "the seed run is the only data baked in"


def test_dockerignore_excludes_secrets_and_dbs() -> None:
    lines = {ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    for required in (".env", "*.db", ".git", ".venv", "tests/"):
        assert required in lines, f"{required} missing from .dockerignore"
    assert not any(ln.rstrip("/") == "ops" for ln in lines), "ops/ must reach the build context"


def test_prompts_ship_as_package_data() -> None:
    names = {p.name for p in resources.files("regwatch.prompts").iterdir()}
    assert {"normalize.md", "classify.md", "agent.md"} <= names


# --- entrypoint.sh ----------------------------------------------------------------------------


def test_entrypoint_is_executable() -> None:
    assert os.access(ENTRYPOINT, os.X_OK), "ops/entrypoint.sh must be committed with mode 100755"
    assert ENTRYPOINT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


@pytest.mark.parametrize(
    ("env", "expected_missing"),
    [
        ({}, ["NEBIUS_API_KEY", "TAVILY_API_KEY"]),
        ({"TAVILY_API_KEY": "tvly-x"}, ["NEBIUS_API_KEY"]),
        ({"NEBIUS_API_KEY": "nb-x"}, ["TAVILY_API_KEY"]),
    ],
    ids=["both-missing", "nebius-missing", "tavily-missing"],
)
def test_entrypoint_crawl_exits_2_without_keys(
    tmp_path: Path, env: dict[str, str], expected_missing: list[str]
) -> None:
    proc = _run_entrypoint("crawl", tmp_path, env)
    assert proc.returncode == 2, proc.stderr
    assert "refusing to run" in proc.stderr and "missing" in proc.stderr
    for key in expected_missing:
        assert key in proc.stderr
    for key in env:
        assert key not in proc.stderr.split("missing", 1)[1].split("(")[0]
    assert "fixtures are never used" in proc.stderr


def test_entrypoint_unknown_mode_exits_2(tmp_path: Path) -> None:
    proc = _run_entrypoint("bogus", tmp_path, {})
    assert proc.returncode == 2
    assert "unknown mode 'bogus'" in proc.stderr
    assert "serve|crawl" in proc.stderr


def _stub_regwatch(tmp_path: Path) -> tuple[Path, Path]:
    """A `regwatch` stub that appends its argv to a log and exits with $STUB_RC (default 0)."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "calls.log"
    stub = stub_dir / "regwatch"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$STUB_LOG"\nexit "${STUB_RC:-0}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub_dir, log


def _run_with_stub(
    tmp_path: Path, extra_env: dict[str, str], *, rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub_dir, log = _stub_regwatch(tmp_path)
    env = {
        # stub dir FIRST; `bash` itself is resolved from the real PATH (BASH above)
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_RC": str(rc),
        "NEBIUS_API_KEY": "nb-test",
        "TAVILY_API_KEY": "tvly-test",
        **extra_env,
    }
    proc = _run_entrypoint("crawl", tmp_path, env)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_entrypoint_crawl_invokes_live_stage_all(tmp_path: Path) -> None:
    proc, calls = _run_with_stub(tmp_path, {})
    assert proc.returncode == 0, proc.stderr
    assert calls[0] == "seed"
    assert len(calls) == 2
    crawl = calls[1].split()
    assert crawl[:7] == ["crawl", "--stage", "all", "--backend", "live", "--llm-backend", "live"]
    assert crawl[crawl.index("--time-range") + 1] == "year"
    assert "--out" in crawl and crawl[crawl.index("--out") + 1] == "/data/documents.jsonl"
    assert "fixtures" not in calls[1]


def test_entrypoint_crawl_honours_time_range(tmp_path: Path) -> None:
    proc, calls = _run_with_stub(tmp_path, {"REGWATCH_TIME_RANGE": "month"})
    assert proc.returncode == 0, proc.stderr
    crawl = calls[1].split()
    assert crawl[crawl.index("--time-range") + 1] == "month"


def test_entrypoint_propagates_regwatch_exit_code(tmp_path: Path) -> None:
    proc, calls = _run_with_stub(tmp_path, {}, rc=3)
    assert proc.returncode == 3
    assert calls == ["seed"], "seed failed, so crawl must not have been attempted"


def test_entrypoint_serve_seeds_then_serves(tmp_path: Path) -> None:
    stub_dir, log = _stub_regwatch(tmp_path)
    env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "PORT": "9123",
    }
    proc = _run_entrypoint("serve", tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == ["seed", "serve --host 0.0.0.0 --port 9123"]


def test_entrypoint_creates_db_directory(tmp_path: Path) -> None:
    stub_dir, log = _stub_regwatch(tmp_path)
    db_dir = tmp_path / "data" / "nested"
    proc = subprocess.run(
        [BASH, "ops/entrypoint.sh", "serve"],
        env={
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "REGWATCH_DB_URL": f"sqlite:///{db_dir}/regwatch.db",
        },
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert db_dir.is_dir()


# --- Serverless value files + workflows ---------------------------------------------------------


def _env_example_keys() -> list[str]:
    keys: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.append(m.group(1))
    return keys


def _yaml_sections(path: Path) -> dict[str, dict[str, str]]:
    """Minimal reader for the block-style value files: top-level key → {child key: value}.

    `key: {}` yields an empty mapping; comments and the `# cli:` line are ignored.
    """
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if not line.startswith(" "):
            key, _, value = line.partition(":")
            current = key.strip()
            sections[current] = {}
            if value.strip() and value.strip() != "{}":
                sections[current] = {"": value.strip()}
            continue
        assert current is not None
        key, _, value = line.strip().partition(":")
        sections[current][key.strip()] = value.strip().strip('"')
    return sections


def test_ops_yaml_mirror_env_example() -> None:
    job = _yaml_sections(JOB_YAML)
    endpoint = _yaml_sections(ENDPOINT_YAML)
    mirrored = [k for k in _env_example_keys() if k.startswith(("REGWATCH_", "TOKENFACTORY_"))]
    assert mirrored, ".env.example must list the REGWATCH_/TOKENFACTORY_ variables"
    for key in mirrored:
        assert key in job["env"], f"{key} from .env.example missing in ops/serverless-job.yaml env"
    for secret in ("NEBIUS_API_KEY", "TAVILY_API_KEY"):
        assert secret in job["env_secret"]
        assert secret not in job["env"]
        assert secret not in endpoint["env"]
    assert endpoint["env_secret"] == {}, "the public Endpoint carries no keys"
    assert job["env"]["REGWATCH_TIME_RANGE"] == "year"
    assert job["args"] == {"": '"crawl"'} or job["args"] == {"": "crawl"}
    assert endpoint["args"] == {"": '"serve"'} or endpoint["args"] == {"": "serve"}
    job_text = JOB_YAML.read_text(encoding="utf-8")
    assert not re.search(r"--env (NEBIUS|TAVILY)_API_KEY", job_text)
    assert "--env-secret NEBIUS_API_KEY=" in job_text
    assert "--env-secret TAVILY_API_KEY=" in job_text
    for path in (JOB_YAML, ENDPOINT_YAML):
        assert "fixtures" not in path.read_text(encoding="utf-8")
        assert "disk_size: 32Gi" in path.read_text(encoding="utf-8")


def test_daily_workflow_guards_secrets() -> None:
    text = DAILY_WORKFLOW.read_text(encoding="utf-8")
    assert "schedule:" in text
    assert re.search(r"cron:\s*['\"]\d+ \d+ \* \* \*['\"]", text)
    assert "workflow_dispatch" in text
    guard_pos = text.find("secrets.NEBIUS_SA_ID")
    assert guard_pos != -1, "the first step must check secrets.NEBIUS_SA_ID"
    assert "::error::" in text
    assert text.find("::error::") < text.find("nebius ai job create")
    assert "exit 1" in text
    for secret in ("NEBIUS_SA_PUBLIC_KEY_ID", "NEBIUS_SA_PRIVATE_KEY", "NEBIUS_PROJECT_ID"):
        assert f"secrets.{secret}" in text
    assert "vars.REGWATCH_IMAGE" in text
    assert "--args crawl" in text
    assert "REGWATCH_TIME_RANGE=month" in text
    assert "fixtures" not in text


def test_ci_workflow_builds_image_and_checks_exit_2() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "docker build --platform linux/amd64 -f ops/Dockerfile -t regwatch:ci ." in text
    assert "docker run --rm -e NEBIUS_API_KEY= -e TAVILY_API_KEY= regwatch:ci crawl" in text
    assert "test $? -eq 2" in text
    assert text.find("uv run pytest") < text.find("docker build")


# --- live: real docker build + serve ------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.live
def test_docker_build_and_healthz() -> None:
    if not _docker_available():
        pytest.skip("docker daemon not available (`docker info` failed)")
    httpx = pytest.importorskip("httpx")
    build = "docker build --platform linux/amd64 -f ops/Dockerfile -t regwatch:ci ."
    subprocess.run(build.split(), cwd=REPO, check=True, timeout=900)
    port = _free_port()
    cid = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:8000", "regwatch:ci", "serve"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "/healthz not ready within 60s"
            time.sleep(1)
        assert httpx.get(f"{base}/", follow_redirects=False, timeout=5).status_code == 303
        feed = httpx.get(f"{base}/feed", follow_redirects=True, timeout=10)
        assert feed.status_code == 200
        assert "2023/2673" in feed.text
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False, timeout=60)
