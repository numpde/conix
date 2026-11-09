"""Operate Conix images from the existing NMR shared host-local wheel pool.

One selected Docker Engine owns all contained target-Python selection and
networked acquisition. The pinned upstream standalone tool streams a curated
tar context directly to BuildKit; host pip never installs packages or wheels.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.wheelhouse.policy import (
    PoolError, admit_pool, has_tool, release_record, selection_manifest,
    source_allowlist, worker_grants, project_issuer_lock,
)

ROOT = Path(__file__).resolve().parents[2]


class SharedWheelhouse:
    """Reuse one admitted host pool through the already selected Docker Engine."""

    def __init__(self, docker, images, path):
        self.docker = docker
        self.images = images
        self.pool = admit_pool(path)
        self.pin = release_record()
        # Under a rootless daemon, container UID 0 maps to this unprivileged
        # host user; rootful CI instead uses its numeric UID and GID.
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
        """Provide only issuer locks and the reviewed release pin to workers."""
        return (
            "--mount", f"type=bind,src={ROOT / 'requirements/runtime.lock'},dst=/input/runtime.lock,readonly",
            "--mount", f"type=bind,src={ROOT / 'requirements/build.lock'},dst=/input/build.lock,readonly",
            "--mount", f"type=bind,src={ROOT / 'requirements/wheelhouse-tool-release.json'},dst=/input/release.json,readonly",
        )

    def run(self, *argv, network=False, write=False, timeout=120, capture=False, extra_mounts=()):
        """Invoke contained target Python, never a host-side package installer."""
        # All worker commands use the same selected endpoint and private Docker
        # client configuration. No --privileged, rootful UID0, Docker socket or
        # network when a warm read is sufficient.
        return self.docker.run(
            "run", *worker_grants(self.pool, user=self.user, network=network, write=write),
            *self.input_mounts(), *extra_mounts, "--entrypoint", "python",
            self.images["python_base"], "-I", *argv,
            timeout=timeout, capture=capture, public_diagnostic=network,
        )

    def check(self):
        """Verify a warm pool using networkless, read-only target Python."""
        if not has_tool(self.pool, self.pin):
            raise PoolError(
                "Pinned NMR wheelhouse-tool is missing from WHEELHOUSE; run explicit wheelhouse/acquire."
            )

        selection_json = self.run(
            "/input/check.py",
            "--pool", "/wheelhouse", "--release", "/input/release.json",
            "--runtime", "/input/runtime.lock", "--build", "/input/build.lock",
            extra_mounts=(
                "--mount", f"type=bind,src={ROOT / 'tools/wheelhouse/selector.py'},dst=/input/check.py,readonly",
            ), capture=True,
        )
        return selection_manifest(selection_json.encode("utf-8"))

    def acquire(self):
        """Use the network only for an explicitly missing pinned artifact."""
        # A warm check never needs network access, not even for an index lookup.
        if not has_tool(self.pool, self.pin):
            self.run(
                "/input/bootstrap.py", "/wheelhouse", "/input/release.json",
                extra_mounts=(
                    "--mount", f"type=bind,src={ROOT / 'tools/wheelhouse/acquire_tool.py'},dst=/input/bootstrap.py,readonly",
                ), network=True, write=True, timeout=390,
            )
            if not has_tool(self.pool, self.pin):
                raise PoolError("Contained wheelhouse-tool bootstrap returned without the pinned release.")

        try:
            return self.check()
        except Exception as error:
            # Network escalation is permitted ONLY for target-Python's exact
            # missing-wheel status. Docker outages, malformed locks and hash
            # conflicts remain terminal without metadata requests.
            if getattr(error, "exit_code", None) != 10:
                raise

        # The published NMR locator expects multiline hash syntax. Projection
        # is temporary; the original Conix lock remains authoritative.
        with tempfile.TemporaryDirectory(prefix="conix-wheelhouse-projection-") as temporary_dir:
            for name in ("runtime", "build"):
                normalized = Path(temporary_dir) / "normalized.lock"
                normalized.write_text(
                    project_issuer_lock((ROOT / "requirements" / (name + ".lock")).read_text()),
                    encoding="utf-8",
                )
                normalized.chmod(0o600)
                release_mount = (
                    "--mount", f"type=bind,src={normalized},dst=/input/normalized.lock,readonly",
                )
                projected = self.run(
                    "/wheelhouse/" + self.pin["name"], "locate",
                    "--lock", "/input/normalized.lock",
                    "--index-url", "https://pypi.org/simple",
                    "--pool", "/wheelhouse",
                    network=True, write=False, timeout=240, capture=True,
                    extra_mounts=release_mount,
                )
                pylock_path = Path(temporary_dir) / "pylock.toml"
                pylock_path.write_text(projected, encoding="utf-8")
                pylock_path.chmod(0o600)
                try:
                    self.run(
                        "/wheelhouse/" + self.pin["name"],
                        "acquire", "--lock", "/input/pylock.toml",
                        "--source-lock", "/input/normalized.lock",
                        "--pool", "/wheelhouse",
                        network=True, write=True, timeout=390,
                        extra_mounts=release_mount + (
                            "--mount", f"type=bind,src={pylock_path},dst=/input/pylock.toml,readonly",
                        ),
                    )
                finally:
                    pylock_path.unlink(missing_ok=True)
                    normalized.unlink(missing_ok=True)

        return self.check()

    def build(self, target):
        """Stream admitted sources to offline BuildKit, tag only after success."""
        if target not in ("indexer", "test-runner", "evm-test-runner"):
            raise PoolError("The shared wheelhouse supports only the three declared installed-image targets.")

        selected_wheels = self.check()
        if target == "evm-test-runner":
            self.docker.require_image(self.images["foundry"])

        # Source and wheel identities are retained on the image before it
        # receives a local tag. The stream itself is verified byte for byte.
        sources = source_allowlist(ROOT)
        source_inventory = [{"name": item["destination"], "sha256": item["sha256"], "exec": item["executable"]}
                            for item in sources]
        source_identity = hashlib.sha256(
            json.dumps(source_inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        wheel_identity = hashlib.sha256(
            json.dumps(selected_wheels, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with tempfile.TemporaryDirectory(prefix="conix-stream-") as temporary_dir:
            directory = Path(temporary_dir)
            allowlist = directory / "sources.json"
            manifest = directory / "wheels.json"
            image_id_file = directory / "image-id"
            diagnostics = directory / "stream.err"
            allowlist.write_text(json.dumps(sources), encoding="utf-8")
            manifest.write_text(json.dumps(selected_wheels), encoding="utf-8")

            stream_argv = [
                sys.executable, "-I", str(self.release_path), "stream",
                "--pool", str(self.pool),
                "--source-allowlist", str(allowlist),
                "--manifest", str(manifest),
            ]
            build_argv = [
                *self.docker.docker_command_prefix, "build", "--builder", "default",
                "--pull=false", "--platform", "linux/amd64",
                "--network", "none", "--target", target, "--file", "Dockerfile",
                "--build-arg", "PYTHON_BASE_IMAGE=" + self.images["python_base"],
                "--label", "io.conix.source-inputs=" + source_identity,
                "--label", "io.conix.wheel-selection=" + wheel_identity,
                "--label", "io.conix.image-role=" + target,
                "--iidfile", str(image_id_file),
            ]
            if target == "evm-test-runner":
                build_argv.extend(("--build-arg", "FOUNDRY_IMAGE=" + self.images["foundry"]))
            build_argv.append("-")

            with diagnostics.open("wb") as stream_errors:
                streamer = subprocess.Popen(
                    stream_argv, cwd=ROOT, stdout=subprocess.PIPE,
                    stderr=stream_errors, start_new_session=True,
                )
                try:
                    builder = subprocess.Popen(
                        build_argv, cwd=ROOT, stdin=streamer.stdout,
                        stdout=sys.stderr, stderr=sys.stderr, env=self.docker.docker_environment,
                        start_new_session=True,
                    )
                except BaseException:
                    _retire(streamer)
                    raise
                streamer.stdout.close()
                try:
                    builder_status = builder.wait(timeout=600)
                    stream_status = streamer.wait(timeout=60)
                except BaseException as error:
                    _retire(builder)
                    _retire(streamer)
                    if isinstance(error, KeyboardInterrupt):
                        raise

                    raise PoolError(
                        f"Offline BuildKit or wheel-stream wait failed ({type(error).__name__}); "
                        "processes were signalled for retirement. Reconcile selected "
                        "Engine before reusing intermediate artifacts."
                    ) from error
            if builder_status or stream_status:
                # BuildKit output is not an admitted diagnostic. Only bounded
                # wheel-stream errors can appear in the operator's final report.
                reason = _public_stream_diagnostic(diagnostics) if stream_status else None
                extra = f" Shared stream: {reason}." if reason else ""
                raise PoolError(
                    f"Offline build rejected: BuildKit exit={builder_status}, shared stream exit={stream_status}; "
                    f"no image tag was changed.{extra}"
                )
            image_id = image_id_file.read_text().strip() if image_id_file.is_file() else ""
            if len(image_id) != 71 or not image_id.startswith("sha256:") or any(
                character not in "0123456789abcdef" for character in image_id[7:]
            ):
                raise PoolError("Offline BuildKit returned no admitted immutable image ID.")
            verified_id = self.docker.require_image(image_id)
            if verified_id != image_id:
                raise PoolError("Selected image identity changed after BuildKit completion.")

        # Never overwrite a usable tag before both streamer and builder
        # confirmed their outcomes. Shared wheel bytes stay in the pool.
        self.docker.run("image", "tag", image_id, f"conix-{target}:local", timeout=20)
        return image_id


def _public_stream_diagnostic(path):
    """Admit one short, nonsecret diagnostic from the pinned wheel streamer."""
    try:
        tail = path.read_bytes()[-4096:].decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        value = line.strip()
        if (
            1 <= len(value) <= 240
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.,:/()\[\]@+-]*", value)
            and not any(marker in value.lower() for marker in (
                "password", "private", "secret", "token", "://",
            ))
            and re.search(r"(?:0x)?[0-9a-fA-F]{64}", value) is None
        ):
            return value

    return None


def _retire(process):
    """Terminate and reap an owned process group after an interrupted build."""
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
