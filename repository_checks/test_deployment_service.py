"""Retained deployment starts only a pre-admitted stopped container.

The recording Engine never creates resources for real. It records the
ordering and checks actual Docker identities and confinement before start.
"""

import json
import unittest

from tools.containers.client import OperationError
from tools.containers.deployment import activate, show_logs


IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64
VOLUME = "conix-inputs-" + "c" * 24
DATABASE = "db-conix"
RPC = "rpc-conix"
DATABASE_ID = "d" * 64
RPC_ID = "e" * 64
PROJECT = "conix-pilot"


class RecordingEngine:
    def __init__(self, *, existing=False, bad_user=False):
        self.created = existing
        self.bad_user = bad_user
        self.started = False
        self.calls = []

    def require_rootless_engine(self):
        self.calls.append(("require_rootless_engine",))

    def require_role(self, image, role):
        self.calls.append(("require_role", image, role))
        assert (image, role) == (IMAGE, "indexer")

    def verify_inputs(self, image, volume):
        self.calls.append(("verify_inputs", image, volume))
        assert (image, volume) == (IMAGE, VOLUME)

    def record(self):
        return {
            "Id": CONTAINER,
            "Image": IMAGE,
            "State": {"Status": "created"},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": "indexer",
                },
                "Image": IMAGE,
                "User": "0:0" if self.bad_user else "65532:65532",
                "Entrypoint": ["python"],
                "Cmd": ["-I", "-m", "conix", "run"],
            },
            "HostConfig": {
                "Init": True, "ReadonlyRootfs": True, "Privileged": False,
                "CapDrop": ["ALL"], "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": 512 * 1024 * 1024,
                "MemorySwap": 512 * 1024 * 1024,
                "PidsLimit": 128, "NanoCpus": 10**9,
                "RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "local", "Config": {
                    "max-size": "10m", "max-file": "3",
                }},
                "Tmpfs": {
                    "/tmp": "rw,nosuid,nodev,noexec,uid=65532,gid=65532,mode=0700,size=16m",
                },
                "Binds": None, "PortBindings": {}, "Devices": [],
                "DeviceRequests": [], "PidMode": "",
                "PublishAllPorts": False,
            },
            "Mounts": [{
                "Type": "volume", "Name": VOLUME,
                "Destination": "/run/conix", "RW": False,
            }],
            "NetworkSettings": {"Networks": {
                DATABASE: {"NetworkID": DATABASE_ID},
                RPC: {"NetworkID": RPC_ID},
            }},
        }

    def run(self, *args, **_options):
        self.calls.append(args)
        if args[:2] == ("volume", "inspect"):
            return json.dumps({
                "Name": VOLUME, "Driver": "local", "Scope": "local",
                "Options": None, "Labels": {"io.conix.input-token": "c" * 24},
            })
        if args[:2] == ("network", "inspect"):
            name = args[-1]
            return json.dumps({
                "Name": name, "Id": DATABASE_ID if name == DATABASE else RPC_ID,
                "Internal": True, "Driver": "bridge", "Scope": "local",
            })
        if args[0] == "ps":
            return CONTAINER + "\n" if self.created else ""
        if args[:2] == ("container", "inspect"):
            return json.dumps(self.record())
        if args[:2] == ("container", "start"):
            self.started = True
            return ""
        if args[:2] == ("container", "logs"):
            return ""
        if "compose" in args and "create" in args:
            self.created = True
            return ""
        raise AssertionError(f"Unrecognized Docker operation {args!r}")


class DeploymentActivationContracts(unittest.TestCase):
    def activate(self, recorder):
        activate(
            recorder, project=PROJECT,
            compose_argv=("compose", "-p", PROJECT, "-f", "compose/runtime.yml"),
            environment={}, image=IMAGE, volume=VOLUME,
            database=DATABASE, rpc=RPC,
        )

    def test_actual_created_container_is_admitted_before_start(self):
        recorder = RecordingEngine()
        self.activate(recorder)
        self.assertTrue(recorder.started)
        operations = recorder.calls
        index = next(i for (i, x) in enumerate(operations) if x[:2] == ("container", "start"))
        self.assertTrue(any(x[:2] == ("container", "inspect") for x in operations[:index]))
        self.assertEqual(operations[index][-1], CONTAINER)
        self.assertGreaterEqual(sum(x[:2] == ("volume", "inspect") for x in operations), 2)
        self.assertGreaterEqual(sum(x[:2] == ("network", "inspect") for x in operations), 4)

    def test_refuse_existing_service_without_mutation(self):
        recorder = RecordingEngine(existing=True)
        with self.assertRaises(OperationError):
            self.activate(recorder)
        self.assertFalse(recorder.started)
        self.assertFalse(any("create" in x or "start" in x for x in recorder.calls))

    def test_root_user_fails_after_create_but_before_start(self):
        recorder = RecordingEngine(bad_user=True)
        with self.assertRaises(OperationError):
            self.activate(recorder)
        self.assertTrue(recorder.created)
        self.assertFalse(recorder.started)

    def test_logs_are_read_only_and_ownership_checked(self):
        recorder = RecordingEngine(existing=True)
        show_logs(recorder, PROJECT)
        self.assertIn(("container", "logs", "--tail", "100", CONTAINER), recorder.calls)
