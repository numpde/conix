Conix repository instructions
=============================

Scope
-----
Conix's target is one generic EVM event indexer: a single-ABI-driven scanner,
one complete-block PostgreSQL raw-log journal, and one checkpoint-bound reader
supporting PayToPlay, ERC-20 and ERC-721 without contract-specific source code.
T0-1 ABI, T0-2 offline catalog/owner-only journal, T0-3 blockHash-pinned
scanner and T0-4 checkpoint-bound reader are implemented. T0-5 acceptance
scenarios exist on the working branch, but its opt-in dapp32 campaign has not
been qualified by ordinary CI. The experimental PayToPlay-only runtime was
deleted without migration or adapters.

notes/004_rpc_evidence_and_raw_log_indexer_plan.txt governs the new Tier-0
implementation and qualification. notes/005_rpc_history_acquisition_and_optimization.txt
alone owns historical RPC acquisition method/evidence choices and Tier-2
physical substitution. notes/002_make_operator_contract.txt governs
the existing Make/Docker authority. notes/000_architecture_guidelines.txt
governs future Tier-1/2 concrete delegation ONLY AFTER generic Tier 0 exists.
notes/001_implementation_plan.txt and notes/003_declarative_evm_indexer_design.txt
are historical references, not competing product contracts.

Tier 0 owns rebuildable selected event history, exact checkpoints, guarded
canonicality evidence and read-only block-pinned contract calls. Accounting,
signing, invoice acceptance, payment policy and wallet keys belong elsewhere.
Do not restore the removed PayToPlay-specific scanner, journal, reader,
legacy Collection schema or CLI to make old tests green.

Implementation discipline
-------------------------
- Make small coherent commits. Each commit needs a concise imperative subject
  and an explanatory body recording what changed, why, and what was not proved.
- Report separately what a slice implemented, omitted, and added beyond plan.
- Preserve source identity and fail closed on unknown history, chain state or
  transaction outcome. No silently permissive defaults or generic retries.
- Test behavioral contracts and meaningful diagnostic cause/context/effects;
  avoid brittle assertions on complete error-message wording.
- Keep financial state and policy out of the observer, including test-only
  substitutes presented as a real payment workflow.

Python layout, naming and explanation
-------------------------------------
- Keep ordinary operations easy to read from top to bottom. Separate setup,
  validation, side effects and reporting with purposeful blank lines, not
  whitespace after every assignment or statement.
- Keep short, coherent calls and raises on one line; expand expressions only
  when the argument grouping or decision needs room. Do not compact unrelated
  effects merely to reduce line count.
- Name values after what they represent (e.g. schema owner, Docker endpoint,
  wheel selection, phase receipt), especially at process and trust boundaries.
  Use parenthesized tuple targets when unpacking; use conditional bindings only
  when the observed value is immediately tested and reused.
- Write docstrings and comments in direct English. Explain invariants, trust
  assumptions, resource ownership, why ordering matters, and how failure
  affects durable state. Add an inline comment inside a function when the
  reason is not evident from names and types; avoid narrating Python syntax.
- Treat debug f-strings as useful for named nonsecret diagnostic facts, not as
  permission to stringify untrusted inputs, credentials, subprocess argv or
  RPC responses. Prefer explicit bounded failure categories at such boundaries.
- For developer-only converter failures, conix.diagnostics.note_argument
  captures the first argument's source expression in a traceback note.
  Do not duplicate that expression with handwritten label arguments. Source
  introspection is best effort and never participates in input admission;
  public exception messages remain independent of source availability and
  must not expose evaluated values. Keep semantic operation/phase labels
  when the expression itself cannot convey the required context.
- The Python package and test tooling are both subject to these conventions.
  Preserve actual behavior while restyling; test meaningful contracts rather
  than exact diagnostic copy or source whitespace.

Error transparency and recovery reporting
----------------------------------------
- Do not discard an informative failure merely to label it CampaignError,
  OperationError or a generic test failure. Preserve the first cause, its
  category and the operation/phase that failed across subprocess boundaries.
- Cleanup has an independent outcome. Attempt owned-resource retirement,
  report unconfirmed cleanup separately, and print the primary failure LAST.
  A cleanup failure must not erase a preceding mutation/transaction failure.
- Only source-controlled diagnostic text or a validated bounded diagnostic
  record may reach the terminal. Never print raw fixture JSON, captured
  producer stderr, wallet keys, DB credentials, arbitrary RPC error prose,
  untrusted exception values or full subprocess argv.
- Unexpected errors still expose their type, phase, and a safe source location.
  Do not invent a more specific explanation than the available evidence.
  Retain private receipts for inspection, but do not make ordinary failure
  analysis depend on searching private files.
- Long-running acquisition, build and test actions report start, bounded
  liveness, completion and exact failure. Test cause/context/effects and
  secret non-disclosure, not exact error prose.

Container and dependency authority
----------------------------------
- Never interpolate operator text into Make recipes. Only the Python operator
  reads literal exported values and constructs subprocess argument arrays.
- Select one named local Docker Engine per operation, pin its Unix endpoint,
  and refuse host root for ordinary operator commands.
- Production deployment/apply requires a rootless daemon. Host CI image builds
  are not evidence of rootless deployment readiness.
- No ambient Docker/Compose override, arbitrary host bind, Docker Engine socket,
  automatic image pull or privileged runtime.
- Keep dependency acquisition separate. Use reviewed digest-pinned bases and
  exact hash-locked wheels; package builds and tests are offline.
