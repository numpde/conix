# The Docker operator selects one local Engine per operation; no ambient
# Docker/Compose endpoint, credentials or project files influence that choice.
PYTHON ?= python3
_CONTEXT = --context "$(DOCKER_CONTEXT)" --origin "$(origin DOCKER_CONTEXT)"

.PHONY: deps/images/prepare deps/wheelhouse/build image/build image/smoke tests/image/build tests/container check/container

# Only these preparation lanes can request network acquisition.
deps/images/prepare:
	@$(PYTHON) tools/container.py $(_CONTEXT) deps/images/prepare

deps/wheelhouse/build:
	@$(PYTHON) tools/container.py $(_CONTEXT) deps/wheelhouse/build

# Exact wheelhouse ID is required; packaging and every pip RUN are offline.
image/build:
	@test -n "$(WHEELHOUSE_IMAGE)" || { echo "Supply WHEELHOUSE_IMAGE=sha256:<id> from deps/wheelhouse/build." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) image/build --wheelhouse-image "$(WHEELHOUSE_IMAGE)"

tests/image/build:
	@test -n "$(WHEELHOUSE_IMAGE)" || { echo "Supply WHEELHOUSE_IMAGE=sha256:<id> from deps/wheelhouse/build." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) tests/image/build --wheelhouse-image "$(WHEELHOUSE_IMAGE)"

image/smoke:
	@test -n "$(CONIX_IMAGE)" || { echo "Supply CONIX_IMAGE=sha256:<id> from image/build." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) image/smoke --image "$(CONIX_IMAGE)"

# Tests use an already built immutable test image and local pinned PostgreSQL.
tests/container:
	@test -n "$(CONIX_TEST_IMAGE)" || { echo "Supply CONIX_TEST_IMAGE=sha256:<id> from tests/image/build." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) tests/container --test-image "$(CONIX_TEST_IMAGE)"

check/container:
	@$(PYTHON) -m unittest discover -s repository_checks -p 'test_container_*.py' -v


.PHONY: inputs/stage inputs/proof inputs/remove
# This stages fixed inputs to an engine-owned volume. No host 0600 bind-mount
# access assumptions and no secret argv/env arguments.
inputs/stage:
	@test -n "$(CONIX_IMAGE)" -a -n "$(CONIX_CONFIG_FILE)" -a -n "$(CONIX_DSN_FILE)" || { echo "Supply CONIX_IMAGE, CONIX_CONFIG_FILE and CONIX_DSN_FILE explicitly." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) inputs/stage --image "$(CONIX_IMAGE)" --config-file "$(CONIX_CONFIG_FILE)" --dsn-file "$(CONIX_DSN_FILE)"

inputs/proof:
	@test -n "$(CONIX_IMAGE)" -a -n "$(CONIX_CONFIG_FILE)" -a -n "$(CONIX_DSN_FILE)" || { echo "Supply CONIX_IMAGE, CONIX_CONFIG_FILE and CONIX_DSN_FILE explicitly." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) inputs/proof --image "$(CONIX_IMAGE)" --config-file "$(CONIX_CONFIG_FILE)" --dsn-file "$(CONIX_DSN_FILE)"

inputs/remove:
	@test -n "$(CONIX_INPUT_VOLUME)" || { echo "Supply exact CONIX_INPUT_VOLUME=conix-inputs-<token>." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) inputs/remove --volume "$(CONIX_INPUT_VOLUME)"
