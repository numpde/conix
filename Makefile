# Stable resource/action operator API. Do not interpolate operator values into recipes.
# Make's $(value) captures command-line input literally; Python validates it.
override SHELL := /bin/sh
.DEFAULT_GOAL := help

# Never export GNU Make's recursively expanded command-line variables.
unexport DOCKER_CONTEXT WHEELHOUSE IMAGE RUNNER_IMAGE INDEXER_IMAGE DAPP32_DIR FIXTURE_DESCRIPTOR
unexport CONFIG_FILE DSN_FILE INPUT_VOLUME DEPLOYMENT DATABASE_NETWORK RPC_NETWORK
override export CONIX_MAKE_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
override export CONIX_MAKE_WHEELHOUSE := $(value WHEELHOUSE)
override export CONIX_MAKE_IMAGE := $(value IMAGE)
override export CONIX_MAKE_RUNNER_IMAGE := $(value RUNNER_IMAGE)
override export CONIX_MAKE_INDEXER_IMAGE := $(value INDEXER_IMAGE)
override export CONIX_MAKE_DAPP32_DIR := $(value DAPP32_DIR)
override export CONIX_MAKE_FIXTURE_DESCRIPTOR := $(value FIXTURE_DESCRIPTOR)
override export CONIX_MAKE_CONFIG_FILE := $(value CONFIG_FILE)
override export CONIX_MAKE_DSN_FILE := $(value DSN_FILE)
override export CONIX_MAKE_INPUT_VOLUME := $(value INPUT_VOLUME)
override export CONIX_MAKE_DEPLOYMENT := $(value DEPLOYMENT)
override export CONIX_MAKE_DATABASE_NETWORK := $(value DATABASE_NETWORK)
override export CONIX_MAKE_RPC_NETWORK := $(value RPC_NETWORK)

.PHONY: test/repository/run repository/inputs/check image/python-base/acquire image/postgres/acquire image/dockerfile-frontend/acquire image/foundry/acquire wheelhouse/check wheelhouse/acquire image/indexer/build image/test-runner/build image/evm-test-runner/build image/indexer/check image/evm-test-runner/check test/indexer/run test/deployment/acceptance/run test/input-staging/run test/indexer/acceptance/borrowed/run test/indexer/acceptance/owned/run history/block/advance deployment/inputs/stage deployment/inputs/remove deployment/config/init deployment/prepare deployment/database/schema/init deployment/config/check deployment/up deployment/status deployment/logs deployment/down deployment/reader/prepare deployment/reader/check deployment/reader/status deployment/reader/remove help

# Each action's declaration is also the help authority. Recipes share the
# literal-value dispatchers; no Make target aliases or duplicate help list.
RUN_OPERATOR = @python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"
RUN_FIXTURE = @python3 -m tools.fixtures.pay_to_play --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

help: ## Show maintained commands without Docker or side effects
	@printf '%s\n' 'Conix Make API: <resource>[/<subject>]/<action>' \
	  'Docker primitives require DOCKER_CONTEXT=<local Engine>; named deployments use their saved selections.' \
	  'Image selectors are immutable sha256 IDs. WHEELHOUSE is an absolute host pool path.' \
	  'DEPLOYMENT selects config/deployments/<name>/deployment.toml. DSN_FILE is a private file path, never a raw DSN.' \
	  'DATABASE_NETWORK and RPC_NETWORK name existing Engine bridges. No implicit acquisition.' ''
	@awk -F ':[^#]*## ' '/^[a-z][a-z0-9/-]*:.*## / { printf "  %-44s %s\n", $$1, $$2 }' Makefile

test/repository/run: ## Run host-only repository checks; no Docker
	@python3 -m unittest discover -s repository_checks -p 'test_*.py' -v

repository/inputs/check: ## Check pinned inputs and offline build declarations
	@python3 -m unittest discover -s repository_checks -p 'test_input_policy.py' -v

image/python-base/acquire: ## Pull the pinned Python base; DOCKER_CONTEXT=<name>
	$(RUN_OPERATOR)

image/postgres/acquire: ## Pull the pinned PostgreSQL base; DOCKER_CONTEXT=<name>
	$(RUN_OPERATOR)

image/dockerfile-frontend/acquire: ## Pull the pinned BuildKit frontend; DOCKER_CONTEXT=<name>
	$(RUN_OPERATOR)

