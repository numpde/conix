"""Operate Conix images from the existing NMR shared host-local wheel pool.

One selected Docker Engine owns all contained target-Python selection and
networked acquisition. The pinned upstream standalone tool streams a curated
tar context directly to BuildKit; host pip never installs packages or wheels.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from shared_wheelhouse import (
    PoolError, admit_pool, has_tool, release_record, selection_manifest,
    source_allowlist, worker_grants,
)

ROOT = Path(__file__).resolve().parent.parent


class SharedWheelhouse:
    def __init__(self, docker, images, path):
        self.docker = docker
        self.images = images
        self.pool = admit_pool(path)
        self.pin = release_record()
        # A rootless engine maps container uid 0 to this unprivileged host
        # operator. For rootful CI, always use the caller's nonzero host UID.
        observed = docker.run(
            "info", "--format", "{{json .SecurityOptions}}",
            timeout=10, capture=True,
        )
        try:
            security = json.loads(observed)
        except ValueError:
            raise PoolError("Cannot establish the selected Docker Engine's user-namespace policy.") from None
        if type(security) is not list or any(type(item) is not str for item in security):
            raise PoolError("Selected Docker Engine returned invalid security options.")
        if os.geteuid() == 0 or os.getegid() == 0:
            raise PoolError("Shared pool access requires a non-root host account and primary group.")
        self.user = "0:0" if "name=rootless" in security else f"{os.geteuid()}:{os.getegid()}"
        self.docker.require_image(self.images["python_base"])

    @property
    def release_path(self):
        return self.pool / self.pin["name"]

    def input_mounts(self):
        return (
            "--mount", f"type=bind,src={ROOT / 'requirements/runtime.lock'},dst=/input/runtime.lock,readonly",
            "--mount", f"type=bind,src={ROOT / 'requirements/build.lock'},dst=/input/build.lock,readonly",
            "--mount", f"type=bind,src={ROOT / 'requirements/wheelhouse-tool-release.json'},dst=/input/release.json,readonly",
        )

    def run(self, *argv, network=False, write=False, timeout=120, capture=False, extra_mounts=()):
        # All worker commands use the same selected endpoint and private Docker
        # client configuration. No --privileged, rootful UID0, Docker socket or
        # network when a warm read is sufficient.
        return self.docker.run(
            "run", *worker_grants(self.pool, user=self.user, network=network, write=write),
            *self.input_mounts(), *extra_mounts, "--entrypoint", "python",
            self.images["python_base"], "-I", *argv,
            timeout=timeout, capture=capture,
        )

    def check(self):
        if not has_tool(self.pool, self.pin):
            raise PoolError(
                "Pinned NMR wheelhouse-tool is missing from WHEELHOUSE; run explicit deps/wheelhouse/acquire."
            )
        raw = self.run(
            "/input/check.py",
            "--pool", "/wheelhouse", "--release", "/input/release.json",
            "--runtime", "/input/runtime.lock", "--build", "/input/build.lock",
            extra_mounts=(
                "--mount", f"type=bind,src={ROOT / 'tools/wheelhouse_worker.py'},dst=/input/check.py,readonly",
            ), capture=True,
        )
        return selection_manifest(raw.encode("utf-8"))

    def acquire(self):
        # Warm caches must not launch any network-enabled worker, even for an
        # index lookup. The released tool itself is reused by SHA-256.
        if not has_tool(self.pool, self.pin):
            self.run(
                "/input/bootstrap.py", "/wheelhouse", "/input/release.json",
                extra_mounts=(
                    "--mount", f"type=bind,src={ROOT / 'tools/acquire_wheelhouse_tool.py'},dst=/input/bootstrap.py,readonly",
                ), network=True, write=True, timeout=390,
            )
            if not has_tool(self.pool, self.pin):
                raise PoolError("Contained wheelhouse-tool bootstrap returned without the pinned release.")
        try:
            return self.check()
        except Exception:
            # Only explicit acquisition may consult metadata. The upstream
            # source-lock projection remains temporary; the original signed
            # requirements lock authorizes each wheel hash.
            pass
        with tempfile.TemporaryDirectory(prefix="conix-wheelhouse-projection-") as tmp:
            for name in ("runtime", "build"):
                projected = self.run(
                    str(self.release_path).replace(str(self.pool), "/wheelhouse", 1),
                    "locate", "--lock", f"/input/{name}.lock", "--pool", "/wheelhouse",
                    network=True, write=False, timeout=240, capture=True,
                )
                path = Path(tmp) / "pylock.toml"
                path.write_text(projected, encoding="utf-8")
                path.chmod(0o600)
                try:
                    self.run(
                        "/wheelhouse/" + self.pin["name"],
                        "acquire", "--lock", "/input/pylock.toml",
                        "--source-lock", f"/input/{name}.lock",
                        "--pool", "/wheelhouse",
                        network=True, write=True, timeout=390,
                        extra_mounts=(
                            "--mount", f"type=bind,src={path},dst=/input/pylock.toml,readonly",
                        ),
                    )
                finally:
                    path.unlink(missing_ok=True)
        return self.check()

    def build(self, target):
        if target not in ("runtime", "tests", "fixture"):
            raise PoolError("The shared wheelhouse supports only the three declared installed-image targets.")
        selected = self.check()
        if target == "fixture":
            self.docker.require_image(self.images["foundry"])
        # Capture source exactly once: the released streamer checks every
        # source hash and wheel digest before writing it to Docker's stdin.
        sources = source_allowlist(ROOT)
        names = [{"name": item["destination"], "sha256": item["sha256"], "exec": item["executable"]}
                 for item in sources]
        source_identity = hashlib.sha256(
            json.dumps(names, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        wheel_identity = hashlib.sha256(
            json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with tempfile.TemporaryDirectory(prefix="conix-stream-") as tmp:
            directory = Path(tmp)
            allowlist = directory / "sources.json"
            manifest = directory / "wheels.json"
            iid = directory / "image-id"
            diagnostics = directory / "stream.err"
            allowlist.write_text(json.dumps(sources), encoding="utf-8")
            manifest.write_text(json.dumps(selected), encoding="utf-8")
            stream_argv = [
                sys.executable, "-I", str(self.release_path), "stream",
                "--pool", str(self.pool),
                "--source-allowlist", str(allowlist),
                "--manifest", str(manifest),
            ]
            build_argv = [
                *self.docker.base, "build", "--builder", "default",
                "--pull=false", "--platform", "linux/amd64",
                "--network", "none", "--target", target, "--file", "Dockerfile",
                "--build-arg", "PYTHON_BASE_IMAGE=" + self.images["python_base"],
                "--label", "io.conix.source-inputs=" + source_identity,
                "--label", "io.conix.wheel-selection=" + wheel_identity,
                "--iidfile", str(iid),
            ]
            if target == "fixture":
                build_argv.extend(("--build-arg", "FOUNDRY_IMAGE=" + self.images["foundry"]))
            build_argv.append("-")
            with diagnostics.open("wb") as stream_errors:
                producer = subprocess.Popen(
                    stream_argv, cwd=ROOT, stdout=subprocess.PIPE,
                    stderr=stream_errors, start_new_session=True,
                )
                try:
                    build = subprocess.Popen(
                        build_argv, cwd=ROOT, stdin=producer.stdout,
                        stdout=sys.stderr, stderr=sys.stderr, env=self.docker.env,
                        start_new_session=True,
                    )
                except BaseException:
                    _retire(producer)
                    raise
                producer.stdout.close()
                try:
                    status = build.wait(timeout=600)
                    selected_status = producer.wait(timeout=60)
                except BaseException:
                    _retire(build)
                    _retire(producer)
                    raise PoolError(
                        "BuildKit stream or builder did not confirm retirement; "
                        "reconcile selected Engine before reusing intermediate artifacts."
                    ) from None
            if status or selected_status:
                raise PoolError(
                    f"Offline build rejected: BuildKit exit={status}, shared stream exit={selected_status}; "
                    "no image tag was changed."
                )
            raw = iid.read_text().strip() if iid.is_file() else ""
            if len(raw) != 71 or not raw.startswith("sha256:") or any(
                ch not in "0123456789abcdef" for ch in raw[7:]
            ):
                raise PoolError("Offline BuildKit returned no admitted immutable image ID.")
            actual = self.docker.require_image(raw)
            if actual != raw:
                raise PoolError("Selected image identity changed after BuildKit completion.")
        # Never overwrite a usable tag before both streamer and builder
        # confirmed their outcomes. Shared wheel bytes stay in the pool.
        self.docker.run("image", "tag", raw, f"conix-tier0-{target}:local", timeout=20)
        return raw


def _retire(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
