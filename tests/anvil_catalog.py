"""Prepare the same real-Anvil catalog for its owner and runtime processes.

The owner and test runner use separate containers and credentials. Each
compiles the same local ABI snapshot against Anvil's observed genesis.
"""

import json
from pathlib import Path
import tempfile

from conix.catalog import Catalog, prepare_catalog


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCALARS_ADDRESS = "0x" + "33" * 20
SCALARS_EVENT = "IndexedScalars(int8,bool,bytes4)"


def catalog_for_anvil(*, genesis_hash: str, rpc_url: str) -> Catalog:
    """Compile the same selected event source for the owner and both consumers."""
    template = (FIXTURES / "catalog.toml").read_text(encoding="utf-8")
    template = template.replace(
        'genesis_hash = "0x' + "00" * 32 + '"',
        f'genesis_hash = "{genesis_hash.lower()}"',
    )
    template = template.replace(
        'rpc_url = "http://anvil:8545"',
        "rpc_url = " + json.dumps(rpc_url),
    )
    template += (
        f'\n[contracts.scalars]\naddress = "{SCALARS_ADDRESS}"\n'
        f'abi_file = "abi/indexed_scalars.json"\nevents = ["{SCALARS_EVENT}"]\n'
    )

    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        (directory / "abi").mkdir()
        for filename in ("erc20.json", "indexed_scalars.json"):
            (directory / "abi" / filename).write_bytes((FIXTURES / "abi" / filename).read_bytes())
        config = directory / "source.toml"
        config.write_text(template, encoding="utf-8")
        return prepare_catalog(config)
