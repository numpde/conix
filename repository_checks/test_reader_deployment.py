"""A named Reader check may only use an isolated, fully owned one-shot client."""

import copy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from tools.containers import reader_deployment
from tools.containers.client import OperationError


IMAGE = "sha256:" + "a" * 64
VOLUME = "conix-inputs-" + "b" * 24
TOKEN = "1" * 24
PROJECT = "conix-reader-pilot-" + TOKEN[:12]
CONTAINER = "c" * 64


def graph():
    return {
        "services": {
            "reader": {
                "image": IMAGE, "pull_policy": "never",
                "entrypoint": ["python"],
                "command": ["-I", "-m", "conix.reader_cli", "check"],
                "user": "65532:65532", "read_only": True, "init": True,
                "restart": "no", "labels": {"io.conix.reader-check": TOKEN},
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "networks": {"database": None, "rpc": None},
                "cpus": 0.5, "pids_limit": 32, "mem_limit": 268435456,
                "memswap_limit": 268435456,
                "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=16m"],
                "logging": {"driver": "local", "options": {
                    "max-size": "1m", "max-file": "1", "compress": "false",
                }},
                "volumes": [{
                    "type": "volume", "source": "inputs", "target": "/run/conix",
                    "read_only": True, "volume": {"nocopy": True},
                }],
            },
        },
        "networks": {
            "database": {"name": "db-conix", "external": True},
            "rpc": {"name": "rpc-conix", "external": True},
        },
        "volumes": {"inputs": {"name": VOLUME, "external": True}},
    }


def container():
    return {
        "Id": CONTAINER,
        "Config": {
            "Image": IMAGE,
            "Labels": {
                "com.docker.compose.project": PROJECT,
                "com.docker.compose.service": "reader",
                "io.conix.reader-check": TOKEN,
            },
        },
    }


