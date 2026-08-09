# syntax=docker/dockerfile:1.7
#
# PostgreSQL + pg_parquet.
#
# CrunchyData publishes no prebuilt binaries or images for pg_parquet, so stage 1
# builds the extension from a pinned source tag with cargo-pgrx and stage 2 copies
# only the resulting .so / .control / .sql into a clean postgres image.

ARG PG_MAJOR=17
ARG PG_PARQUET_VERSION=0.5.1
ARG PGRX_VERSION=0.16.0

# --------------------------------------------------------------------------- #
# Stage 1 - build pg_parquet
# --------------------------------------------------------------------------- #
FROM postgres:${PG_MAJOR}-bookworm AS builder

ARG PG_MAJOR
ARG PG_PARQUET_VERSION
ARG PGRX_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        clang \
        curl \
        git \
        libclang-dev \
        libssl-dev \
        pkg-config \
        "postgresql-server-dev-${PG_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSfL https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable

RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    cargo install cargo-pgrx --version "${PGRX_VERSION}" --locked

# Reuse the PostgreSQL already present in this image instead of letting pgrx
# download and compile its own copy.
RUN cargo pgrx init "--pg${PG_MAJOR}" "$(command -v pg_config)"

WORKDIR /src
RUN git clone --depth 1 --branch "v${PG_PARQUET_VERSION}" \
        https://github.com/CrunchyData/pg_parquet.git .

RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/src/target,sharing=locked \
    cargo pgrx install \
        --release \
        --no-default-features \
        --features "pg${PG_MAJOR}" \
        --pg-config "$(command -v pg_config)"

# --------------------------------------------------------------------------- #
# Stage 2 - runtime
# --------------------------------------------------------------------------- #
FROM postgres:${PG_MAJOR}-bookworm AS runtime

ARG PG_MAJOR
ARG PG_PARQUET_VERSION

LABEL org.opencontainers.image.title="postgres-pg-parquet" \
      org.opencontainers.image.description="PostgreSQL ${PG_MAJOR} with the pg_parquet extension" \
      org.opencontainers.image.source="https://github.com/CrunchyData/pg_parquet" \
      org.opencontainers.image.version="${PG_PARQUET_VERSION}"

COPY --from=builder \
    /usr/lib/postgresql/${PG_MAJOR}/lib/pg_parquet.so \
    /usr/lib/postgresql/${PG_MAJOR}/lib/
COPY --from=builder \
    /usr/share/postgresql/${PG_MAJOR}/extension/pg_parquet.control \
    /usr/share/postgresql/${PG_MAJOR}/extension/pg_parquet--${PG_PARQUET_VERSION}.sql \
    /usr/share/postgresql/${PG_MAJOR}/extension/

# pg_parquet must be preloaded; docker-compose passes the same flag explicitly so
# the setting survives a `command:` override.
RUN printf "shared_preload_libraries = 'pg_parquet'\n" \
    > /usr/share/postgresql/postgresql.conf.sample.pg_parquet \
    && cat /usr/share/postgresql/postgresql.conf.sample.pg_parquet \
    >> /usr/share/postgresql/postgresql.conf.sample \
    && rm /usr/share/postgresql/postgresql.conf.sample.pg_parquet

# Default target for `COPY ... TO '/parquet/...'`; bind-mounted from the host.
RUN mkdir -p /parquet && chown postgres:postgres /parquet