- Do not expand the Docker build context or installer inputs implicitly.
- Private DSN bytes must never appear in image layers, argv, service logs or
  repository files. Staging is an explicit one-time confined operation.
- T0-2 provides owner-only schema.initialize() and a distinct one-shot
  initializer container for qualification. The installed runtime and reader
  must not receive that credential or execute DDL.
- The installed journal, scanner and checkpoint reader perform no DDL.
  The reader uses only conix_reader SELECT privileges and an explicit,
  checked RPC source. Keep schema establishment owner-only.
- Disposable resources have exact owners and must be retired on success/failure.
  Report uncertain cleanup rather than claiming it succeeded.
- Do not infer runtime egress isolation, UID mapping or genuine chain finality
  from Compose declarations or mock tests.

Qualification
-------------
make test/repository/run and make repository/inputs/check require no Docker, credentials or
network. Other lanes require explicitly reviewed inputs. The installed package
and real PostgreSQL tests run in a contained, disposable stack with a restricted
runtime database role. The full Anvil and pay-to-play accounting scenario must
be qualified independently; do not claim it passed merely because source, SQL
or container tests are green.


Sibling-fixture campaign
------------------------
The explicit campaign/indexer/acceptance/run lane consumes the public dapp32 repository's
supported fixture/up, fixture/info and fixture/down contract, never changes its
source or starts a replacement Anvil implementation. The host admits the bounded private producer descriptor, but projects a
keyless public source for all child phases. Producer test keys never enter
child stdin, output, Docker labels, environments, logs or runtime images.
The optional Foundry Cast binary belongs in the evm-test-runner image alone. Prefer
the deployed contract's voucherDigest over a duplicate EIP-712 implementation.
Only Conix-owned SQL and verified newly created producer fixtures may be
retired automatically; preserve exact recovery records on uncertain outcomes.
The borrowed and owned generic acceptance gates are distinct qualifications.
Borrowed mode is nondestructive baseline acceptance; the owned campaign runs
real Solidity token transactions, signed PayToPlay collection, process restart
and manual reorganization repair. The owned campaign remains UNQUALIFIED until
executed against the actual dapp32 fixture. Contained PostgreSQL/Anvil tests
qualify generic scanner/reader behavior, not financial reconciliation.

Shared wheelhouse
-----------------
Use only an explicitly selected WHEELHOUSE host-local pool shared with NMR
consumers, never a per-repository wheelhouse Docker image or host pip install.
Keep original runtime/build hash locks authoritative and use temporary
source-lock-bound syntax projections only for the upstream released metadata
locator. Warm target-Python selection must run in a contained, networkless,
read-only worker; only explicit acquisition of missing artifacts may use a
network and writable pool mount. Pin wheelhouse-tool by full released SHA-256.
Stream only tracked, hashed source inputs and the selected wheel manifest
directly into the same selected Engine's offline BuildKit; the installer
never accesses a package index or writes wheels into final image layers.
Do not assume rootless UID mapping or network interface binding from Docker
context names; the security proof must be per selected Engine. Cross-repo
dapp32 fixture creation requires an explicit named context, not a silent
interactive re-selection by a second Make process.

Tool ownership
--------------
tools/containers/client.py owns sanitized selected Docker CLI calls and
rootless/image admission. tools/containers/operator.py composes that client
for Conix-specific images, private inputs, deployment and contained tests.
The client must not import application lifecycles or the test harness.
tools/fixtures/pay_to_play.py owns the dapp32 fixture campaign using the
selected Docker operator without changing the sibling checkout.
tools/fixtures/protocol.py owns the test-only JSON fixture admission and
phase records; its source is copied into EVM/test images but never packaged
in the installed Conix runtime. Host operators must not import from tests/.
tools/wheelhouse/ contains issuer lock regeneration, released NMR tool
bootstrap, shared pool/source policy, contained target selector, and offline
image-build orchestration. Invoke operators with python -m via Make; avoid new
top-level tools/*.py scripts or reintroducing manual Python path mutation.
No operator module is packaged into the installed Conix runtime distribution.

Operator API contract
---------------------
notes/002_make_operator_contract.txt is the public Make interface. Targets
name a stable resource, role or subject, and precise action, not Python
locations, implementation tiers or test mechanisms. Do not introduce aliases
for retired names. Distinguish read-only check, explicit acquire, offline
build, test run, retained resource create, status and remove. Owner schema
initialization and deployment apply are absent until implemented generically. Config/check validates the concrete Compose graph but does
not produce a rendered document. Deployment/remove retires only the selected
labelled Conix indexer containers, not external networks, SQL or inputs.

The borrowed test/indexer/acceptance/run accepts a mode-0600 private JSON
fixture descriptor and has NO producer lifecycle rights.
campaign/indexer/acceptance/run creates, verifies and owns one producer
fixture, then retires its own consumers before calling the producer's
fixture/down. Do not reintroduce the old PayToPlay-only test command.

IMAGE names the single image checked for its expected role; RUNNER_IMAGE names
a composite test runner; INDEXER_IMAGE names the installed core library image used by private-input
transport checks, NOT a runnable indexer worker. Both selectors are
immutable sha256 IDs and
the image role is validated before any durable activity. WHEELHOUSE and
DOCKER_CONTEXT stay explicit operator choices; source, test and build tooling
must never change the selected engine implicitly. New test targets require
separate source/admission tests and explicit granted resources.
