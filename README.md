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

Named T0-7 B deployment (candidate)
----------------------------------
T0-7 B is the standalone continuous indexer. Its normal lifecycle follows
the saved-deployment model used by Agent Mail and the prepare/run separation
used by NMR provider tools. The schema, scanner, journal, reader and
one-shot history/advance command retain their existing owners.

A fresh deployment first records one nonsecret selection. The init command
prompts once for any omitted settings when attached to a terminal; in CI or
another unattended shell all values must be supplied explicitly:

    make deployment/init DEPLOYMENT=local DOCKER_CONTEXT=agentic

One-time settings are CONFIG_FILE (absolute Conix source TOML, with adjacent
abi/ files), DSN_FILE (mode-0600 runtime database credential), OWNER_DSN_FILE
(mode-0600 schema-owner database credential), DATABASE_NETWORK, RPC_NETWORK,
and WHEELHOUSE (existing shared offline wheel pool). Initialization stores
only their *paths/names*, not credential contents, under ignored
config/deployments/local/deployment.toml (mode 0600, parent mode 0700).
There is no implicit Docker context, network, database or source.

Then run the named operations without repeating the underlying resources:

    make deployment/prepare DEPLOYMENT=local
    make deployment/database/init DEPLOYMENT=local
    make deployment/config/check DEPLOYMENT=local
    make deployment/up DEPLOYMENT=local
    make deployment/status DEPLOYMENT=local
    make deployment/logs DEPLOYMENT=local
    make deployment/down DEPLOYMENT=local

Prepare checks the pinned cached wheelhouse, builds the selected image
offline, checks the image, stages and verifies a private input volume, then
records the exact image/volume IDs in a private prepared receipt. No missing
base image or wheel is automatically downloaded. Use the separate explicit
image/*/acquire and wheelhouse/acquire operations if inputs are missing.
Preparing an upgrade requires the selected standalone service to be down.
Preparation alone can be qualified on a rootful CI Engine, using the same
existing offline build and staging rules. Actual retained startup and
schema-owner operations still require a verified rootless Engine.
An old input volume is retired only after replacement preparation succeeds.

The PostgreSQL service and roles/schema must ALREADY exist on the selected
internal database network: conix_owner owns schema conix, while the distinct
conix_runtime and conix_reader roles have their ordinary restricted grants.
The separately authorized deployment/database/init sends the private owner
credential to a short-lived non-root container through stdin only and
calls the owner-only conix.schema.initialize() API. It never makes the
ordinary indexer a schema owner. It is create-only and refuses initialized
or incompatible history. Its outcome is unknown if transport fails; inspect
the database before retrying.

Up starts only a previously prepared candidate. It does NOT prepare an
image, pull dependencies, initialize the schema, create Docker networks or
change source identity. The actual container runs `python -I -m conix run`.
The host command exits once the contained service starts. Tier 0 does not
automatically retry transient RPC failures, repair a reorganization or
restart a failed service. Status/logs/down inspect or retire only owned
indexer containers and preserve the external database, networks, image
and staged inputs. If the saved declaration has been lost, recovery may
use an explicit DOCKER_CONTEXT=<name> with DEPLOYMENT=<name> for those three
commands only. A conflicting context is rejected while the saved file exists.

For a complete disposable demonstration and installed-process qualification:

    make test/standalone/run DOCKER_CONTEXT=agentic WHEELHOUSE="$HOME/Downloads/wheelhouse"

That command builds a test runner offline from selected cached inputs,
creates an isolated Anvil/PostgreSQL stack, initializes the schema under
its separate owner, runs the contained tests (including a real launched
`python -I -m conix run` process), and retires owned resources. It is NOT
a deployed instance and cannot alter the saved named deployment.

Manual repair stays explicit. Inside an appropriately confined container
with the selected staged inputs, `python -I -m conix history inspect`
reads bounded retained ancestry; `python -I -m conix history repair
--ancestor N` uses the existing core's canonicality-checked repair.
Do not treat a stored identity alone as a canonical-chain proof.

Remaining qualification: a dedicated retained-deployment campaign on
the selected rootless Engine and the full dapp32 end-to-end scenario.
Existing GitHub CI and the disposable Anvil/PostgreSQL tests are evidence
only for the paths they actually execute. No runtime production readiness
or automatic recovery guarantee follows from a green repository check.
