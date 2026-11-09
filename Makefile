# Stable resource/action operator API. Do not interpolate operator values into recipes.
# Make's $(value) captures command-line input literally; Python validates it.
override SHELL := /bin/sh
.DEFAULT_GOAL := help

# Never export GNU Make's recursively expanded command-line variables.
unexport DOCKER_CONTEXT WHEELHOUSE IMAGE RUNNER_IMAGE INDEXER_IMAGE DAPP32_DIR FIXTURE_DESCRIPTOR
unexport CONFIG_FILE DSN_FILE OWNER_DSN_FILE INPUT_VOLUME DEPLOYMENT DATABASE_NETWORK RPC_NETWORK
override export CONIX_MAKE_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
override export CONIX_MAKE_WHEELHOUSE := $(value WHEELHOUSE)
override export CONIX_MAKE_IMAGE := $(value IMAGE)
override export CONIX_MAKE_RUNNER_IMAGE := $(value RUNNER_IMAGE)
override export CONIX_MAKE_INDEXER_IMAGE := $(value INDEXER_IMAGE)
override export CONIX_MAKE_DAPP32_DIR := $(value DAPP32_DIR)
override export CONIX_MAKE_FIXTURE_DESCRIPTOR := $(value FIXTURE_DESCRIPTOR)
override export CONIX_MAKE_CONFIG_FILE := $(value CONFIG_FILE)
override export CONIX_MAKE_DSN_FILE := $(value DSN_FILE)
override export CONIX_MAKE_OWNER_DSN_FILE := $(value OWNER_DSN_FILE)
override export CONIX_MAKE_INPUT_VOLUME := $(value INPUT_VOLUME)
override export CONIX_MAKE_DEPLOYMENT := $(value DEPLOYMENT)
override export CONIX_MAKE_DATABASE_NETWORK := $(value DATABASE_NETWORK)
override export CONIX_MAKE_RPC_NETWORK := $(value RPC_NETWORK)

.PHONY: help test/repository/run repository/inputs/check \
 image/python-base/acquire image/postgres/acquire image/dockerfile-frontend/acquire image/foundry/acquire \
 wheelhouse/check wheelhouse/acquire \
 image/indexer/build image/test-runner/build image/evm-test-runner/build \
 image/indexer/check image/evm-test-runner/check \
 test/postgres/run test/input-staging/run test/pay-to-play/run campaign/pay-to-play/run \
 deployment/inputs/create deployment/inputs/remove database/schema/initialize \
 deployment/config/check deployment/apply deployment/status deployment/remove

help:
	@printf '%s\n' \
	  'Conix Make API: <resource>[/<role-or-subject>]/<action> (no implicit acquisition)' \
	  '  test/repository/run                         Host repository checks; no Docker' \
	  '  repository/inputs/check                     Offline lock and build-context checks' \
	  '  image/python-base/acquire                   Pull reviewed Python base' \
	  '  image/postgres/acquire                      Pull reviewed PostgreSQL base' \
	  '  image/dockerfile-frontend/acquire           Pull reviewed BuildKit frontend' \
	  '  image/foundry/acquire                       Pull reviewed test-only Foundry' \
	  '  wheelhouse/check|acquire                    DOCKER_CONTEXT=agentic WHEELHOUSE=/absolute/pool' \
	  '  image/indexer/build                         DOCKER_CONTEXT=agentic WHEELHOUSE=/absolute/pool' \
	  '  image/test-runner/build                     DOCKER_CONTEXT=agentic WHEELHOUSE=/absolute/pool' \
	  '  image/evm-test-runner/build                 DOCKER_CONTEXT=agentic WHEELHOUSE=/absolute/pool' \
	  '  image/indexer/check                         DOCKER_CONTEXT=agentic IMAGE=sha256:<id>' \
	  '  image/evm-test-runner/check                 DOCKER_CONTEXT=agentic IMAGE=sha256:<id>' \
	  '  test/postgres/run                           DOCKER_CONTEXT=agentic RUNNER_IMAGE=sha256:<id>' \
	  '  test/input-staging/run                      DOCKER_CONTEXT=agentic INDEXER_IMAGE=sha256:<id>' \
	  '                                                CONFIG_FILE=/absolute/config.toml DSN_FILE=/absolute/secret' \
	  '  test/pay-to-play/run                        DOCKER_CONTEXT=agentic RUNNER_IMAGE=sha256:<id>' \
	  '                                                FIXTURE_DESCRIPTOR=/absolute/private/fixture.json' \
	  '  campaign/pay-to-play/run                    DOCKER_CONTEXT=agentic RUNNER_IMAGE=sha256:<id>' \
	  '                                                DAPP32_DIR=/absolute/dapp32' \
	  '  deployment/inputs/create                    DOCKER_CONTEXT=agentic INDEXER_IMAGE=sha256:<id>' \
	  '                                                CONFIG_FILE=/absolute/config.toml DSN_FILE=/absolute/secret' \
	  '  deployment/inputs/remove                    DOCKER_CONTEXT=agentic INPUT_VOLUME=<owned-volume>' \
	  '  database/schema/initialize                  DOCKER_CONTEXT=agentic INDEXER_IMAGE=sha256:<id>' \
	  '                                                CONFIG_FILE=/absolute/config.toml OWNER_DSN_FILE=/absolute/secret' \
	  '                                                DATABASE_NETWORK=<selected-network>' \
	  '  deployment/config/check|apply|status|remove DOCKER_CONTEXT=agentic DEPLOYMENT=<name>' \
	  '                                                INDEXER_IMAGE=sha256:<id> INPUT_VOLUME=<volume>' \
	  '                                                DATABASE_NETWORK=<name> RPC_NETWORK=<name>'

test/repository/run:
	@python3 -m unittest discover -s repository_checks -p 'test_*.py' -v

repository/inputs/check:
	@python3 -m unittest discover -s repository_checks -p 'test_input_policy.py' -v

# Exactly one host operator owns Docker selection and each container lifecycle.
image/python-base/acquire image/postgres/acquire image/dockerfile-frontend/acquire image/foundry/acquire:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

wheelhouse/check wheelhouse/acquire:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

image/indexer/build image/test-runner/build image/evm-test-runner/build:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

image/indexer/check image/evm-test-runner/check:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

test/postgres/run test/input-staging/run:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

deployment/inputs/create deployment/inputs/remove database/schema/initialize:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

deployment/config/check deployment/apply deployment/status deployment/remove:
	@python3 -m tools.containers.operator --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

# The borrowed test never invokes dapp32's fixture lifecycle operations.
test/pay-to-play/run campaign/pay-to-play/run:
	@python3 -m tools.fixtures.pay_to_play --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"
