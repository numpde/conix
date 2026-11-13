Conix is a standalone blockchain indexer.

It connects to blockchain nodes through RPC, retrieves historical and new blocks, transactions, receipts, and contract logs, and decodes events using contract ABIs. It stores the resulting data in PostgreSQL and exposes it through an API.

It maintains its position on each chain, resumes after restarts, and detects blockchain reorganizations. When previously indexed blocks are replaced, it updates the stored records to reflect the new canonical chain while preserving the relevant history and evidence.

In its intended end-state, Conix supports multiple blockchains and contracts, with configurable indexing and queries. It runs continuously, catches up when behind, and handles recoverable failures automatically.

Conix records what happened on-chain. It does not execute transactions or determine the business meaning of those events.

### How to use Conix

#### 1. Provision PostgreSQL

Conix uses an existing PostgreSQL database. It does not create or manage the database server.

The operator provisions the database and three roles:

- `conix_owner`: Owns the Conix schema and initializes its tables.
- `conix_runtime`: Writes indexed observations and maintains checkpoints.
- `conix_reader`: Has read-only access to indexed data.

The database must be reachable from the indexer through a configured Docker network. The blockchain RPC endpoint must also be reachable.

#### 2. Configure the indexer

Create a named deployment:

```bash
make deployment/config/init DEPLOYMENT=ethereum DOCKER_CONTEXT=agentic
```

This creates:

```text
config/deployments/ethereum/
├── deployment.toml
├── abi/
└── secrets/
    ├── owner.dsn
    └── runtime.dsn
```

Edit `deployment.toml` to specify:

- Docker context and existing database/RPC networks.
- Blockchain chain ID, genesis hash, RPC URL, and starting block.
- Contract addresses, ABI files, and events to index.
- Resource limits and operation timeouts.

For example, to index ERC-20 transfers:

```toml
[contracts.token]
address = "0x..."
abi_file = "abi/token.json"
events = ["Transfer(address,address,uint256)"]
```

Place the contract's ABI in `abi/token.json`.

The PostgreSQL connection strings are stored separately:

```text
secrets/owner.dsn
secrets/runtime.dsn
```

These contain the database hostname, port, database name, username, and password. They are private files, not entries in the TOML configuration.

The database network is selected in `deployment.toml` through `database_network`. The hostname and credentials for the actual PostgreSQL connection come from the DSN files.

#### 3. Prepare and start the indexer

```bash
make deployment/prepare DEPLOYMENT=ethereum &&
make deployment/database/schema/init DEPLOYMENT=ethereum &&
make deployment/config/check DEPLOYMENT=ethereum &&
make deployment/up DEPLOYMENT=ethereum
```

Preparation builds the indexer image, stages the selected configuration and credentials, and records the prepared resources in `deployment.lock.toml`.

Schema initialization creates the Conix tables and grants the appropriate permissions. It uses the owner credential, which is not passed to the running indexer.

The indexer then starts, processes historical blocks from its configured starting point, and continues following the chain.

Its progress is stored in PostgreSQL, allowing subsequent executions to resume from the last completed checkpoint.

Operational commands include:

```bash
make deployment/status DEPLOYMENT=ethereum
make deployment/logs DEPLOYMENT=ethereum
make deployment/down DEPLOYMENT=ethereum
```

#### 4. Connect a Reader

The Reader is a separate consumer of the indexed data. It does not connect to the indexer's process.

It connects to:

1. The same PostgreSQL database, using the `conix_reader` role.
2. A blockchain RPC endpoint, used to check chain identity, canonicality, and historical contract state.

It also needs the indexer's selected source catalog, including the contract ABIs.

The intended arrangement is:

```text
                 Blockchain RPC
                  │         │
                  ▼         ▼
               Indexer    Reader
                  │         │
                  ▼         ▼
               PostgreSQL
                  │
           Indexed history
```

The named deployment uses the same existing internal PostgreSQL and RPC
bridges for Readers. Install the separate, mode-0600 credential
`config/deployments/ethereum/secrets/reader.dsn` for the PostgreSQL
`conix_reader` role; do not copy `runtime.dsn` or `owner.dsn`.

After the indexer has been prepared and its schema initialized:

```bash
make deployment/reader/prepare DEPLOYMENT=ethereum &&
make deployment/reader/check DEPLOYMENT=ethereum &&
make deployment/reader/status DEPLOYMENT=ethereum
```

Preparation derives the source catalog and selected ABIs from the **same**
`deployment.toml`, reuses the exact indexer image, and stages only the Reader
credential in a **different** private Docker input volume. It records that
selection atomically in the generated `reader.lock.toml`; it neither restarts
the indexer nor modifies `deployment.lock.toml`.

The check runs a short-lived, unprivileged Reader container on both selected
internal bridges. It calls the installed `open_reader()`, verifies the
dedicated SQL role, selected chain and durable source, reports its checkpoint,
then retires only its own check container. There is **no Reader daemon**.

An independently operated Reader container on the **same selected Docker
Engine** can use the image and volume shown by `deployment/reader/status`,
mount the Reader volume read-only at `/run/conix`, and join those two
existing internal networks. The client uses the installed Python API:

