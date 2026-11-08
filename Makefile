# All operator-supplied parameters cross Make through raw environment values.
# Never interpolate them into recipe shell text: quoting is not a defense
# against $(shell ...) or shell command substitution.
override SHELL := /bin/sh
.DEFAULT_GOAL := help

# GNU Make automatically exports command-line variables and may recursively
# expand $(shell ...) in their values while constructing the child environment.
# Suppress that path; only the simple $(value)-captured copies are exported.
unexport DOCKER_CONTEXT WHEELHOUSE_IMAGE CONIX_IMAGE CONIX_TEST_IMAGE CONIX_FIXTURE_IMAGE DAPP32_DIR
unexport CONIX_CONFIG_FILE CONIX_DSN_FILE CONIX_OWNER_DSN_FILE CONIX_INPUT_VOLUME
unexport CONIX_DEPLOYMENT CONIX_DATABASE_NETWORK CONIX_RPC_NETWORK

override export CONIX_MAKE_DOCKER_CONTEXT := $(value DOCKER_CONTEXT)
override export CONIX_MAKE_WHEELHOUSE_IMAGE := $(value WHEELHOUSE_IMAGE)
override export CONIX_MAKE_CONIX_IMAGE := $(value CONIX_IMAGE)
override export CONIX_MAKE_CONIX_TEST_IMAGE := $(value CONIX_TEST_IMAGE)
override export CONIX_MAKE_CONIX_FIXTURE_IMAGE := $(value CONIX_FIXTURE_IMAGE)
override export CONIX_MAKE_DAPP32_DIR := $(value DAPP32_DIR)
override export CONIX_MAKE_CONIX_CONFIG_FILE := $(value CONIX_CONFIG_FILE)
override export CONIX_MAKE_CONIX_DSN_FILE := $(value CONIX_DSN_FILE)
override export CONIX_MAKE_CONIX_OWNER_DSN_FILE := $(value CONIX_OWNER_DSN_FILE)
override export CONIX_MAKE_CONIX_INPUT_VOLUME := $(value CONIX_INPUT_VOLUME)
override export CONIX_MAKE_CONIX_DEPLOYMENT := $(value CONIX_DEPLOYMENT)
override export CONIX_MAKE_CONIX_DATABASE_NETWORK := $(value CONIX_DATABASE_NETWORK)
override export CONIX_MAKE_CONIX_RPC_NETWORK := $(value CONIX_RPC_NETWORK)

.PHONY: help check/container check/inputs deps/images/prepare deps/fixture-image/prepare deps/wheelhouse/build \
	image/build tests/image/build tests/fixture/image/build image/smoke tests/container tests/fixture \
	inputs/stage inputs/proof inputs/remove schema/init runtime/start runtime/stop runtime/status

help:
	@printf '%s\n' \
	  'Conix container commands (no implicit downloads, builds or Docker operations):' \
	  '  make check/container                                     Read-only host repository checks' \
  '  make check/inputs                                        Offline locks/build-context admission' \
	  '  make deps/images/prepare DOCKER_CONTEXT=<local-context>   Explicit networked image acquisition' \
	  '  make deps/wheelhouse/build DOCKER_CONTEXT=<context>       Networked locked wheel acquisition' \
  '  make deps/fixture-image/prepare DOCKER_CONTEXT=<context> Acquire reviewed optional Cast image' \
	  '  make image/build DOCKER_CONTEXT=<context> WHEELHOUSE_IMAGE=sha256:<id>' \
	  '  make tests/image/build DOCKER_CONTEXT=<context> WHEELHOUSE_IMAGE=sha256:<id>' \
	  '  make tests/container DOCKER_CONTEXT=<context> CONIX_TEST_IMAGE=sha256:<id>' \
  '  make tests/fixture/image/build DOCKER_CONTEXT=<context> WHEELHOUSE_IMAGE=sha256:<id>' \
  '  make tests/fixture DOCKER_CONTEXT=<context> DAPP32_DIR=/path/to/dapp32 CONIX_FIXTURE_IMAGE=sha256:<id>' \
	  '  make image/smoke DOCKER_CONTEXT=<context> CONIX_IMAGE=sha256:<id>' \
	  '  make inputs/stage|inputs/proof DOCKER_CONTEXT=<context> CONIX_IMAGE=sha256:<id>' \
	  '        CONIX_CONFIG_FILE=/path/to/config CONIX_DSN_FILE=/path/to/credentials' \
	  '  make inputs/remove DOCKER_CONTEXT=<context> CONIX_INPUT_VOLUME=<id>' \
  '  make schema/init DOCKER_CONTEXT=<context> CONIX_IMAGE=sha256:<id>' \
  '        CONIX_CONFIG_FILE=/path/to/config CONIX_OWNER_DSN_FILE=/path/to/owner-secret' \
  '        CONIX_DATABASE_NETWORK=<name>' \
	  '  make runtime/start|runtime/status|runtime/stop DOCKER_CONTEXT=<context>' \
	  '        CONIX_DEPLOYMENT=<name> CONIX_IMAGE=sha256:<id> CONIX_INPUT_VOLUME=<id>' \
	  '        CONIX_DATABASE_NETWORK=<name> CONIX_RPC_NETWORK=<name>'

check/container:
	@python3 -m unittest discover -s repository_checks -p 'test_*.py' -v

check/inputs:
	@python3 -m unittest discover -s repository_checks -p 'test_input_policy.py' -v

# Only preparation lanes may acquire images or wheels. Python validates all
# raw inputs after Make starts it; no user-controlled value enters shell code.
deps/images/prepare:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" deps/images/prepare

deps/wheelhouse/build:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" deps/wheelhouse/build

image/build:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" image/build

tests/image/build:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" tests/image/build

image/smoke:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" image/smoke

tests/container:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" tests/container

inputs/stage:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" inputs/stage

inputs/proof:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" inputs/proof

inputs/remove:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" inputs/remove

runtime/start runtime/status runtime/stop:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" "$@"

# Owner credentials are staged in a disposable engine volume and never enter
# the persistent runtime worker's configuration volume.
schema/init:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" schema/init

# Optional integration image only. The normal runtime image has no signing tool.
deps/fixture-image/prepare:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" deps/fixture-image/prepare

tests/fixture/image/build:
	@python3 tools/container.py --from-make --origin "$(origin DOCKER_CONTEXT)" tests/fixture/image/build

# The host orchestrator owns the sibling fixture; no Conix worker mounts a
# Docker socket, contract source checkout, or test signing account keys.
tests/fixture:
	@python3 tools/fixture.py --from-make --origin "$(origin DOCKER_CONTEXT)"
