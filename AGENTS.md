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

Container and dependency authority
----------------------------------
- Never interpolate operator text into Make recipes. Only the Python operator
  reads literal exported values and constructs subprocess argument arrays.
- Select one named local Docker Engine per operation, pin its Unix endpoint,
  and refuse host root for ordinary operator commands.
- Production runtime/start requires a rootless daemon. Host CI image builds
  are not evidence of rootless deployment readiness.
- No ambient Docker/Compose override, arbitrary host bind, Docker Engine socket,
  automatic image pull or privileged runtime.
- Keep dependency acquisition separate. Use reviewed digest-pinned bases and
  exact hash-locked wheels; package builds and tests are offline.
- Do not expand the Docker build context or installer inputs implicitly.
- Private DSN bytes must never appear in image layers, argv, service logs or
  repository files. Staging is an explicit one-time confined operation.
- Schema installation and upgrades use a separate non-superuser owner login and
  the explicit one-shot schema/init path. Runtime sessions and observers use
  only conix_runtime DML. No owner credentials in the ordinary worker's volume.
- Never add DDL to the concrete Journal/Writer. Evolve schema ownership and
  migration authorization outside the protected Tier-0 scanner and read guard.
- Disposable resources have exact owners and must be retired on success/failure.
  Report uncertain cleanup rather than claiming it succeeded.
- Do not infer runtime egress isolation, UID mapping or genuine chain finality
  from Compose declarations or mock tests.

Qualification
-------------
make check/container and make check/inputs require no Docker, credentials or
network. Other lanes require explicitly reviewed inputs. The installed package
and real PostgreSQL tests run in a contained, disposable stack with a restricted
runtime database role. The full Anvil and pay-to-play accounting scenario must
be qualified independently; do not claim it passed merely because source, SQL
or container tests are green.
