FROM python:3.12.9-slim-bookworm AS wheel
WORKDIR /build
COPY pyproject.toml .
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /dist .

FROM python:3.12.9-slim-bookworm AS runtime
COPY --from=wheel /dist /dist
RUN python -m pip install --no-cache-dir /dist/conix-0.1.0-py3-none-any.whl \
    && rm -rf /dist
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["conix"]

FROM runtime AS tests
COPY tests /tests
ENTRYPOINT ["python", "-m", "unittest", "discover", "-s", "/tests", "-v"]
