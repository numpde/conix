# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Runtime packaging is offline; the acquisition Dockerfile is separate.
ARG PYTHON_BASE_IMAGE
ARG WHEELHOUSE_IMAGE
ARG FOUNDRY_IMAGE=ghcr.io/foundry-rs/foundry:v1.7.1@sha256:8347b728d5d393dac1c018691b36f506d23b9dcd78341d40ea0fcb11c3a19cdd
FROM ${WHEELHOUSE_IMAGE} AS wheelhouse

FROM ${PYTHON_BASE_IMAGE} AS package
WORKDIR /build
COPY requirements/build.lock /build/requirements/build.lock
RUN --network=none --mount=type=bind,from=wheelhouse,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    --require-hashes --only-binary=:all: -r /build/requirements/build.lock
COPY pyproject.toml .
COPY src ./src
RUN --network=none python -m pip wheel --no-deps --no-build-isolation \
    --no-index --wheel-dir /dist .

FROM ${PYTHON_BASE_IMAGE} AS runtime
ENV HOME=/tmp PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1
COPY requirements/runtime.lock /build/requirements/runtime.lock
RUN --network=none --mount=type=bind,from=wheelhouse,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    --require-hashes --only-binary=:all: -r /build/requirements/runtime.lock
COPY --from=package /dist/conix-0.1.0-py3-none-any.whl /dist/conix-0.1.0-py3-none-any.whl
RUN --network=none python -m pip install --no-cache-dir --no-index \
    --no-deps /dist/conix-0.1.0-py3-none-any.whl \
    && python -m pip check \
    && rm -rf /dist /build
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["conix"]

FROM runtime AS tests
COPY tests /tests
ENTRYPOINT ["python", "-m", "unittest", "discover", "-s", "/tests", "-v"]

# Optional *test-only* Foundry signing tool; absent from runtime and ordinary tests.
# The deployed PayToPlay contract supplies voucherDigest. Cast signs that
# prehashed EIP-712 digest using only dapp32's public fixture key.
FROM ${FOUNDRY_IMAGE} AS fixture_signer
FROM runtime AS fixture
COPY --from=fixture_signer /usr/local/bin/cast /usr/local/bin/cast
COPY tests /tests
RUN --network=none cast --version
ENTRYPOINT ["python", "-I", "/tests/fixture_campaign.py"]
