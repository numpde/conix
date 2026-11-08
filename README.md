Conix — focused PayToPlay collection observer
===============================================

Status: Tier 0 under qualification. Branch 20261108-tier0-executable-core.
The implementation observes one explicitly configured chain, one PayToPlay
contract, one trusted Ethereum RPC and one PostgreSQL database. It does not
validate independent consensus, supply financial finality policy, send
transactions, allocate invoices or administer the payment ledger.

See notes/000_architecture_guidelines.txt for the mandatory concrete-delegation
rule and notes/001_implementation_plan.txt for scope and acceptance gates.

Run only the exact deployment and configured history origin
-----------------------------------------------------------

Build the runtime image through the explicit acquisition and offline packaging
lanes described below. Construct a TOML file based on
examples/conix.toml, replacing the contract address and all deployment values.
The example intentionally uses a zero address and cannot observe a real
PayToPlay deployment. Select the first block whose complete logs you need:
coverage never silently begins at deployment birth or before that block.

The database operator first provisions a separate `conix_owner` login,
`conix_runtime` login and an existing `conix` schema owned by `conix_owner`.
Neither application identity needs PostgreSQL superuser, CREATEDB or CREATEROLE.
Only the schema owner installs tables/constraints and grants runtime DML; it
must not be the account used by the worker or ordinary observation clients.
Keep PostgreSQL bootstrap credentials outside Conix. The database owner also
controls database CONNECT/HBA rules, avoiding unwanted sibling-database access.

Put each PostgreSQL connection string in a separate absolute regular file,
owned by the operator and mode 0600. The normal runtime credential must select
the explicit database.runtime_role and receive no schema ownership, CREATE or
TRUNCATE permission; only SELECT/UPDATE on source_progress, SELECT/INSERT/DELETE
on blocks, and SELECT/INSERT on collections. The checked repair path uses
DELETE on blocks with the existing foreign-key cascade; it does not need DDL.
Do not grant Conix access to pay-to-play's accounting tables. The configured RPC
must permit historical block-hash-pinned eth_call and contract-specific
eth_getLogs at a block hash.

Mount the TOML and connection-file read-only at their configured absolute
container paths. Supply separately authorized RPC and PostgreSQL network access.
The runtime image starts as uid/gid 65532 and exposes no ports. A bridge network
alone is not an egress firewall. Do not mount Docker Engine sockets into conix.

Rootless engines translate container UIDs/GIDs; a host-owned mode-0600 credential
is not readable merely because the container uses the same numeric UID. Never
chmod secrets world-readable or assume matching host/container numeric IDs.

The maintained deployment path uses an engine-owned, one-time staged input
volume instead of host bind mounts. The short-lived, networkless provisioning
container receives only CAP_CHOWN; the actual worker receives no capabilities.
A separate non-root read-only container verifies that uid 65532 can read the
private settings and DSN before activation. The source files remain owned by
the operator and are not altered. The volume is a fixed snapshot: updates
require a new staging generation, not silently edited live files.

Install the schema separately, using ONLY the owner's credential. The
operator's TOML config must name database.dsn_file=/run/conix/connection.dsn
and database.runtime_role=conix_runtime; owner credentials are supplied in
their own host-owned mode-0600 file. The controlled owner lane creates a new
temporary private volume containing this config and owner credential, verifies
the non-root read boundary, invokes the real conix init command on the
selected database network, and retires that volume after the result:

    make schema/init DOCKER_CONTEXT=science CONIX_IMAGE="$runtime" \
      CONIX_CONFIG_FILE=/absolute/config.toml \
      CONIX_OWNER_DSN_FILE=/absolute/owner-postgres.dsn \
      CONIX_DATABASE_NETWORK=payment-db

The one-shot installed command is:

    conix init --config /run/conix-owner/config.toml \
      --owner-dsn-file /run/conix-owner/connection.dsn

This does not require the runtime DSN to be mounted. The owner-only command
refuses a schema owned by another role, a privileged owner login, or a runtime
role that could become the owner; it never silently resets a different source.
A live writer blocks owner initialization through the same advisory lock.
For a schema upgrade in a future tier, extend this owner-only installation
boundary with an explicitly reviewed migration; never invoke DDL from the
ordinary worker or an automatic recovery wrapper.

