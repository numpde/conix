"""Host admission and receipt contracts for the foreground history operation."""

import io
import json
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import Mock, patch

from tools.containers import history
from tools.containers.client import OperationError


class HistoryOperatorContracts(unittest.TestCase):
    def test_external_network_is_refused_before_any_container_creation(self):
        docker = Mock()
        volume = "conix-inputs-" + "a" * 24
        docker.run.side_effect = [
            json.dumps({
                "Name": volume, "Driver": "local", "Scope": "local", "Options": None,
                "Labels": {"io.conix.input-token": "a" * 24},
            }),
            json.dumps({
                "Name": "database", "Id": "b" * 64, "Driver": "bridge", "Scope": "local", "Internal": False,
            }),
        ]
        with self.assertRaises(OperationError):
            history.advance_history(docker, image="sha256:" + "c" * 64,
                                    volume=volume, database="database", rpc="rpc")
        docker.require_rootless_engine.assert_called_once_with()
        self.assertEqual([call.args[:2] for call in docker.run.call_args_list],
                         [("volume", "inspect"), ("network", "inspect")])

    def test_container_with_mismatched_ownership_is_never_removed(self):
        docker = Mock()
        token = "a" * 24
        container_id = "b" * 64
        image = "sha256:" + "c" * 64
        name = "conix-advance-" + token
        docker.run.side_effect = [
            container_id + "\n",
            json.dumps({
                "Id": container_id, "Name": "/other", "Image": image,
                "Config": {"Labels": {"io.conix.history-run": token}},
            }),
        ]
        with self.assertRaises(OperationError):
            history._retire(docker, token, name, image, None)
        self.assertEqual([call.args[:2] for call in docker.run.call_args_list],
                         [("container", "ls"), ("container", "inspect")])

    def test_receipt_requires_exact_fields_and_canonical_types(self):
        result = {"outcome": "advanced", "source_id": "a" * 64, "block_number": 1,
                  "block_hash": "0x" + "b" * 64, "revision": 1}
        self.assertEqual(history._receipt(json.dumps(result)), result)
        for malformed in (
            json.dumps({**result, "revision": True}),
            json.dumps({**result, "block_hash": "0x1"}),
            json.dumps({**result, "unreviewed": 1}),
            json.dumps(result)[:-1] + ', "revision": 2}',
        ):
            with self.subTest(malformed=malformed), self.assertRaises(OperationError):
                history._receipt(malformed)

    def test_attached_worker_refuses_output_larger_than_its_bound(self):
        docker = Mock()
        docker.docker_command_prefix = [sys.executable, "-c", "print('upstream' * 1024)"]
        docker.working_dir = Path(__file__).resolve().parent.parent
        docker.docker_environment = os.environ.copy()
        with self.assertRaises(OperationError) as failure:
            history._attached(docker, "a" * 64)
        self.assertIn("output bound", str(failure.exception))
        self.assertNotIn("upstream", str(failure.exception))

    def test_known_worker_failure_retains_a_safe_causal_explanation(self):
        """A documented RPC deadline failure has useful method and effect."""
        known_failure = (
            b"Index advance failed (RpcUnavailable). "
            b"RPC eth_getLogs exceeded its request deadline.\n"
        )
        admitted = history._failure(known_failure, 3)
        description = str(admitted)

        self.assertIn("RpcUnavailable", description)
        self.assertIn("eth_getLogs", description)
        self.assertIn("request deadline", description)
        self.assertIn("3", description)

    def test_known_worker_failure_does_not_forward_secret_shaped_cause(self):
        """Trusted exception category does not authenticate arbitrary stderr."""
        key = "0x" + "af" * 32
        unsafe_causes = (
            f"RPC eth_getLogs failed because privateKey={key}.",
            f"RPC eth_getLogs failed for http://secret.example/{key}.",
            "RPC eth_getLogs failed\x1b[2J and erased the screen.",
        )
        for cause in unsafe_causes:
            with self.subTest(cause_type=cause.split()[2]):
                stderr = f"Index advance failed (RpcUnavailable). {cause}\n".encode()
                diagnostic = str(history._failure(stderr, 5))
                self.assertIn("RpcUnavailable", diagnostic)
                self.assertIn("5", diagnostic)
                self.assertNotIn(key, diagnostic)
                self.assertNotIn("secret.example", diagnostic)
                self.assertNotIn("\x1b", diagnostic)

    def test_worker_diagnostics_admit_category_without_arbitrary_text(self):
        secret = "private producer output"
        diagnostic = history._failure(f"Index advance failed (RpcUnavailable). {secret}\n".encode(), 1)
        self.assertIn("RpcUnavailable", str(diagnostic))
        self.assertNotIn(secret, str(diagnostic))

    def test_configured_and_live_networks_must_match_selected_identities(self):
        database_id, rpc_id = "d" * 64, "e" * 64
        selected = (("database", database_id), ("rpc", rpc_id))
        self.assertTrue(history._selected_networks(
            {"database": {"NetworkID": ""}, rpc_id: {"NetworkID": ""}}, selected,
        ))
        self.assertTrue(history._selected_networks(
            {"database": {"NetworkID": database_id}, "rpc": {"NetworkID": rpc_id}}, selected,
        ))
        self.assertFalse(history._selected_networks(
            {"database": {"NetworkID": rpc_id}, "rpc": {"NetworkID": database_id}}, selected,
        ))

    def test_persistent_container_uses_the_same_owned_confinement(self):
        """The only changed grants are interactive stdin and a fixed module."""
        docker = Mock()
        docker.run.return_value = "c" * 64
        result = history._create(
            docker, "sha256:" + "a" * 64, "conix-inputs-" + "b" * 24,
            ("d" * 64, "e" * 64), "conix-advance-" + "f" * 24, "f" * 24,
            interactive=True,
        )
        self.assertEqual(result, "c" * 64)
        argv = docker.run.call_args.args
        self.assertIn("--interactive", argv)
        self.assertEqual(argv[-4:], tuple(history.SESSION_COMMAND))
        for required in ("--read-only", "--cap-drop", "--security-opt", "--pids-limit",
                         "--memory", "--memory-swap", "--cpus", "--mount", "--network"):
            self.assertIn(required, argv)
        self.assertNotIn("--privileged", argv)

    def test_command_client_exchanges_two_bounded_frames(self):
        """Pipes carry no secrets and a second command waits for the first reply."""
        script = (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            "  request=json.loads(line)\n"
            "  if request['action']=='advance':\n"
            "    print(json.dumps({'version':1,'outcome':'at_tip','source_id':'a'*64}),flush=True)\n"
            "  elif request['action']=='stop':\n"
            "    print(json.dumps({'version':1,'outcome':'stopped'}),flush=True)\n"
            "    break\n"
        )
        docker = Mock()
        docker.docker_command_prefix = [sys.executable, "-u", "-c", script]
        docker.working_dir = Path(__file__).resolve().parent.parent
        docker.docker_environment = os.environ.copy()

        session = history.CommandSession(docker, "c" * 64, lambda: False)
        try:
            self.assertEqual(session.advance(), {"outcome": "at_tip", "source_id": "a" * 64})
            self.assertEqual(session.advance(), {"outcome": "at_tip", "source_id": "a" * 64})
            session.stop()
            self.assertTrue(session.stopped)
        finally:
            session.retire_client()

    def test_command_client_refuses_unconfirmed_or_unexpected_results(self):
        """An unexpected response cannot be treated as an empty chain tip."""
        script = "import sys;sys.stdin.readline();print('{}',flush=True)\n"
        docker = Mock()
        docker.docker_command_prefix = [sys.executable, "-u", "-c", script]
        docker.working_dir = Path(__file__).resolve().parent.parent
        docker.docker_environment = os.environ.copy()
        session = history.CommandSession(docker, "c" * 64, lambda: False)
        try:
            with self.assertRaises(OperationError):
                session.advance()
        finally:
            session.retire_client()

    def test_persistent_session_retires_owned_container_after_caller_failure(self):
        """The session never converts a caller failure into successful cleanup."""
        docker = Mock(endpoint="unix:///tmp/selected.sock")
        started = Mock()
        retired = Mock()
        with (
            patch.multiple(
                history, _admit_volume=Mock(),
                _admit_network=Mock(side_effect=("d" * 64, "e" * 64)),
                _create=Mock(return_value="c" * 64), _record=Mock(),
                _owned=Mock(return_value=True), _admit_container=Mock(),
                _retire=retired,
            ),
            patch.object(history, "CommandSession") as session_type,
        ):
            session_type.return_value.retire_client = started
            cause = ValueError("consumer failed")
            with self.assertRaises(ValueError) as caught:
                with history.open_history_commands(
                    docker, image="sha256:" + "a" * 64,
                    volume="inputs", database="database", rpc="rpc",
                ):
                    raise cause
        self.assertIs(caught.exception, cause)
        started.assert_called_once()
        session_type.return_value.stop.assert_not_called()
        retired.assert_called_once()

    def test_persistent_session_stops_before_retiring_its_exact_container(self):
        """Normal completion sends a stop frame and checks owned retirement."""
        docker = Mock(endpoint="unix:///tmp/selected.sock")
        retire = Mock()
        with (
            patch.multiple(
                history, _admit_volume=Mock(),
                _admit_network=Mock(side_effect=("d" * 64, "e" * 64)),
                _create=Mock(return_value="c" * 64), _record=Mock(),
                _owned=Mock(return_value=True), _admit_container=Mock(),
                _retire=retire,
            ),
            patch.object(history, "CommandSession") as session_type,
        ):
            with history.open_history_commands(
                docker, image="sha256:" + "a" * 64,
                volume="inputs", database="database", rpc="rpc",
            ) as session:
                session.advance()
            session_type.return_value.stop.assert_called_once()
            session_type.return_value.retire_client.assert_called_once()
            retire.assert_called_once()

    def test_persistent_session_refuses_unreviewed_or_noninteractive_driver(self):
        """The shared confinement gate must validate exact command and stdin."""
        network_ids = ("d" * 64, "e" * 64)
        volume = "conix-inputs-" + "a" * 24
        admitted = {
            "State": {"Status": "created"},
            "Config": {
                "User": "65532:65532", "Entrypoint": ["python"],
                "Cmd": history.SESSION_COMMAND, "StopSignal": "SIGINT",
                "OpenStdin": True, "Tty": False,
            },
            "HostConfig": {
                "Init": True, "ReadonlyRootfs": True, "Privileged": False,
                "CapDrop": ["ALL"], "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": 128 * 1024 * 1024, "MemorySwap": 128 * 1024 * 1024,
                "PidsLimit": 32, "NanoCpus": 10**9,
                "LogConfig": {"Type": "local", "Config": {
                    "max-size": "32k", "max-file": "1", "compress": "false",
                }},
                "PublishAllPorts": False,
            },
            "Mounts": [{
                "Type": "volume", "Name": volume,
                "Destination": "/run/conix", "RW": False,
            }],
            "NetworkSettings": {"Networks": {
                "database": {"NetworkID": network_ids[0]},
                "rpc": {"NetworkID": network_ids[1]},
            }},
        }
        selected = (("database", network_ids[0]), ("rpc", network_ids[1]))
        history._admit_container(admitted, volume, selected, interactive=True)
        for condition in ("wrong_command", "closed_stdin", "tty"):
            with self.subTest(condition=condition):
                record = json.loads(json.dumps(admitted))
                if condition == "wrong_command":
                    record["Config"]["Cmd"] = ["-I", "-m", "conix.unreviewed", "serve"]
                elif condition == "closed_stdin":
                    record["Config"]["OpenStdin"] = False
                else:
                    record["Config"]["Tty"] = True
                with self.assertRaises(OperationError):
                    history._admit_container(record, volume, selected, interactive=True)

    def test_operator_failures_preserve_worker_outcome_on_stderr_only(self):
        docker = Mock(endpoint="unix:///tmp/unused.sock")
        docker.run.return_value = "0\n"
        result = {"outcome": "advanced", "source_id": "a" * 64, "block_number": 1,
                  "block_hash": "0x" + "b" * 64, "revision": 1}
        for fault in ("retirement", "stdout", "wait"):
            stdout = Mock(write=Mock(side_effect=BrokenPipeError)) if fault == "stdout" else io.StringIO()
            cleanup = OperationError("retirement fault") if fault == "retirement" else None
            docker.run.side_effect = OperationError("wait fault") if fault == "wait" else None
            with (
                self.subTest(fault=fault),
                patch.multiple(
                    history, _admit_volume=Mock(), _admit_network=Mock(side_effect=("d" * 64, "e" * 64)),
                    _create=Mock(return_value="c" * 64), _record=Mock(), _owned=Mock(return_value=True),
                    _admit_container=Mock(), _attached=Mock(return_value=(0, json.dumps(result).encode(), b"")),
                    _retire=Mock(side_effect=cleanup),
                ),
                patch("sys.stdout", stdout),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                self.assertRaises(OperationError) as failure,
            ):
                history.advance_history(docker, image="sha256:" + "a" * 64,
                                        volume="inputs", database="database", rpc="rpc")
            self.assertIn(result["block_hash"], stderr.getvalue())
            self.assertIn(fault, str(failure.exception))
            if fault != "stdout":
                self.assertEqual(stdout.getvalue(), "")

    def test_signal_during_retirement_is_reported_after_cleanup(self):
        docker = Mock(endpoint="unix:///tmp/unused.sock")
        docker.run.return_value = "0\n"
        result = {"outcome": "at_tip", "source_id": "a" * 64}
        retired = []

        def signal_while_retiring(*_args):
            os.kill(os.getpid(), signal.SIGTERM)
            retired.append(True)

        previous_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            with (
                patch.multiple(
                    history, _admit_volume=Mock(), _admit_network=Mock(side_effect=("d" * 64, "e" * 64)),
                    _create=Mock(return_value="c" * 64), _record=Mock(), _owned=Mock(return_value=True),
                    _admit_container=Mock(), _attached=Mock(return_value=(0, json.dumps(result).encode(), b"")),
                    _retire=Mock(side_effect=signal_while_retiring),
                ),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                self.assertRaises(OperationError) as failure,
            ):
                history.advance_history(docker, image="sha256:" + "a" * 64,
                                        volume="inputs", database="database", rpc="rpc")
            self.assertEqual(retired, [True])
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn(result["source_id"], stderr.getvalue())
            self.assertIn("interrupted", str(failure.exception))
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.default_int_handler)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)

    def test_signal_after_creation_cannot_start_or_skip_retirement(self):
        docker = Mock(endpoint="unix:///tmp/unused.sock")

        def create_then_signal(*_args):
            os.kill(os.getpid(), signal.SIGTERM)
            return "c" * 64

        previous_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            with (
                patch.multiple(
                    history, _admit_volume=Mock(), _admit_network=Mock(side_effect=("d" * 64, "e" * 64)),
                    _create=Mock(side_effect=create_then_signal), _record=Mock(), _owned=Mock(return_value=True),
                    _admit_container=Mock(),
                ),
                patch.object(history, "_attached") as attached,
                patch.object(history, "_retire") as retire,
                patch("sys.stderr", new_callable=io.StringIO),
                self.assertRaises(OperationError),
            ):
                history.advance_history(docker, image="sha256:" + "a" * 64,
                                        volume="inputs", database="database", rpc="rpc")
            attached.assert_not_called()
            retire.assert_called_once()
            self.assertEqual(retire.call_args.args[-1], "c" * 64)
            self.assertIs(signal.getsignal(signal.SIGTERM), signal.default_int_handler)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    unittest.main()
