FROM rust:1.93.1-bookworm AS build

ARG RHWP_COMMIT=8d3bfa4b92174b16bac587fe1409975cf34ba566

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        git \
        libfontconfig1-dev \
        libfreetype6-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src \
    && git -C /src remote add origin https://github.com/edwardkim/rhwp.git \
    && git -C /src fetch --depth=1 origin "${RHWP_COMMIT}" \
    && git -C /src checkout --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = "${RHWP_COMMIT}"

WORKDIR /src
RUN cargo build --locked --release --features native-skia --bin rhwp

FROM debian:bookworm-slim

ARG RHWP_COMMIT=8d3bfa4b92174b16bac587fe1409975cf34ba566

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        fonts-dejavu-core \
        fonts-noto-cjk \
        libfontconfig1 \
        libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/target/release/rhwp /usr/local/bin/rhwp

LABEL org.opencontainers.image.source="https://github.com/edwardkim/rhwp" \
      org.opencontainers.image.revision="${RHWP_COMMIT}" \
      org.opencontainers.image.version="0.7.19"

ENTRYPOINT ["rhwp"]
