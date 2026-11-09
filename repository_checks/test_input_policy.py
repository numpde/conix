"""Read-only dependency and Docker-context admission checks.

This checks declarations and hashes without using pip or Docker. Exact wheel
bytes are independently checked by the offline --require-hashes image build.
"""
import json
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent.parent

NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
VERSION = r"[A-Za-z0-9][A-Za-z0-9_.+!-]*"
DIRECT = re.compile(rf"({NAME})(?:\[[A-Za-z0-9_,.-]+\])?==({VERSION})\Z")
PIN = re.compile(rf"({NAME})==({VERSION}) --hash=sha256:([0-9a-f]{{64}})\Z")
IMAGE = re.compile(r"[a-zA-Z0-9./_-]+(?::[a-zA-Z0-9_.-]+)?@sha256:[0-9a-f]{64}\Z")


def _name(value):
    return value.lower().replace("_", "-")


def direct_versions(requirements):
    """Identify exact root requirements without resolving dependencies."""
    found = {}
    for requirement in requirements:
        result = DIRECT.fullmatch(requirement)
        if result is None:
            raise ValueError("A direct dependency is not exactly version pinned.")

        name, version = result.groups()
        name = _name(name)
        if name in found:
            raise ValueError("A direct dependency is declared more than once.")

        found[name] = version
    return found


def lock_versions(content):
    """Require a single version and artifact hash per canonical package name."""
    found = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        result = PIN.fullmatch(line)
        if result is None:
            raise ValueError("A lock entry lacks an exact artifact version/hash.")

        name, version, digest = result.groups()
        name = _name(name)
        if name in found:
            raise ValueError("The lock includes more than one artifact for a project.")

        found[name] = (version, digest)
    if not found:
        raise ValueError("The lock has no admitted wheel artifacts.")

    return found


def verify_requirements(project, runtime, build):
    """Check that recorded wheels cover the issuer's direct requirements."""
    for expected, actual, label in (
        (direct_versions(project["project"]["dependencies"]), lock_versions(runtime), "runtime"),
        (direct_versions(project["build-system"]["requires"]), lock_versions(build), "build"),
    ):
        for (name, version) in expected.items():
            if name not in actual or actual[name][0] != version:
                raise ValueError(f"{label} lock has no matching exact artifact for {name}.")

        if label == "build" and set(actual) != set(expected):
            raise ValueError("Build lock includes packages outside its declared build requirements.")


class OfflineInputChecks(unittest.TestCase):
    """Verify source and image admission without pip, Docker or network."""

    def test_source_and_artifact_pins_agree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        verify_requirements(
            project,
            (ROOT / "requirements/runtime.lock").read_text(),
            (ROOT / "requirements/build.lock").read_text(),
        )
        images = json.loads((ROOT / "containers/images.json").read_text())
        self.assertEqual(set(images), {"postgres", "python_base", "foundry", "dockerfile_frontend"})
        self.assertTrue(all(type(value) is str and IMAGE.fullmatch(value) for value in images.values()))
        self.assertEqual((ROOT / "Dockerfile").read_text().splitlines()[0],
                         "# syntax=" + images["dockerfile_frontend"])

    def test_duplicate_missing_and_malformed_wheel_pins_are_rejected(self):
        pin = "foo==1.0 --hash=sha256:" + "a" * 64
        for data in (pin + "\n" + pin, "foo==1.0",
                     "foo==1.0 --hash=sha256:wrong", "foo>=1.0"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                lock_versions(data)
        project = {"project": {"dependencies": ["foo==1.0"]},
                   "build-system": {"requires": ["setuptools==82.0.1"]}}
        build = "setuptools==82.0.1 --hash=sha256:" + "b" * 64
        with self.assertRaises(ValueError):
            verify_requirements(project, "bar==1.0 --hash=sha256:" + "c" * 64, build)

    def test_build_contexts_have_closed_source_inputs(self):
        # Exact allowlisting is a security boundary: expanding it requires
        # consciously updating this review, not a generic negative glob.
        rules = [
            line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        expected = {
            "!Dockerfile", "!pyproject.toml",
            "!requirements/", "!requirements/runtime.lock", "!requirements/build.lock",
            "!src/", "!src/**", "!tests/",
            "!tests/test_tier0.py", "!tests/test_input_staging.py",
            "!tests/prepare_schema.py", "!tests/fixture_protocol.py",
            "!tests/pay_to_play_integration.py",
        }
        self.assertEqual(rules[0], "**", "The default Docker context must deny every path.")
        self.assertEqual(set(rules[1:]), expected)
        self.assertEqual(len(rules) - 1, len(expected), "Duplicate build-context rules.")

        # No per-repository dependency image or host wheel staging authority:
        # the released wheelhouse-tool streams only selected verified artifacts
        # to an offline BuildKit context, and installer mounts are ephemeral.
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("WHEELHOUSE_IMAGE", dockerfile)
        self.assertNotIn("FROM ${WHEELHOUSE_IMAGE}", dockerfile)
        self.assertEqual(dockerfile.count("source=wheelhouse,target=/wheels,readonly"), 2)
        self.assertIn("--no-index", dockerfile)
        self.assertIn("--require-hashes", dockerfile)
        self.assertNotIn("COPY --from=package /dist/", dockerfile,
                         "The Conix distribution wheel must not remain in a runtime image layer.")
        self.assertIn("--mount=type=bind,from=package,", dockerfile,
                      "The built Conix wheel must enter installation as an ephemeral mount.")
        from_file = (ROOT / "requirements/wheelhouse-tool-release.json").read_text()
        pin = json.loads(from_file)
        self.assertEqual(pin["version"], "0.5.14")
        self.assertEqual(pin["sha256"],
                         "9d8cd59c0aeffd61ce462fa81f2951a24fb8a8749a1332d5f87842b4c095d656")


if __name__ == "__main__":
    unittest.main()
