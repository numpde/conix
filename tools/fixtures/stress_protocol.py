"""Exact file and byte grants shared by host and contained evidence completion.

This test-only transport contract contains no RPC acquisition or lifecycle.
The editable source remains the retained deployment and ABI, never this list.
"""

RECEIPT = b"CONIX-STRESS-EVIDENCE-V1 "
CAPTURE_INPUTS = {
    "manifest.json": 2 * 1048576,
    "source.json": 65536,
    "abi.json": 8 * 1048576,
    "deployment.toml": 65536,
    "application.warc.gz": 64 * 1048576,
    "observer.warc.gz": 64 * 1048576,
}
BUNDLE_INPUTS = {**CAPTURE_INPUTS, "catalog.toml": 65536}