class NamedReaderCheckContracts(unittest.TestCase):
    def test_reader_failure_record_reaches_host_without_private_stderr(self):
        """A real check failure retains its admitted phase, type, location and exit."""
        temporary_receipt = tempfile.mkstemp
        record = (b'CONIX-FAILURE-V1 {"phase":"reader_check","cause":"HistoryUnavailable",'
                  b'"effect":"not_started","location":"reader.py:655"}\n')
        for stderr, admitted in (
            (record, True), (b"Container creating\n" + record, True),
            (record + record, False), (b"password=do-not-report", False),
        ):
            with self.subTest(admitted=admitted):
                docker = Mock()
                docker.docker_environment = {}
                docker.docker_command_prefix = ["docker"]
                def run(*args, **kwargs):
                    if "config" in args:
                        return json.dumps(graph())
                    if "run" in args:
                        raise OperationError("Docker Compose run failed (exit 2).", exit_code=2)
                    return ""
                docker.run.side_effect = run
                with (
                    patch.object(reader_deployment.secrets, "token_hex", return_value=TOKEN),
                    patch.object(reader_deployment, "_admit_volume"),
                    patch.object(reader_deployment, "_admit_network"),
                    tempfile.TemporaryDirectory() as receipts,
                    patch.object(reader_deployment.tempfile, "mkstemp",
                                 side_effect=lambda **kwargs: temporary_receipt(dir=receipts, **kwargs)),
                    patch("subprocess.run", return_value=SimpleNamespace(
                        returncode=2, stdout=b"", stderr=stderr,
                    )),
                    self.assertRaises(OperationError) as caught,
                ):
                    reader_deployment.check(
                        docker, name="pilot", image=IMAGE, volume=VOLUME,
                        database="db-conix", rpc="rpc-conix",
                    )
                message = str(caught.exception)
                if admitted:
                    self.assertIn("HistoryUnavailable", message)
                    self.assertIn("reader.py:655", message)
                    self.assertIn("reader check", message)
                else:
                    self.assertIn("no admitted diagnostic", message)
                self.assertEqual(caught.exception.exit_code, 2)
                self.assertNotIn("do-not-report", message)

    def test_rendered_graph_requires_only_external_bridges_private_volume_and_readonly_process(self):
        reader_deployment.admit_graph(
            graph(), image=IMAGE, volume=VOLUME,
            database="db-conix", rpc="rpc-conix", token=TOKEN,
        )
        for change in (
            lambda g: g["services"]["reader"].update(image=IMAGE + "-wrong"),
            lambda g: g["services"]["reader"].update(command=["-m", "conix", "run"]),
            lambda g: g["services"]["reader"].update(user="0:0"),
            lambda g: g["services"]["reader"].update(read_only=False),
            lambda g: g["services"]["reader"].update(cap_add=["NET_ADMIN"]),
            lambda g: g["services"]["reader"].update(privileged=True),
            lambda g: g["services"]["reader"].update(ports=["1234:1234"]),
            lambda g: g["services"]["reader"].update(environment={"SECRET":"credential"}),
            lambda g: g["services"]["reader"]["volumes"].append({"type":"bind","source":"/host"}),
            lambda g: g["services"]["reader"]["volumes"][0].update(source="writer-volume"),
            lambda g: g["services"]["reader"]["networks"].update({"internet": None}),
            lambda g: g["networks"]["rpc"].update(external=False),
            lambda g: g["networks"]["database"].update(name="wrong-bridge"),
            lambda g: g["volumes"]["inputs"].update(name="wrong-volume"),
            lambda g: g["services"]["reader"]["labels"].update({"io.conix.reader-check":"wrong"}),
        ):
            candidate = copy.deepcopy(graph())
            change(candidate)
            with self.subTest(change=change), self.assertRaises(OperationError):
                reader_deployment.admit_graph(
                    candidate, image=IMAGE, volume=VOLUME,
                    database="db-conix", rpc="rpc-conix", token=TOKEN,
                )

    def test_reader_logging_requires_bounded_uncompressed_local_file(self):
        """Single-file local logging must disable compression before container creation."""
        for change in (
            lambda value: value["options"].update(compress="true"),
            lambda value: value["options"].pop("compress"),
            lambda value: value.update(driver="syslog"),
            lambda value: value["options"].update({"max-size": "0"}),
        ):
            candidate = graph()
            change(candidate["services"]["reader"]["logging"])
            with self.subTest(change=change), self.assertRaises(OperationError):
                reader_deployment.admit_graph(
                    candidate, image=IMAGE, volume=VOLUME,
                    database="db-conix", rpc="rpc-conix", token=TOKEN,
                )

    def test_conflicting_container_identity_refuses_all_retirement(self):
        docker = Mock()
        docker.run.return_value = CONTAINER + "\n"
        record = container()
        record["Config"]["Labels"]["io.conix.reader-check"] = "foreign"
        with patch.object(reader_deployment, "_record", return_value=record):
            with self.assertRaises(OperationError):
                reader_deployment._check_candidates(
                    docker, project=PROJECT, image=IMAGE, token=TOKEN,
                )
        self.assertFalse(any("rm" in call.args for call in docker.run.call_args_list))

    def test_actual_check_admits_output_only_after_no_owned_containers_remain(self):
        docker = Mock()
        docker.docker_environment = {}
        docker.run.side_effect = lambda *args, **kwargs: (
            json.dumps(graph()) if "config" in args
            else json.dumps({
                "outcome": "reader_ready", "source_id": "1" * 64,
                "checkpoint": 7, "generation": "checked-generation",
            }) if "run" in args
            else ""
        )
        with (
            patch.object(reader_deployment.secrets, "token_hex", return_value=TOKEN),
            patch.object(reader_deployment, "_admit_volume") as admit_volume,
            patch.object(reader_deployment, "_admit_network") as admit_network,
            redirect_stdout(StringIO()) as out,
            patch.object(reader_deployment, "_run_reader", return_value=json.dumps({
                "outcome": "reader_ready", "source_id": "1" * 64,
                "checkpoint": 7, "generation": "checked-generation",
            })),
        ):
            actual = reader_deployment.check(
                docker, name="pilot", image=IMAGE, volume=VOLUME,
                database="db-conix", rpc="rpc-conix",
            )
        self.assertEqual(actual["checkpoint"], 7)
        self.assertIn("reader_ready", out.getvalue())
        self.assertEqual(admit_volume.call_args.args, (docker, VOLUME))
        self.assertEqual(admit_network.call_count, 2)
        docker.require_rootless_engine.assert_called_once()
        docker.verify_inputs.assert_called_once_with(IMAGE, VOLUME)
        self.assertFalse(any("rm" in call.args for call in docker.run.call_args_list))

    def test_primary_reader_failure_survives_uncertain_owned_check_cleanup(self):
        docker = Mock()
        docker.docker_environment = {}
        failure = OperationError("read-only consumer refused")
        observed = 0

        def run(*args, **kwargs):
            nonlocal observed
            if "config" in args:
                return json.dumps(graph())
            if args[0] == "ps":
                observed += 1
                return "" if observed == 1 else CONTAINER + "\n"
            if "run" in args:
                raise failure
            if args[:3] == ("container", "rm", "--force"):
                raise OperationError("retirement uncertain")
            raise AssertionError(f"Unexpected Docker action {args[:3]}")

        docker.run.side_effect = run
        with (
            patch.object(reader_deployment.secrets, "token_hex", return_value=TOKEN),
            patch.object(reader_deployment, "_admit_volume"),
            patch.object(reader_deployment, "_admit_network"),
            patch.object(reader_deployment, "_record", return_value=container()),
            redirect_stderr(StringIO()) as err,
            patch.object(reader_deployment, "_run_reader", side_effect=failure),
            self.assertRaises(OperationError) as raised,
        ):
            reader_deployment.check(
                docker, name="pilot", image=IMAGE, volume=VOLUME,
                database="db-conix", rpc="rpc-conix",
            )
        self.assertIs(raised.exception, failure)
        self.assertIn("cleanup is unconfirmed", err.getvalue())
        self.assertNotIn("read-only consumer refused", err.getvalue())

    def test_cleanup_failure_cannot_report_success(self):
        docker = Mock()
        docker.docker_environment = {}
        observed = 0

        def run(*args, **kwargs):
            nonlocal observed
            if "config" in args:
                return json.dumps(graph())
            if args[0] == "ps":
                observed += 1
                return "" if observed == 1 else CONTAINER + "\n"
            if "run" in args:
                return json.dumps({
                    "outcome": "reader_ready", "source_id": "1" * 64,
                    "checkpoint": None, "generation": None,
                })
            if args[:3] == ("container", "rm", "--force"):
                raise OperationError("retirement uncertain")
            raise AssertionError("Unexpected Docker operation")

        docker.run.side_effect = run
        with (
            patch.object(reader_deployment.secrets, "token_hex", return_value=TOKEN),
            patch.object(reader_deployment, "_admit_volume"),
            patch.object(reader_deployment, "_admit_network"),
            patch.object(reader_deployment, "_record", return_value=container()),
            redirect_stdout(StringIO()) as out,
            patch.object(reader_deployment, "_run_reader", return_value=json.dumps({
                "outcome": "reader_ready", "source_id": "1" * 64,
                "checkpoint": None, "generation": None,
            })),
            self.assertRaises(OperationError),
        ):
            reader_deployment.check(
                docker, name="pilot", image=IMAGE, volume=VOLUME,
                database="db-conix", rpc="rpc-conix",
            )
        self.assertNotIn("reader_ready", out.getvalue())


if __name__ == "__main__":
    unittest.main()
