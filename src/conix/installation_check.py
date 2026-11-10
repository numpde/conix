"""Check that the installed Conix distribution exposes the expected ABI core.

This runs without a checkout, database or RPC. It is deliberately a small
operational smoke check, not a substitute for the installed test suite.
"""

from importlib.util import find_spec

from conix.abi import compile_event, encode_indexed_filter


def check_installation() -> None:
    event = compile_event({
        "type": "event",
        "name": "Ready",
        "anonymous": False,
        "inputs": [{"name": "value", "type": "uint256", "indexed": True}],
    })
    actual_topic = encode_indexed_filter(event, "value", 7)
    expected_topic = (7).to_bytes(32, "big")

    if actual_topic != expected_topic:
        raise RuntimeError(f"Installed ABI codec produced the wrong {actual_topic = !r}.")

    for obsolete_module in ("conix.pay_to_play", "conix.journal", "conix.commands"):
        if find_spec(obsolete_module) is not None:
            raise RuntimeError(f"Installed wheel still contains {obsolete_module = !r}.")


if __name__ == "__main__":
    check_installation()
