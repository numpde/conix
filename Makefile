.PHONY: image/build tests/container clean/tests

image/build:
	docker build --target runtime -t conix:tier0 .

tests/container:
	@docker compose -f compose/tests.yml -p conix-tier0-tests up --build --abort-on-container-exit --exit-code-from tests; \
	result=$$?; docker compose -f compose/tests.yml -p conix-tier0-tests down --volumes --remove-orphans; exit $$result

clean/tests:
	docker compose -f compose/tests.yml -p conix-tier0-tests down --volumes --remove-orphans
