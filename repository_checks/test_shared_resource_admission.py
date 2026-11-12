"""Shared Docker resource admission remains independent of worker lifetime."""

import json
import unittest
from unittest.mock import Mock

from tools.containers import resources, history, named, deployment
from tools.containers.client import OperationError


class SharedResourceAdmission(unittest.TestCase):
    def test_three_lifecycles_use_one_resource_authority(self):
        self.assertIs(history._admit_volume, resources._admit_volume)
        self.assertIs(deployment._admit_volume, resources._admit_volume)
        self.assertIs(named._admit_volume, resources._admit_volume)
        self.assertIs(history._admit_network, resources._admit_network)
        self.assertIs(deployment._admit_network, resources._admit_network)
        self.assertIs(named._admit_network, resources._admit_network)
        self.assertIs(history._selected_networks, resources._selected_networks)

    def test_volume_and_network_check_selected_engine_facts(self):
        docker = Mock()
        volume = "conix-inputs-" + "a" * 24
        network = "internal"
        records = [
            {
                "Name": volume, "Driver": "local", "Scope": "local", "Options": None,
                "Labels": {"io.conix.input-token": "a" * 24},
            },
            {
                "Name": network, "Driver": "bridge", "Scope": "local", "Internal": True,
                "Id": "b" * 64,
            },
        ]
        docker.run.side_effect = [json.dumps(record) for record in records]
        self.assertIsNone(resources._admit_volume(docker, volume))
        self.assertEqual(resources._admit_network(docker, network), "b" * 64)
        self.assertEqual(
            [call.args[:2] for call in docker.run.call_args_list],
            [("volume", "inspect"), ("network", "inspect")],
        )

    def test_invalid_ownership_or_external_bridge_refused(self):
        docker = Mock()
        volume = "conix-inputs-" + "a" * 24
        docker.run.return_value = json.dumps({
            "Name": volume, "Driver": "local", "Scope": "local", "Options": None,
            "Labels": {"io.conix.input-token": "b" * 24},
        })
        with self.assertRaises(OperationError):
            resources._admit_volume(docker, volume)

        docker.run.return_value = json.dumps({
            "Name": "rpc", "Driver": "bridge", "Scope": "local", "Internal": False,
            "Id": "c" * 64,
        })
        with self.assertRaises(OperationError):
            resources._admit_network(docker, "rpc")


if __name__ == "__main__":
    unittest.main()
