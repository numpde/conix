Conix repository instructions
=============================

Scope
-----
Conix is a focused PayToPlay collection observer. It owns rebuildable chain
history, exact checkpoints, canonicality evidence and guarded observations.
It does not own invoice acceptance, accounting, wallet keys, nonce management
or collection decisions.

The governing contracts are notes/000_architecture_guidelines.txt and
notes/001_implementation_plan.txt. Tier 0 is the concrete program in every
future composition. Do not replace its scanner, publisher, observation guards
or repair with alternative implementations behind compatible interfaces.

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
- Schema installation and upgrades use a separate non-superuser owner login and
  the explicit one-shot database/schema/initialize path. Runtime sessions and observers use
  only conix_runtime DML. No owner credentials in the ordinary worker's volume.
- Never add DDL to the concrete Journal/Writer. Evolve schema ownership and
  migration authorization outside the protected Tier-0 scanner and read guard.
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
The explicit campaign/pay-to-play/run lane consumes the public dapp32 repository's
supported fixture/up, fixture/info and fixture/down contract, never changes its
source or starts a replacement Anvil implementation. The descriptor is a
bounded private stdin handoff; its test keys cannot appear in output, Docker
labels, environment variables, log messages or runtime image layers.
The optional Foundry Cast binary belongs in the evm-test-runner image alone. Prefer
the deployed contract's voucherDigest over a duplicate EIP-712 implementation.
Only Conix-owned SQL and verified newly created producer fixtures may be
retired automatically; preserve exact recovery records on uncertain outcomes.
The borrowed test, owned fixture campaign and pay-to-play accounting acceptance gate are
distinct qualifications and must be reported separately.

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
tools/containers/operator.py is the sole general Docker operator: explicit
engine selection, image and input-volume lifecycle, and runtime services.
tools/fixtures/pay_to_play.py owns only the dapp32 JSON fixture campaign,
using the selected Docker operator without changing the sibling checkout.
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
build, test run, owner initialize, retained resource create, deployment apply,
status and remove. Config/check validates the concrete Compose graph but does
not produce a rendered document. Deployment/remove deletes only its Conix
Compose project, never external networks, PostgreSQL data or staged inputs.

The standalone test/pay-to-play/run accepts a private owned mode-0600 JSON
fixture descriptor and may alter the selected Anvil state through transactions,
snapshots and reorganization. It has NO permission to invoke dapp32 up/info/down
or retire producer resources. Campaign/pay-to-play/run creates and inspects its
own producer fixture, runs exactly the same test and calls dapp32 down only
after owned consumer retirement. These actions must remain separate in code,
tests, Make, help text and recoverability diagnostics.

IMAGE names the single image checked for its expected role; RUNNER_IMAGE names
a composite test runner; INDEXER_IMAGE names the application image used by
schema, inputs and deployment. Both selectors are immutable sha256 IDs and
the image role is validated before any durable activity. WHEELHOUSE and
DOCKER_CONTEXT stay explicit operator choices; source, test and build tooling
must never change the selected engine implicitly. New test targets require
separate source/admission tests and explicit granted resources.
