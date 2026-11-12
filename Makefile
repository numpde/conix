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

.PHONY: test/repository/run repository/inputs/check image/python-base/acquire image/postgres/acquire image/dockerfile-frontend/acquire image/foundry/acquire wheelhouse/check wheelhouse/acquire image/indexer/build image/test-runner/build image/evm-test-runner/build image/indexer/check image/evm-test-runner/check test/indexer/run test/deployment/run test/input-staging/run test/indexer/acceptance/run campaign/indexer/acceptance/run history/advance deployment/inputs/create deployment/inputs/remove deployment/init deployment/prepare deployment/database/init deployment/config/check deployment/up deployment/status deployment/logs deployment/down help

# Each action's declaration is also the help authority. Recipes share the
# literal-value dispatchers; no Make target aliases or duplicate help list.
RUN_OPERATOR = @python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"
RUN_FIXTURE = @python3 -m tools.fixtures.pay_to_play --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

help: ## Show maintained commands without Docker or side effects
	@printf '%s\n' 'Conix Make API: <resource>[/<subject>]/<action>' \
	  'Docker operations require DOCKER_CONTEXT=<local context>; no implicit acquisition.' ''
	@awk -F ':[^#]*## ' '/^[a-z][a-z0-9/-]*:.*## / { printf "  %-36s %s\n", $$1, $$2 }' Makefile

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

image/indexer/check: ## Check installed ABI package; DOCKER_CONTEXT=<name> IMAGE=<sha256:id>
	$(RUN_OPERATOR)

image/evm-test-runner/check: ## Check EVM test image; DOCKER_CONTEXT=<name> IMAGE=<sha256:id>
	$(RUN_OPERATOR)

test/indexer/run: ## Run contained indexer/CLI tests against PostgreSQL and Anvil; DOCKER_CONTEXT=<name> RUNNER_IMAGE=<id>
	$(RUN_OPERATOR)

test/deployment/run: ## Qualify the committed deployment-acceptance profile through the ordinary named lifecycle
	$(RUN_OPERATOR)

test/input-staging/run: ## Prove confined input staging; INDEXER_IMAGE=<id> CONFIG_FILE=<path> DSN_FILE=<path>
	$(RUN_OPERATOR)

test/indexer/acceptance/run: ## Borrow dapp32 fixture; RUNNER_IMAGE=<id> FIXTURE_DESCRIPTOR=<private path>
	$(RUN_FIXTURE)

campaign/indexer/acceptance/run: ## Own dapp32 fixture; RUNNER_IMAGE=<id> DAPP32_DIR=<path>
	$(RUN_FIXTURE)

history/advance: ## Advance one block; INDEXER_IMAGE=<id> INPUT_VOLUME=<id> DATABASE_NETWORK=<name> RPC_NETWORK=<name>
	$(RUN_OPERATOR)

deployment/inputs/create: ## Stage private inputs; INDEXER_IMAGE=<id> CONFIG_FILE=<path> DSN_FILE=<path>
	$(RUN_OPERATOR)

deployment/inputs/remove: ## Remove selected input volume; INPUT_VOLUME=<owned volume>
	$(RUN_OPERATOR)

deployment/init: ## Create editable source skeleton and private ABI/credential directories; DEPLOYMENT=<name> DOCKER_CONTEXT=<name>
	$(RUN_OPERATOR)

deployment/prepare: ## Build offline and stage checked runtime inputs for saved DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/database/init: ## Run owner-only schema initialization once for prepared DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/config/check: ## Inspect prepared service configuration without creating resources; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/up: ## Start only the prepared, owner-initialized standalone service; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/status: ## Show selected existing deployment without requiring prepared image; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/logs: ## Show bounded logs from owned deployment; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

deployment/down: ## Retire owned indexer containers, keeping database/networks/inputs; DEPLOYMENT=<name>
	$(RUN_OPERATOR)

