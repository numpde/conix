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

Put the PostgreSQL connection string in a separate absolute regular file,
owned by the operator and mode 0600. The database role must be authorized to
create only its designated conix schema and its tables. Do not give conix
privileges over the payment ledger. The configured RPC must permit historical
block-hash-pinned eth_call and contract-specific eth_getLogs at a block hash.

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

    conix init --config /absolute/conix.toml
    conix run --config /absolute/conix.toml
    conix status --config /absolute/conix.toml

Run these as container commands with mounts and network configured by the
operator. The entry point does not acquire credentials from ambient Docker
or infer a database or RPC from the container environment.

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

The Docker engine is selected explicitly for each Make invocation. Use
DOCKER_CONTEXT=<local-context> on the command line; without a terminal, an
omitted choice is rejected. Docker's ambient DOCKER_HOST, DOCKER_CONTEXT and
COMPOSE_* variables do not authorize the operation. The selected local Unix
socket is pinned through build, run and teardown; remote Engines are refused.

    make check/container
    make deps/images/prepare DOCKER_CONTEXT=science
    wheels="$(make -s deps/wheelhouse/build DOCKER_CONTEXT=science)"
    runtime="$(make -s image/build DOCKER_CONTEXT=science WHEELHOUSE_IMAGE="$wheels")"
    make image/smoke DOCKER_CONTEXT=science CONIX_IMAGE="$runtime"
    tests="$(make -s tests/image/build DOCKER_CONTEXT=science WHEELHOUSE_IMAGE="$wheels")"
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

Replace 'science' with your selected Docker context. The preparatory commands
are deliberately networked and consume reviewed base-image digests and the
committed per-wheel SHA-256 locks. Runtime/test packaging does not download
Python dependencies. A missing source wheel or base image fails explicitly;
no alternate image or unpinned package resolution is attempted. Upgrade locks
and base digests only through deliberate, reviewed changes. Python wheel lock
regeneration is supported for CPython 3.12 on Linux x86_64 using
'python tools/lock_wheels.py --write'; it contacts the network and must be
run separately from ordinary verification.

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
