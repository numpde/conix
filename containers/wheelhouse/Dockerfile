# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# ONLY acquisition stage: deliberately network-enabled; hashes are mandatory.
ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}
WORKDIR /requirements
COPY requirements/runtime.lock requirements/build.lock ./
RUN python -m pip download --disable-pip-version-check --no-cache-dir \
    --require-hashes --only-binary=:all: --dest /wheels \
    -r runtime.lock -r build.lock
