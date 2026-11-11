Conix — generic EVM raw-log indexer
===================================

The generic ABI codec, catalog, PostgreSQL journal, blockHash-pinned scanner
and checkpoint-bound reader have completed T0-6 qualification. The selected
core baseline is frozen for concrete delegation by later tiers. Note 004,
section 8.8, records the tested environment and scope. The obsolete
PayToPlay-only runtime was deleted, with no production data to migrate.

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

The image/indexer/check lane exercises installed ABI behavior and imports
the writer and reader APIs. The foreground worker is an explicit module
invocation. Owner schema initialization is a separate Python API.

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

The private-input staging test sends the TOML, selected ABI files and DSN
through bounded stdin into an engine-owned volume. Use operator-owned input
files. ABI paths in staged TOML have the form `abi/<name>.json`, relative
to the configuration directory. The installed `prepare_catalog()` admits
the snapshot from a read-only mount as the runtime UID. The proof lane
retires its volume. Worker activation is a separate operation.

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
and separate schema-owner/consumer lifecycles. The host admits dapp32's
private fixture descriptor, then sends only public contract, ABI and account
coordinates to test consumers. The schema owner receives an even narrower
source projection. No child phase receives dapp32 wallet keys.

The owned campaign deploys real compiled ERC-20/ERC-721 fixtures before schema
initialization, indexes a mixed token/payment block, verifies signed PayToPlay
collections and filtered history, restarts in a fresh process, and exercises
an Anvil reorganization with manual repair. Borrowed-fixture mode runs only
the nondestructive baseline.

After explicit dependency acquisition and a verified rootless Docker context,
build the EVM test runner from the already selected wheelhouse, then run:

    evm_runner="$(make -s image/evm-test-runner/build DOCKER_CONTEXT=agentic WHEELHOUSE="$HOME/Downloads/wheelhouse")"
    make image/evm-test-runner/check DOCKER_CONTEXT=agentic IMAGE="$evm_runner"
    make campaign/indexer/acceptance/run DOCKER_CONTEXT=agentic RUNNER_IMAGE="$evm_runner" DAPP32_DIR="$HOME/repos/dapp32"

Select the actual local Docker context and prepared dapp32 checkout. The
campaign creates and retires an isolated producer fixture and a separate
Conix SQL project; on uncertain cleanup, inspect the reported ownership IDs.

Note 004, section 8.8, records the tested revisions and environment, the scope
of ordinary CI, and the open Tier-0 release gates.

Security and next step
----------------------
Private DB credentials are never embedded in images or public logs.
Container operators preserve the first failure and report uncertain cleanup
independently. Existing retained deployments may be inspected or removed
using ownership-constrained deployment/status and deployment/remove.

The signer and economic policy remain external. After the owner initializes the
schema and you stage the runtime inputs, select two existing internal
Docker networks with the database and RPC peers:

    make history/advance DOCKER_CONTEXT=agentic INDEXER_IMAGE="$indexer" \
      INPUT_VOLUME="$inputs" DATABASE_NETWORK="$db_network" RPC_NETWORK="$rpc_network"

The command runs the installed `conix.worker advance` once and retires its
container. It reports the confirmed block identity or that the source was
at tip. Production deployment activation remains unavailable.
Schema initialization uses the owner-only `conix.schema.initialize()` API
in a separate process. Readers use `open_reader()` with the SELECT-only
credential. An operator chooses a retained canonical ancestor and invokes
`Writer.repair_to()` explicitly when repair is required.
Do not reintroduce Collection models, PayToPlay-specific decoders or
unqualified completeness shortcuts.
