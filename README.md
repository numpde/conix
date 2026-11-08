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

Build the container with 'make image/build'. Construct a TOML file based on
examples/conix.toml, replacing the contract address and all deployment values.
The example intentionally uses a zero address and cannot observe a real
PayToPlay deployment. Select the first block whose complete logs you need:
coverage never silently begins at deployment birth or before that block.

Put the PostgreSQL connection string in a separate absolute regular file,
owned by the operator and mode 0600. The database role must be authorized to
create only its designated conix schema and its tables. Do not give conix
privileges over the payment ledger. The configured RPC must permit historical
block-hash-pinned eth_call and contract-specific eth_getLogs at a block hash.

Mount the TOML and connection-file read-only into the container at their
configured absolute paths. Supply the intended Docker network explicitly.
The runtime image starts as uid/gid 65532 and does not expose ports.

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

Verification
------------

    make tests/container

This uses the actual installed package, a disposable PostgreSQL instance and
controlled RPC test data; it tears down its own project and volume after
success or failure. CI independently installs and tests the wheel. Real
PayToPlay/Anvil and payment-ledger end-to-end qualification is tracked
separately; source-only or controlled-RPC tests do not substitute for it.

Not in Tier 0: retries, automatic fork recovery, prefetch, caches, alternative
payment logic, parallel/range scanning, multiple writers, other chains or
automatic schema upgrades. An unsupported observation fails rather than
manufacturing complete history, current canonicality or finality evidence.
