# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Runtime packaging is offline; shared-pool acquisition is a separate operation.
ARG PYTHON_BASE_IMAGE
ARG FOUNDRY_IMAGE=ghcr.io/foundry-rs/foundry:v1.7.1@sha256:8347b728d5d393dac1c018691b36f506d23b9dcd78341d40ea0fcb11c3a19cdd

# The curated BuildKit context contains exact hash-verified wheels streamed
# from the NMR shared WHEELHOUSE. Only the RUN mounts see those files; no
# stage COPYs the wheel pool into a persistent image layer.
FROM ${PYTHON_BASE_IMAGE} AS package
WORKDIR /build
COPY requirements/build.lock /build/requirements/build.lock
RUN --network=none --mount=type=bind,source=wheelhouse,target=/wheels,readonly \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    --require-hashes --only-binary=:all: -r /build/requirements/build.lock
COPY pyproject.toml .
COPY src ./src
RUN --network=none python -m pip wheel --no-deps --no-build-isolation \
    --no-index --wheel-dir /dist .

FROM ${PYTHON_BASE_IMAGE} AS indexer
LABEL io.conix.image-role=indexer
ENV HOME=/tmp PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1
RUN --network=none \
    --mount=type=bind,source=requirements/runtime.lock,target=/tmp/conix-runtime.lock,readonly \
    --mount=type=bind,source=wheelhouse,target=/wheels,readonly \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    --require-hashes --only-binary=:all: -r /tmp/conix-runtime.lock
RUN --network=none \
    --mount=type=bind,from=package,source=/dist/conix-0.1.0-py3-none-any.whl,target=/tmp/conix-0.1.0-py3-none-any.whl,readonly \
    python -m pip install --no-cache-dir --no-index \
    --no-deps /tmp/conix-0.1.0-py3-none-any.whl \
    && python -m pip check
USER 65532:65532
WORKDIR /tmp
# The foreground worker is an explicit module invocation.

FROM indexer AS test-runner
LABEL io.conix.image-role=test-runner
COPY tests /tests
COPY tools/fixtures/protocol.py /tests/fixture_protocol.py
ENTRYPOINT ["python", "-m", "unittest", "discover", "-s", "/tests", "-v"]

# Optional *test-only* Foundry signing tool; absent from runtime and ordinary tests.
# The deployed PayToPlay contract supplies voucherDigest. Cast signs that
# prehashed EIP-712 digest using only dapp32's public fixture key.
FROM ${FOUNDRY_IMAGE} AS fixture_signer
FROM indexer AS evm-test-runner
LABEL io.conix.image-role=evm-test-runner
COPY --from=fixture_signer /usr/local/bin/cast /usr/local/bin/cast
COPY tests /tests
COPY tools/fixtures/protocol.py /tests/fixture_protocol.py
RUN --network=none cast --version
ENTRYPOINT ["python", "-I", "/tests/generic_acceptance.py"]