Stage runtime inputs separately using the (different) conix_runtime DSN.
The worker and ordinary observers execute only:

    conix run --config /run/conix/config.toml
    conix status --config /run/conix/config.toml

The commands neither infer credentials from ambient Docker nor select another
database or RPC. An operator must explicitly retire a failed owner-init input
volume if its cleanup is reported unconfirmed. No ordinary runtime container
may mount or retain the owner's secret.

The worker advances one complete block per transaction, including empty
blocks, and checks the retained checkpoint against the currently configured
RPC source even when at the tip. It performs no automatic retries/restarts.
After transient outage, restart the same command: committed progress determines
the resume position. An unanswered COMMIT must not cause a guessed replay.

If canonical history contradicts retained history, reads become unavailable.
After inspecting the branch and selecting an exact known canonical retained
ancestor, run the deliberate repair command:

    conix repair --config /absolute/conix.toml --to NUMBER:0xBLOCK_HASH

Alternatively start a complete reindex from the configured origin:

    conix rebuild --config /absolute/conix.toml --from-configured-origin

Both require exclusive writer ownership. Do not remove or edit PostgreSQL
records by hand and do not confuse conix repair with accounting correction.
Repair invalidates existing history handles; clients must obtain new ones.
Previously accepted invoices and paid decisions remain outside this database.

An importable conix.observer.Observer supplies the four existing observation
methods over a Journal and Chain. The caller explicitly owns chain/network
and database-resource lifetimes. No HTTP server or alternate scanner exists.

Container preparation and verification
--------------------------------------

Production runtime/start verifies that the selected daemon reports rootless
operation; a local Unix socket is not sufficient. Source/status/stop remain
available for recovery even if the daemon's previous image or external inputs
are absent. CI's disposable build and test engine is not thereby certified
as an approved rootless deployment environment.

The Docker engine is selected explicitly for each Make invocation. Use
DOCKER_CONTEXT=<local-context> on the command line; without a terminal, an
omitted choice is rejected. Docker's ambient DOCKER_HOST, DOCKER_CONTEXT and
COMPOSE_* variables do not authorize the operation. The selected local Unix
socket is pinned through build, run and teardown; remote Engines are refused.

    # Select the existing host-local pool shared with NMR repositories.
    pool="$HOME/Downloads/wheelhouse"
    make check/container
    make deps/images/prepare DOCKER_CONTEXT=science
    make deps/wheelhouse/check DOCKER_CONTEXT=science WHEELHOUSE="$pool"

    # Explicit acquisition ONLY if the pinned tool or locked wheels are absent.
    make deps/wheelhouse/acquire DOCKER_CONTEXT=science WHEELHOUSE="$pool"

    runtime="$(make -s image/build DOCKER_CONTEXT=science WHEELHOUSE="$pool")"
    make image/smoke DOCKER_CONTEXT=science CONIX_IMAGE="$runtime"
    tests="$(make -s tests/image/build DOCKER_CONTEXT=science WHEELHOUSE="$pool")"
    make tests/container DOCKER_CONTEXT=science CONIX_TEST_IMAGE="$tests"


Static private inputs and runtime
---------------------------------

The example config names /run/conix/connection.dsn. Before staging, create
an operator-owned regular TOML file with complete, correct settings and a
separate operator-owned mode-0600 connection file. The file contains the
PostgreSQL DSN as a private value; it is not written into the image, a Compose
environment variable, Docker labels, or a command argument.

Stage and independently prove the inputs on the same selected local Engine:

    make inputs/proof DOCKER_CONTEXT=science CONIX_IMAGE="$runtime" \
      CONIX_CONFIG_FILE=/absolute/config.toml \
      CONIX_DSN_FILE=/absolute/postgres.dsn

The proof destroys its own temporary private volume after verifying that the
real installed conix image reads both files as uid 65532, with a read-only
root filesystem, no capabilities and no network.

For an actual deployment, stage a retained private volume:

    inputs="$(make -s inputs/stage DOCKER_CONTEXT=science CONIX_IMAGE="$runtime" \
      CONIX_CONFIG_FILE=/absolute/config.toml CONIX_DSN_FILE=/absolute/postgres.dsn)"

