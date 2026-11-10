"""Offline catalog acceptance: source identity, local ABI snapshots and refusals."""

from pathlib import Path
import json
import tempfile
import unittest

from conix.catalog import ConfigurationError, prepare_catalog
from conix.abi import UnsupportedABI


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "abi"
TRANSFER = "Transfer(address,address,uint256)"


class CatalogAdmission(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.abi = self.root / "token.json"
        self.abi.write_bytes((FIXTURES / "erc20.json").read_bytes())
        self.config = self.root / "source.toml"

    def write(self, *, alias="token", address="0x" + "11" * 20, chain_id="1",
              start_block=2, rpc="http://anvil:8545", abi_file="token.json",
              selected=TRANSFER):
        self.config.write_text(
            "version = 1\n"
            "[chain]\n"
            f"chain_id = {chain_id}\n"
            f'genesis_hash = "0x{"00" * 32}"\n'
            f"start_block = {start_block}\n"
            f"rpc_url = {json.dumps(rpc)}\n"
            f"[contracts.{alias}]\n"
            f"address = {json.dumps(address)}\n"
            f"abi_file = {json.dumps(abi_file)}\n"
            f"events = [{json.dumps(selected)}]\n"
            "[database]\n"
            'dsn_file = "/run/conix/connection.dsn"\n'
            'runtime_role = "conix_runtime"\n'
            "[limits]\n"
            "rpc_timeout_seconds = 5\n"
            "operation_timeout_seconds = 30\n"
            "max_response_bytes = 1048576\n"
            "max_connections = 4\n"
            "max_page_size = 100\n"
            "max_evidence_blocks = 64\n"
            "max_logs_per_block = 10000\n"
            "max_abi_bytes = 1048576\n"
            "max_abi_nesting = 8\n"
            "max_abi_elements = 4096\n",
            encoding="utf-8",
        )
        return self.config

    def test_one_erc20_source_id_matches_fixed_canonical_vector(self):
        catalog = prepare_catalog(self.write())
        self.assertEqual(
            catalog.source_id.hex(),
            "8b11c036c7ba2197fbede9c7b41fe2d54f1d0549905b72a4117390a75a043aee",
        )
        self.assertEqual(catalog.chain.chain_id, 1)
        self.assertEqual(catalog.chain.start_block, 2)
        self.assertEqual(catalog.contract("token").descriptor.events[0].signature, TRANSFER)
        self.assertEqual(catalog.contract("token").descriptor.functions[0].signature, "balanceOf(address)")

    def test_alias_rpc_json_order_and_view_functions_do_not_change_history(self):
        first = prepare_catalog(self.write())
        second = prepare_catalog(self.write(alias="asset", rpc="https://provider.example/rpc"))
        self.assertEqual(first.source_id, second.source_id)
        self.assertNotEqual(first.contracts[0].alias, second.contracts[0].alias)

        contents = json.loads(self.abi.read_text())
        contents.append({
            "type": "function",
            "name": "version",
            "stateMutability": "pure",
            "inputs": [],
            "outputs": [{"name": "", "type": "uint256"}],
        })
        self.abi.write_text(json.dumps(contents[::-1], indent=2))
        changed_reader = prepare_catalog(self.config)
        self.assertEqual(first.source_id, changed_reader.source_id)
        self.assertEqual(len(changed_reader.contracts[0].descriptor.functions), 2)

    def test_event_layout_address_and_origin_change_history(self):
        original = prepare_catalog(self.write())
        changed_origin = prepare_catalog(self.write(start_block=3))
        changed_address = prepare_catalog(self.write(address="0x" + "12" * 20))
        self.assertNotEqual(original.source_id, changed_origin.source_id)
        self.assertNotEqual(original.source_id, changed_address.source_id)

        declarations = json.loads(self.abi.read_text())
        declarations[0]["inputs"][-1]["indexed"] = True
        self.abi.write_text(json.dumps(declarations))
        changed_layout = prepare_catalog(self.write())
        self.assertEqual(changed_layout.contracts[0].descriptor.events[0].signature, TRANSFER)
        self.assertNotEqual(original.source_id, changed_layout.source_id)

    def test_erc20_and_erc721_can_share_topic0_on_distinct_addresses(self):
        self.write()
        path = self.root / "collectible.json"
        path.write_bytes((FIXTURES / "erc721.json").read_bytes())
        text = self.config.read_text()
        insertion = (
            '[contracts.collectible]\n'
            'address = "0x' + "22" * 20 + '"\n'
            'abi_file = "collectible.json"\n'
            'events = ["Transfer(address,address,uint256)"]\n'
        )
        self.config.write_text(text.replace("[database]", insertion + "[database]"))
        catalog = prepare_catalog(self.config)
        self.assertEqual(len(catalog.contracts), 2)
        self.assertEqual(
            catalog.contract("token").descriptor.events[0].topic0,
            catalog.contract("collectible").descriptor.events[0].topic0,
        )
        self.assertNotEqual(
            catalog.contract("token").descriptor.events[0].arguments,
            catalog.contract("collectible").descriptor.events[0].arguments,
        )

    def test_invalid_chain_and_duplicate_toml_are_refused(self):
        with self.assertRaises(ConfigurationError):
            prepare_catalog(self.write(chain_id="true"))

        path = self.write()
        path.write_text(path.read_text().replace("start_block = 2", "start_block = 2\nstart_block = 3"))
        with self.assertRaises(ConfigurationError):
            prepare_catalog(path)

    def test_catalog_owns_database_credential_location(self):
        configured = self.write()
        configured.write_text(
            configured.read_text().replace(
                'dsn_file = "/run/conix/connection.dsn"',
                'dsn_file = "/tmp/secret"',
            )
        )
        with self.assertRaises(ConfigurationError):
            prepare_catalog(configured)

    def test_abi_traversal_duplicate_members_and_symlinks_are_refused(self):
        with self.assertRaises(ConfigurationError):
            prepare_catalog(self.write(abi_file="../token.json"))
        path = self.root / "link.json"
        path.symlink_to(self.abi)
        with self.assertRaises(ConfigurationError):
            prepare_catalog(self.write(abi_file="link.json"))
        self.abi.write_text(
            '[{"type":"event","name":"Transfer","name":"Transfer","anonymous":false,"inputs":[]}]'
        )
        with self.assertRaises(ConfigurationError):
            prepare_catalog(self.write())

    def test_missing_selected_event_and_unsupported_layout_are_refused(self):
        with self.assertRaises(UnsupportedABI):
            prepare_catalog(self.write(selected="Missing(uint256)"))

        declarations = json.loads(self.abi.read_text())
        declarations[0]["inputs"][0]["type"] = "fixed128x18"
        self.abi.write_text(json.dumps(declarations))
        with self.assertRaises(UnsupportedABI):
            prepare_catalog(self.write())

    def test_duplicate_deployed_address_is_refused(self):
        self.write()
        text = self.config.read_text()
        self.config.write_text(
            text.replace(
                "[database]",
                '[contracts.other]\n'
                'address = "0x' + "11" * 20 + '"\n'
                'abi_file = "token.json"\n'
                'events = ["Transfer(address,address,uint256)"]\n'
                "[database]",
            )
        )
        with self.assertRaises(ConfigurationError):
            prepare_catalog(self.config)


if __name__ == "__main__":
    unittest.main()