```python
from conix.input_staging import load_runtime_inputs
from conix.reader import open_reader

catalog, reader_dsn = load_runtime_inputs()
async with open_reader(
    catalog=catalog,
    database_dsn=reader_dsn,
    rpc_url=catalog.chain.rpc_url,
) as reader:
    checkpoint = await reader.checkpoint()
```

The Reader's private input volume belongs to the selected local Engine; it
is not a portable cross-host artifact. External consumer lifetimes belong
to their own applications. When no consumer uses the snapshot, retire it
independently:

```bash
make deployment/reader/remove DEPLOYMENT=ethereum
```

Removal checks the Reader's exact owned volume and retains the lock if
retirement fails. It does not stop the indexer or delete SQL/network
resources. Changing a deployment's source requires preparing a new Reader
snapshot; old snapshots are not silently rewritten. Neither a Reader
snapshot nor its container receives the schema-owner or runtime-writer DSN.

#### 5. Query indexed events

Once connected, the application obtains a checkpoint and queries the indexed history.

For example:

```python
checkpoint = await reader.checkpoint()

page = await reader.events(
    checkpoint=checkpoint,
    contract="token",
    event="Transfer(address,address,uint256)",
    where={"from": wallet_address},
    limit=100,
)
```

This returns ERC-20 Transfer events originating from the selected wallet.

Each event includes decoded ABI values, the original event topics and data, the contract address, block identity, transaction hash, and log position.

Queries operate against a specific checkpoint, rather than the moving chain tip.

Additional results are retrieved using `page.next_cursor` with the same query and checkpoint. This allows an application to traverse a consistent history while Conix continues indexing new blocks.

The Reader can also execute declared read-only contract functions at a specific historical block:

```python
balance = await reader.call(
    checkpoint=checkpoint,
    contract="token",
    function="balanceOf(address)",
    arguments=(wallet_address,),
)
```

The function executes against the checkpoint's block hash, not the latest blockchain state.

#### 6. Retrieve and verify evidence

Suppose an application receives a Transfer event and needs to verify its blockchain provenance.

The event already identifies the transaction, block, and original log.

The application can request additional chain evidence:

```python
event = page.events[0]

evidence = await reader.evidence(
    checkpoint=checkpoint,
    block_numbers=(event.block.number,),
    include_finalized=True,
)
```

Conix compares the retained block identities with the canonical history reported by the selected RPC provider.

The result contains:

- The requested block headers, including hashes, parent hashes, and timestamps.
- The checkpoint against which the evidence was obtained.
- The provider's reported finalized block, when available.
- An explicit finality status, including when finality information is unavailable.

If the stored history conflicts with the provider's canonical chain, the Reader raises an error rather than returning the evidence as valid.

The application can use the original event data and returned block identifiers to independently retrieve and verify the corresponding blockchain records.

A checkpoint therefore establishes the scope of indexed history, while the evidence operation checks its continued agreement with the selected blockchain provider.

This is provider-relative evidence, not an independent consensus proof.

#### 7. Handle reorganizations and interruptions

Conix stores its indexing progress in PostgreSQL. After a normal restart, it resumes from the durable checkpoint rather than indexing everything again.

If a blockchain reorganization affects previously indexed blocks, the stored history must be reconciled with the new canonical chain.

The current implementation provides explicit inspection and repair operations. Its Reader invalidates checkpoints and pagination cursors affected by a repair.

The intended end-state extends this with automatic handling of recoverable failures and reorganizations, while retaining explicit failure states for conditions requiring operator intervention.

### Current interface boundaries

The implemented consumer interface is a Python API providing `checkpoint()`, `events()`, `call()`, and `evidence()`.

The current indexer records selected raw contract logs, transaction identifiers, and block headers. Full transaction/receipt acquisition, broader query capabilities, and an HTTP API should not be assumed to exist merely because they are possible end-state extensions.

Named Reader preparation, connection checks, status and removal are provided
by the `deployment/reader/*` operator commands. They reuse the named source
configuration and prepare a separate Reader input volume and read-only
credential. Reader consumers own their application processes on the same
Docker Engine; Conix does not publish a Reader HTTP service or manage
consumer deployments on other hosts.


### Optional Tier 1 resilience

The default `conix run` remains the Tier 0 application. Two opt-in Python
compositions are available: `conix.resilience.rpc_retry.run_with_read_retries`
adds bounded selected-read retries; `conix.resilience.supervisor.supervise`
adds at most two fresh application sessions after classified connection/read
failures, once the previous writer and HTTP session have retired.

Provider rejection, malformed or contradictory evidence, SQL failures and
uncertain COMMIT outcomes do not grant a restart. There is no automatic
reorganization repair or production deployment change. T1-B passed 141
contained tests (2 expected skips) on fw/rootless agentic at `455ecfd`.

The unmerged `main-tier-1-reorg-ancestor-20261113` branch implements bounded
canonical-ancestor selection without repair. Selected-rootless-Engine
qualification and integration remain open.

[Note 007](notes/007_tier_1_qualification.txt) records policies, exact revisions,
CI evidence and qualification limits.