Do not print or expose the original DSN. The inputs variable is an engine-owned
opaque volume name, not a credential; it is nonetheless bound to one Engine's
storage. Existing RPC and PostgreSQL networks must have been provisioned
explicitly on that Engine. Deploy the one-worker profile with:

    make runtime/start DOCKER_CONTEXT=science CONIX_DEPLOYMENT=pilot \
      CONIX_IMAGE="$runtime" CONIX_INPUT_VOLUME="$inputs" \
      CONIX_DATABASE_NETWORK=payment-db CONIX_RPC_NETWORK=payment-rpc

    make runtime/status DOCKER_CONTEXT=science CONIX_DEPLOYMENT=pilot \
      CONIX_IMAGE="$runtime" CONIX_INPUT_VOLUME="$inputs" \
      CONIX_DATABASE_NETWORK=payment-db CONIX_RPC_NETWORK=payment-rpc

    make runtime/stop DOCKER_CONTEXT=science CONIX_DEPLOYMENT=pilot \
      CONIX_IMAGE="$runtime" CONIX_INPUT_VOLUME="$inputs" \
      CONIX_DATABASE_NETWORK=payment-db CONIX_RPC_NETWORK=payment-rpc

After stopping the worker and ensuring no other reader uses the selected
volume, retire its exact identity explicitly:

    make inputs/remove DOCKER_CONTEXT=science CONIX_INPUT_VOLUME="$inputs"

The runtime profile uses no host binds, Docker Engine mount, public port,
root user or writable root filesystem. It does not create/manage PostgreSQL or
an RPC node. Docker bridge/external networks are reachability grants, not
outbound-destination filtering; enforce any production RPC egress policy at
the selected network/proxy owner. The worker has no automatic restart or fork
repair in Tier 0, and a running container is not evidence of source readiness.
Verify the actual indexed checkpoint and accounting basis separately before
financial use.

Input staging and startup on a particular operator rootless Engine still
require local qualification. Passing CI on a hosted Docker Engine does not
establish that another host's Docker mapping or network topology matches it.

Replace 'science' with your selected Docker context. Every Docker action
resolves one local Unix-socket endpoint, disables ambient client/build/proxy
overrides, and uses that same endpoint for contained acquisition, BuildKit,
service startup, read-only inspection and teardown. An absent context in an
unattended operation is never guessed.

The shared WHEELHOUSE is an existing absolute directory owned by the invoking
operator without symlink ancestors or group/other write access. It is the
same host-local pool that NMR repositories consume. Conix does not create a
per-repository wheelhouse image or a staging directory. The standalone NMR
wheelhouse-tool v0.5.14 is pinned by SHA-256 and reused by byte identity.
A warm check runs target CPython 3.12 pip in --dry-run mode inside a read-only,
networkless container. The only cold acquisition lane bootstraps that release
if missing, derives temporary source-lock-bound pylocks from the original
issuer locks, and acquires missing hash-approved wheels through the upstream
atomic no-clobber publisher. The cold locator uses the explicit PyPI HTTPS
Simple API. Conflicting cached files are preserved, not overwritten.

Target selection and actual Python dependency installation remain in contained
Python/BuildKit environments. Builds use --no-index and --require-hashes and
stream exact selected wheels from the pool under a reader lock, alongside
only checked source inputs. The wheels are mounted ephemerally into installation
steps, not copied into the final image or an intermediate wheelhouse image.
A build tag is published only after both streaming and offline BuildKit
succeed. The host executes the released zipapp only as a streamer; it never
runs pip install.

The pool worker uses the invoking nonzero host UID on a rootful CI Engine.
On a rootless Engine, the namespace's uid 0 maps to the unprivileged host
owner of the shared pool; that worker has no Linux capabilities, no new
privileges, a read-only root and only its explicit pool bind permissions.
Warm workers mount the pool read-only with network disabled. Explicit cold
acquisition gets a writable pool mount and Docker-bridge network reachability.
That bridge is NOT equivalent to NMR's separately qualified interface-bound
proxy: egress routing policy must be enforced independently.

Explicit base-image preparation may consult registries for digest-pinned
Python, PostgreSQL, Foundry and Dockerfile frontend images; repeated pulls
reuse Docker's cached layers. These are separate from warm wheel selection
and ordinary offline builds. A missing wheel or base image fails without
silent alternatives. Upgrade locks and base digests through reviewed changes.
Python wheel lock regeneration using 'python tools/lock_wheels.py --write'
is a separate operator action; the image installation path never invokes
host pip.

