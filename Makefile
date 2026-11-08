# Docker context must be chosen for each operation, not inherited from
# arbitrary DOCKER_HOST/DOCKER_CONTEXT/COMPOSE_* environment variables.
PYTHON ?= python3
_CONTEXT = --context "$(DOCKER_CONTEXT)" --origin "$(origin DOCKER_CONTEXT)"

.PHONY: image/build tests/image/build tests/container check/container
image/build:
	@$(PYTHON) tools/container.py $(_CONTEXT) image/build

tests/image/build:
	@$(PYTHON) tools/container.py $(_CONTEXT) tests/image/build

# No build/pull during a test. The caller supplies the prepared exact image ID.
tests/container:
	@test -n "$(CONIX_TEST_IMAGE)" || { echo "Set CONIX_TEST_IMAGE=sha256:<image-id> from tests/image/build." >&2; exit 2; }
	@$(PYTHON) tools/container.py $(_CONTEXT) tests/container --test-image "$(CONIX_TEST_IMAGE)"

check/container:
	@$(PYTHON) -m unittest discover -s repository_checks -p 'test_container_*.py' -v
