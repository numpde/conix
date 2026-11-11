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
    """Use one admitted host pool through the explicitly selected Docker Engine."""

    def __init__(self, docker, images, path):
        self.docker = docker
        self.images = images
        self.pool = admit_pool(path)
        self.pin = release_record()

        # Rootless container UID 0 maps to this unprivileged host user; rootful
        # CI uses the host account's nonzero numeric UID and GID instead.
        security_options_json = docker.run(
            "info", "--format", "{{json .SecurityOptions}}",
            timeout=10,
            capture=True,
        )

        try:
            engine_security_options = json.loads(security_options_json)
        except ValueError:
            raise PoolError("Cannot establish the selected Docker Engine's user-namespace policy.") from None

        if type(engine_security_options) is not list or any(type(option) is not str for option in engine_security_options):
            raise PoolError("Selected Docker Engine returned invalid security options.")

        if os.geteuid() == 0 or os.getegid() == 0:
            raise PoolError("Shared pool access requires a non-root host account and primary group.")

        self.user = "0:0" if "name=rootless" in engine_security_options else f"{os.geteuid()}:{os.getegid()}"
        self.docker.require_image(self.images["python_base"])

    @property
    def release_path(self):
        return self.pool / self.pin["name"]

    def input_mounts(self):
        """Mount only the issuer locks and reviewed tool pin into target Python."""
        return (
            "--mount", f"type=bind,src={ROOT / 'requirements/runtime.lock'},dst=/input/runtime.lock,readonly",
            "--mount", f"type=bind,src={ROOT / 'requirements/build.lock'},dst=/input/build.lock,readonly",
            "--mount", f"type=bind,src={ROOT / 'requirements/wheelhouse-tool-release.json'},dst=/input/release.json,readonly",
        )

    def run(self, *argv, network=False, write=False, timeout=120, capture=False, extra_mounts=()):
        """Run contained target Python, not host pip, with explicit network grants."""
        return self.docker.run(
            "run", *worker_grants(self.pool, user=self.user, network=network, write=write),
            *self.input_mounts(), *extra_mounts, "--entrypoint", "python",
            self.images["python_base"], "-I", *argv,
            timeout=timeout,
            capture=capture,
            public_diagnostic=network,
        )

    def check(self):
        """Verify exact locked wheel selection without network or pool writes."""
        if not has_tool(self.pool, self.pin):
            raise PoolError("Pinned NMR wheelhouse-tool is missing from WHEELHOUSE; run explicit wheelhouse/acquire.")

        selection_json = self.run(
            "/input/check.py",
            "--pool", "/wheelhouse", "--release", "/input/release.json",
            "--runtime", "/input/runtime.lock", "--build", "/input/build.lock",
            extra_mounts=(
                "--mount", f"type=bind,src={ROOT / 'tools/wheelhouse/selector.py'},dst=/input/check.py,readonly",
            ),
            capture=True,
        )

        return selection_manifest(selection_json.encode("utf-8"))

    def acquire(self):
        """Grant network access only for the pinned release or missing wheels."""
        if not has_tool(self.pool, self.pin):
            self.run(
                "/input/bootstrap.py", "/wheelhouse", "/input/release.json",
                extra_mounts=(
                    "--mount", f"type=bind,src={ROOT / 'tools/wheelhouse/acquire_tool.py'},dst=/input/bootstrap.py,readonly",
                ),
                network=True,
                write=True,
                timeout=390,
            )

            if not has_tool(self.pool, self.pin):
                raise PoolError("Contained wheelhouse-tool bootstrap returned without the pinned release.")

        try:
            return self.check()
        except Exception as selection_error:
            # Only target Python's exact missing-wheel status permits network
            # acquisition; broken hashes, locks and Docker still fail closed.
            if getattr(selection_error, "exit_code", None) != 10:
                raise

        # The upstream metadata locator needs multiline hash syntax. These
        # projections are temporary; the original issuer locks stay canonical.
        with tempfile.TemporaryDirectory(prefix="conix-wheelhouse-projection-") as temporary_dir:
            for lock_name in ("runtime", "build"):
                projected_source_lock = Path(temporary_dir) / "normalized.lock"
                projected_source_lock.write_text(
                    project_issuer_lock((ROOT / "requirements" / (lock_name + ".lock")).read_text()),
                    encoding="utf-8",
                )
                projected_source_lock.chmod(0o600)

                source_lock_mount = (
                    "--mount", f"type=bind,src={projected_source_lock},dst=/input/normalized.lock,readonly",
                )
                resolved_lock_json = self.run(
                    "/wheelhouse/" + self.pin["name"], "locate",
                    "--lock", "/input/normalized.lock",
                    "--index-url", "https://pypi.org/simple",
                    "--pool", "/wheelhouse",
                    network=True,
                    write=False,
                    timeout=240,
                    capture=True,
                    extra_mounts=source_lock_mount,
                )

                pylock_path = Path(temporary_dir) / "pylock.toml"
                pylock_path.write_text(resolved_lock_json, encoding="utf-8")
                pylock_path.chmod(0o600)

                try:
                    self.run(
                        "/wheelhouse/" + self.pin["name"],
                        "acquire", "--lock", "/input/pylock.toml",
                        "--source-lock", "/input/normalized.lock",
                        "--pool", "/wheelhouse",
                        network=True,
                        write=True,
                        timeout=390,
                        extra_mounts=source_lock_mount + (
                            "--mount", f"type=bind,src={pylock_path},dst=/input/pylock.toml,readonly",
                        ),
                    )
                finally:
                    pylock_path.unlink(missing_ok=True)
                    projected_source_lock.unlink(missing_ok=True)

        return self.check()

    def build(self, target):
        """Stream verified inputs into offline BuildKit; tag only after success."""
        image_role = target

        if image_role not in ("indexer", "test-runner", "evm-test-runner"):
            raise PoolError("The shared wheelhouse supports only the three declared installed-image targets.")

        selected_wheels = self.check()

        if image_role == "evm-test-runner":
            self.docker.require_image(self.images["foundry"])

        source_files = source_allowlist(ROOT)
        source_inventory = [
            {"name": source["destination"], "sha256": source["sha256"], "exec": source["executable"]}
            for source in source_files
        ]
        source_identity = hashlib.sha256(
            json.dumps(source_inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        wheel_identity = hashlib.sha256(
            json.dumps(selected_wheels, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with tempfile.TemporaryDirectory(prefix="conix-stream-") as temporary_dir:
            build_directory = Path(temporary_dir)
            allowlist_path = build_directory / "sources.json"
            wheel_manifest_path = build_directory / "wheels.json"
            image_id_path = build_directory / "image-id"
            stream_error_path = build_directory / "stream.err"

            allowlist_path.write_text(json.dumps(source_files), encoding="utf-8")
            wheel_manifest_path.write_text(json.dumps(selected_wheels), encoding="utf-8")

            streamer_argv = [
                sys.executable, "-I", str(self.release_path), "stream",
                "--pool", str(self.pool),
                "--source-allowlist", str(allowlist_path),
                "--manifest", str(wheel_manifest_path),
            ]
            build_argv = [
                *self.docker.docker_command_prefix, "build", "--builder", "default",
                "--pull=false", "--platform", "linux/amd64",
                "--network", "none", "--target", image_role, "--file", "Dockerfile",
                "--build-arg", "PYTHON_BASE_IMAGE=" + self.images["python_base"],
                "--label", "io.conix.source-inputs=" + source_identity,
                "--label", "io.conix.wheel-selection=" + wheel_identity,
                "--label", "io.conix.image-role=" + image_role,
                "--iidfile", str(image_id_path),
            ]

            if image_role == "evm-test-runner":
                build_argv.extend(("--build-arg", "FOUNDRY_IMAGE=" + self.images["foundry"]))

            build_argv.append("-")

            with stream_error_path.open("wb") as stream_error_file:
                streamer_process = subprocess.Popen(
                    streamer_argv,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=stream_error_file,
                    start_new_session=True,
                )

                try:
                    builder_process = subprocess.Popen(
                        build_argv,
                        cwd=ROOT,
                        stdin=streamer_process.stdout,
                        stdout=sys.stderr,
                        stderr=sys.stderr,
                        env=self.docker.docker_environment,
                        start_new_session=True,
                    )
                except BaseException as error:
                    if not _retire(streamer_process):
                        error.add_note(
                            "Wheel streamer retirement is unconfirmed after builder startup failure."
                        )
                    raise

                streamer_process.stdout.close()

                try:
                    builder_exit_code = builder_process.wait(timeout=600)
                    streamer_exit_code = streamer_process.wait(timeout=60)
                except BaseException as error:
                    builder_retired = _retire(builder_process)
                    streamer_retired = _retire(streamer_process)
                    uncertain_retirement = not (builder_retired and streamer_retired)

                    if uncertain_retirement:
                        error.add_note(
                            "BuildKit client or wheel streamer retirement remains unconfirmed."
                        )
                    if isinstance(error, KeyboardInterrupt):
                        raise

                    retirement = (
                        " Child retirement is unconfirmed; inspect the selected Engine."
                        if uncertain_retirement else ""
                    )
                    raise PoolError(
                        f"Offline BuildKit or wheel-stream wait failed ({type(error).__name__})."
                        f"{retirement}"
                    ) from error

            if builder_exit_code or streamer_exit_code:
                # BuildKit output is not admitted into public diagnostics.
                # Only the verified local streamer's bounded errors can appear.
                admitted_stream_failure = _public_stream_diagnostic(stream_error_path) if streamer_exit_code else None
                stream_failure_note = f" Shared stream: {admitted_stream_failure}." if admitted_stream_failure else ""

                raise PoolError(
                    f"Offline build rejected: BuildKit exit={builder_exit_code}, shared stream exit={streamer_exit_code}; "
                    f"no image tag was changed.{stream_failure_note}"
                )

            image_id = image_id_path.read_text().strip() if image_id_path.is_file() else ""

            if len(image_id) != 71 or not image_id.startswith("sha256:") or any(
                character not in "0123456789abcdef" for character in image_id[7:]
            ):
                raise PoolError("Offline BuildKit returned no admitted immutable image ID.")

            verified_image_id = self.docker.require_image(image_id)

            if verified_image_id != image_id:
                raise PoolError("Selected image identity changed after BuildKit completion.")

        # Do not overwrite the usable local tag until both processes exited
        # successfully and the immutable image identity was confirmed.
        self.docker.run("image", "tag", image_id, f"conix-{image_role}:local", timeout=20)

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
    """Bound TERM/KILL/reap and report whether the child actually exited.

    A resistant child cannot replace the first failure or prevent another
    owned process from receiving cleanup. Reaping the local client does not
    establish the final state of Docker daemon-side build work.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass

    try:
        process.wait(timeout=8)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass

        try:
            process.wait(timeout=5)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        return False