The test command does not build or pull an image. It uses the exact selected
image ID, an isolated network, a uniquely named disposable PostgreSQL volume,
non-root read-only containers, bounded resources and no host-published ports.
It attempts to remove its own resources on success/failure; uncertain teardown
reports the owned project for manual recovery. The Docker operator tests run
without a Docker daemon. CI independently installs and checks the wheel.

Real PayToPlay/Anvil and payment-ledger end-to-end qualification is outstanding:
source-only or controlled-RPC tests do not substitute for that proof.

Not in Tier 0: retries, automatic fork recovery, prefetch, caches, alternative
payment logic, parallel/range scanning, multiple writers, other chains or
automatic schema upgrades. An unsupported observation fails rather than
manufacturing complete history, current canonicality or finality evidence.


Real-contract fixture campaign (explicit, test-only)
---------------------------------------------------

Conix consumes dapp32's existing pay-to-play.local-fixture.v1 interface. The
producer checkout, contracts and Anvil Compose stack are NOT copied or edited.
The test harness invokes dapp32's supported fixture/up, validates the returned
private one-line JSON (including exact chain/deployment/ABI identities), checks
the actual internal Docker network and producer-owned Anvil container, then
independently invokes fixture/info. A new unpredictable fixture ID is generated
per test. Attached/existing fixtures are never mutated or automatically removed.

Choose an explicit local rootless Docker context and a real absolute dapp32
checkout. The sibling's own pinned Solidity dependencies/build prerequisites
must be prepared in that checkout before its fixture/up operation; Conix does
not change or install them. Conix builds its test-only Cast tool from dapp32's
reviewed Foundry-image digest. Neither the normal runtime nor the ordinary SQL
test image contains the signing tool.

    pool="$HOME/Downloads/wheelhouse"
    make deps/images/prepare DOCKER_CONTEXT=science
    make deps/wheelhouse/check DOCKER_CONTEXT=science WHEELHOUSE="$pool"
    make deps/fixture-image/prepare DOCKER_CONTEXT=science
    fixture="$(make -s tests/fixture/image/build DOCKER_CONTEXT=science WHEELHOUSE="$pool")"
    make tests/fixture/image/smoke DOCKER_CONTEXT=science CONIX_FIXTURE_IMAGE="$fixture"
    make tests/fixture DOCKER_CONTEXT=science \
      DAPP32_DIR=/absolute/path/to/dapp32 CONIX_FIXTURE_IMAGE="$fixture"

What this explicit lane exercises:
  - dapp32 emits a receipt-verified real contract deployment as JSON;
  - no fixture metadata is accepted from an unverified project/network;
  - Conix creates disposable PostgreSQL roles with separate owner/runtime
    privileges and installs its real schema through the owner-only operation;
  - the original run_session scans real blocks and empty blocks;
  - the test-only transaction driver funds a real position, registers its
    voucher key, signs the contract's own voucherDigest using Cast, and
    collects two actual vouchers across a payee change;
  - the concrete observer serves matching collection pages, pinned position
    values and canonical block evidence;
  - a separate test container restarts the ordinary session on retained SQL
    state with no duplicate effects;
  - Anvil snapshot/revert replaces collection blocks, the same Tier-0 session
    detects and persists the contradiction, the original checked repair_to
    rewinds to a verified retained ancestor, and normal scanning resumes
    without old collection events or valid old cursors.

Private fixture test keys arrive only as stdin to one-shot test containers;
they are absent from image layers, Docker environment and ordinary worker
credentials. Test-only Anvil account impersonation is NEVER a production
transaction-submission mechanism. The temporary Conix SQL stack is retired
before the producer-owned dapp32 fixture. On an uncertain outcome, the harness
retains a private diagnostic directory and prints exact recovery coordinates.
Do not publish its fixture.json or stderr receipts.

The hosted CI campaign proves source contracts and optionally builds/smokes
the test image; it does not create a dapp32 fixture. An actual local run must
be reported separately. Successful Conix event scanning does not establish
pay-to-play invoice acceptance, payment-cycle reconciliation, unknown-COMMIT
fault injection or production finality safety; those remaining T0-D/T0-F gates
require the real accounting consumer and causal failure tests.
