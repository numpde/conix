Conix — generic EVM raw-log indexer
===================================

Current status (2026-11-10): T0-1 ABI, T0-2 offline catalog and
PostgreSQL journal, T0-3 blockHash-pinned scanner, and T0-4 checkpoint-bound
reader are implemented. The obsolete PayToPlay-only runtime was deleted,
with no production data to migrate.

The authoritative end-state specification is
notes/004_rpc_evidence_and_raw_log_indexer_plan.txt. The separate
notes/005_rpc_history_acquisition_and_optimization.txt specifies which
historical RPC methods we use, which evidence forms are admissible, and
how Tier 2 may change fetching without bypassing the core. Historical
notes 001/003 are not implementation contracts; note 000 governs mandatory
concrete delegation by later tiers after generic Tier 0 is complete.

What exists
-----------
src/conix/abi.py is the installed pure-Python interface for compiling
nonanonymous selected events and view/pure functions, decoding exact ABI
wire values, encoding indexed scalar equality filters, encoding read-only
calls and decoding their returns. It delegates ABI mathematics to pinned
eth-abi/eth-utils. No database, network or contract-specific callback enters
the codec. The independent ABI vectors and supported/rejected type matrix
live under tests/ and tests/fixtures/abi/.

The generic catalog, owner-only initializer and atomic raw-history journal
are installed. src/conix/rpc.py and src/conix/core.py add one bounded
no-proxy JSON-RPC transport and the public open_writer() context manager.
It verifies the chain and genesis, queries every configured address/topic0
using eth_getLogs(blockHash), checks block and transaction identities and
ABI data, and rechecks canonicality before publishing one complete block.
Missing evidence, unavailable history and changed branches never become
a successful empty block. No receipts, range queries or retries are used.

src/conix/reader.py provides a SELECT-only open_reader() context manager.
checkpoint() identifies the complete stored prefix; events() applies
indexed-topic equality in SQL with bounded keyset cursor pages. Old
checkpoints remain usable after appends but fail after generation-changing
repairs, including A->B->A. call() executes only declared view/pure
functions using EIP-1898 canonical block hashes and never falls back to
latest. evidence() checks stored ancestors against the selected provider
and distinguishes provider-reported finality from unavailable finality.
These are provider-relative facts, not consensus proofs.

The indexer image remains a library image: image/indexer/check exercises
an installed ABI operation and imports writer/reader APIs, but does not
run a worker. No deployment worker CLI exists. Owner schema initialization
remains a separate Python API, not a Make action.

Build and checks
----------------
The Make API is described by notes/002_make_operator_contract.txt. Plain
make prints help without Docker. Host checks run without network or
credentials:

    make test/repository/run
    make repository/inputs/check

Pinned artifacts are acquired explicitly, never implicitly during build:

    make image/python-base/acquire DOCKER_CONTEXT=agentic
    make image/postgres/acquire DOCKER_CONTEXT=agentic
    make image/dockerfile-frontend/acquire DOCKER_CONTEXT=agentic
    make image/foundry/acquire DOCKER_CONTEXT=agentic
    make wheelhouse/check DOCKER_CONTEXT=agentic WHEELHOUSE="$HOME/Downloads/wheelhouse"

A cold, deliberately authorized acquisition uses wheelhouse/acquire.
All runtime/test image builds stream tracked sources and hash-verified wheels
into the selected Docker Engine's networkless BuildKit and retain no wheel
cache in the finished image.

    runtime="$(make -s image/indexer/build DOCKER_CONTEXT=agentic WHEELHOUSE="$HOME/Downloads/wheelhouse")"
    make image/indexer/check DOCKER_CONTEXT=agentic IMAGE="$runtime"
    runner="$(make -s image/test-runner/build DOCKER_CONTEXT=agentic WHEELHOUSE="$HOME/Downloads/wheelhouse")"
    make test/postgres/run DOCKER_CONTEXT=agentic RUNNER_IMAGE="$runner"

test/postgres/run uses a confined PostgreSQL and Anvil stack, with separate
owner-initialized SQL databases for generic journal tests and real EVM
ingestion. The scanner stores real Anvil-emitted Transfers, processes
an empty block, repairs its suffix and reindexes it. The reader checks
indexed-topic pages, historical view calls, ancestry, post-snapshot
repair invalidation, and old cursors after A->B->A. Controlled HTTP tests
also reject malformed selectors, duplicate logs, missing history and reorgs.
The test runner never receives an owner DSN. Owned containers, network and
volumes are retired after each run. No Docker socket mount, host port,
unreviewed pull, privilege escalation or ambient Docker override is used.

The separate private-input staging test verifies bounded TOML/DSN transport,
non-root readability and cleanup of an engine-owned private volume. Staging
proves *transport*, not configuration admission or successful deployment.
prepare_catalog() admits local immutable ABI snapshots; the deployment
input volume still needs to carry those bytes before a worker can start.

Real-chain acceptance
---------------------
The opt-in test/indexer/acceptance/run consumes a user-owned, mode-0600
dapp32 PayToPlay fixture descriptor and MUST NOT start or retire producer
resources. The opt-in campaign/indexer/acceptance/run uses dapp32's
fixture/up, fixture/info and fixture/down, confirms the selected network,
and owns only the freshly created producer fixture and disposable Conix SQL
project. The dapp32 deployment and contract are external test evidence,
not a new PayToPlay-specific Conix code path.

The generic acceptance runner uses the installed package, a real Anvil RPC,
private stdin handoff, and a separate schema-owner/consumer lifecycle.
Private account data never reaches schema initialization. This acceptance
campaign remains intentionally RED pending the actual dapp32 signed
collection, reader filtering and recovery scenario. The ordinary CI
Anvil test proves the generic reader/writer, not that external workflow.

Security and next step
----------------------
Private DB credentials are never embedded in images or public logs.
Container operators preserve the first failure and report uncertain cleanup
independently. Existing retained deployments may be inspected or removed
using ownership-constrained deployment/status and deployment/remove; no
new generic worker may be started until it is implemented and qualified.

Next is T0-5: qualify real ERC-20/ERC-721 and signed PayToPlay events,
consumer-filtered history, multi-contract recovery and checkpoint queries
against the externally owned dapp32 fixture. The signer and economic
policy remain external. A production worker CLI and private immutable ABI
snapshot staging are separate prerequisites for deployment.
Do not reintroduce Collection models, PayToPlay-specific decoders or
unqualified completeness shortcuts.