image/foundry/acquire: ## Pull test-only Foundry image; DOCKER_CONTEXT=<name>
	$(RUN_OPERATOR)

wheelhouse/check: ## Check offline wheelhouse; DOCKER_CONTEXT=<name> WHEELHOUSE=<path>
	$(RUN_OPERATOR)

wheelhouse/acquire: ## Acquire missing pinned wheels; DOCKER_CONTEXT=<name> WHEELHOUSE=<path>
	$(RUN_OPERATOR)

image/indexer/build: ## Build offline installed indexer image; DOCKER_CONTEXT=<name> WHEELHOUSE=<path>
	$(RUN_OPERATOR)

image/test-runner/build: ## Build offline contained indexer test image; DOCKER_CONTEXT=<name> WHEELHOUSE=<path>
	$(RUN_OPERATOR)

image/evm-test-runner/build: ## Build offline EVM test image; DOCKER_CONTEXT=<name> WHEELHOUSE=<path>
	$(RUN_OPERATOR)

image/indexer/check: ## Run offline installed imports/ABI checks; DOCKER_CONTEXT=<name> IMAGE=<sha256:id>
	$(RUN_OPERATOR)

image/evm-test-runner/check: ## Run offline Python/Cast checks; DOCKER_CONTEXT=<name> IMAGE=<sha256:id>
	$(RUN_OPERATOR)

test/indexer/run: ## Run installed indexer/CLI suite in disposable PostgreSQL/Anvil, then retire stack; DOCKER_CONTEXT=<name> required RUNNER_IMAGE=<test-runner sha256:id>
	$(RUN_OPERATOR)

test/deployment/acceptance/run: ## Test catch-up, tip-following, restart continuity and shutdown; owns disposable SQL/RPC; uses saved deployment-acceptance profile
	$(RUN_OPERATOR)

test/input-staging/run: ## Stage catalog/ABIs/runtime DSN, verify confinement, then retire test volume; INDEXER_IMAGE=<sha256:id> CONFIG_FILE=<catalog path> DSN_FILE=<private path>
	$(RUN_OPERATOR)

test/indexer/acceptance/borrowed/run: ## Baseline acceptance; preserve existing dapp32 producer; RUNNER_IMAGE=<evm-test-runner sha256:id> FIXTURE_DESCRIPTOR=<private JSON path>
	$(RUN_FIXTURE)

test/indexer/acceptance/owned/run: ## Create dapp32 fixture; test transactions/restart/manual reorg repair; retire owned consumers then fixture; RUNNER_IMAGE=<evm-test-runner sha256:id> DAPP32_DIR=<checkout>
	$(RUN_FIXTURE)

history/block/advance: ## Advance one complete block then exit; INDEXER_IMAGE=<sha256:id> INPUT_VOLUME=<owned volume> DATABASE_NETWORK=<bridge> RPC_NETWORK=<bridge>
	$(RUN_OPERATOR)

deployment/inputs/stage: ## Stage existing catalog/ABIs/runtime DSN into private volume; print volume name; INDEXER_IMAGE=<sha256:id> CONFIG_FILE=<catalog path> DSN_FILE=<private path>
	$(RUN_OPERATOR)

deployment/inputs/remove: ## Remove selected input volume; INPUT_VOLUME=<owned volume>
	$(RUN_OPERATOR)

deployment/config/init: ## Create editable config skeleton and private ABI/credential directories; DEPLOYMENT=<name> DOCKER_CONTEXT=<name>
	$(RUN_OPERATOR)

deployment/prepare: ## Build offline and stage checked runtime inputs for saved DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/database/schema/init: ## Initialize Conix schema once in externally provisioned PostgreSQL using separate owner DSN; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/config/check: ## Inspect prepared service configuration without creating resources; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/up: ## Start prepared, owner-initialized indexer; no downloads/build/DDL; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/status: ## Show selected existing deployment without requiring prepared image; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/logs: ## Show bounded logs from owned deployment; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/down: ## Stop/remove owned indexer containers; retain external SQL/networks/staged inputs; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/reader/prepare: ## Stage an independent SELECT-only Reader snapshot from the named source; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/reader/check: ## Check the prepared Reader with existing private database/RPC bridges; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/reader/status: ## Show selected Reader image and input volume without exposing credentials; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/reader/remove: ## Retire only the prepared Reader volume and lock; preserve indexer and external SQL; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

