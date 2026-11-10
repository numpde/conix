Conix — generic EVM raw-log indexer
===================================

Current status (2026-11-10): T0-1, the strict generic ABI compiler/codec,
is implemented. The PayToPlay-specific experimental runtime, schema,
journal, observer, CLI and integration suite have been deleted. No deployed
database, checkpoint or API contract needs migration.

The authoritative end-state specification is
notes/004_rpc_evidence_and_raw_log_indexer_plan.txt. Historical notes 001
and 003 are not implementation contracts; note 000 governs mandatory
delegation by later tiers after the generic Tier 0 is complete.

What exists
-----------
src/conix/abi.py is the installed pure-Python interface for compiling
nonanonymous selected events and view/pure functions, decoding exact ABI
wire values, encoding indexed scalar equality filters, encoding read-only
calls and decoding their returns. It delegates ABI mathematics to pinned
eth-abi/eth-utils. No database, network or contract-specific callback enters
the codec. The independent ABI vectors and supported/rejected type matrix
live under tests/ and tests/fixtures/abi/.

No generic catalog, raw-log journal, scanner, checkpoint reader, SQL schema
or deployment CLI exists yet. The indexer-labelled image currently contains
only the ABI package; image/indexer/check verifies its installed codec, NOT
that a worker can scan. deployment/apply and database/schema/initialize are
not published, and no command starts the deleted PayToPlay observer.

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

test/postgres/run uses a disposable PostgreSQL stack to verify only the
current installed ABI tests and the independent owner/runtime/read role
boundaries; it does NOT install a generic journal. On termination, it
retires its owned SQL containers, network and volume. The selected Docker
context must be explicit and local; no host root, socket mount, unreviewed
image pull, privilege escalation or implicit ambient Docker overrides.

The separate private-input staging test verifies bounded TOML/DSN transport,
non-root readability and cleanup of an engine-owned private volume. Staging
proves *transport*, not configuration admission or successful deployment:
ABI snapshots and the full catalog must be validated by T0-2.

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
campaign is intentionally RED until T0-2 through T0-5 deliver the generic
catalog, journal, scanner, reader and actual signed collection scenario.
Ordinary CI does not claim to qualify these missing capabilities.

Security and next step
----------------------
Private DB credentials are never embedded in images or public logs.
Container operators preserve the first failure and report uncertain cleanup
independently. Existing retained deployments may be inspected or removed
using ownership-constrained deployment/status and deployment/remove; no
new generic worker may be started until it is implemented and qualified.

Next is T0-2: compile one immutable TOML/ABI catalog, initialize new
owner-only PostgreSQL tables and grants, and publish complete raw-log blocks.
Do not reintroduce adapters, Collection models, legacy migration code,
PayToPlay-specific event decoders, or permissive fake completeness.
