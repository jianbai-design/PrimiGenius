FROM 016a941a68d52b737edc08295fa6b5ee65d7b3e292e8c801236307bc02275468
LABEL org.primigenius.r-version="4.6.1"
LABEL org.primigenius.bioconductor-version="3.23"
LABEL org.primigenius.base-digest="sha256:18aac8c3b1fece4c60720f5f9b0dc066e04409c7de2896b28528e7be0bdd9535"
LABEL org.primigenius.recipe-version="6"
LABEL org.primigenius.recipe-sha256="d029da6d09321aca1b701e3c9e327297ac38f6d4df8d7c15a99824893ce9b02e"
ARG APT_MIRROR=https://archive.ubuntu.com/ubuntu
RUN set -eu; \
    echo "[APT] Using ${APT_MIRROR}"; \
    sed -i -E "s#https?://(archive\.ubuntu\.com|security\.ubuntu\.com)/ubuntu/?#${APT_MIRROR}/#g" /etc/apt/sources.list.d/ubuntu.sources; \
    echo "[APT] Refreshing Ubuntu package indexes..."; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Languages=none -o Dpkg::Use-Pty=0 update; \
    echo "[APT] Installing R system libraries..."; \
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Dpkg::Use-Pty=0 install -y --no-install-recommends --no-upgrade \
        locales \
        ca-certificates \
        build-essential \
        gfortran \
        make \
        pkg-config \
        cmake \
        rustc-1.89 \
        cargo-1.89 \
        libuv1-dev \
        libgdal-dev \
        libproj-dev \
        libgeos-dev \
        libudunits2-dev \
        libharfbuzz-dev \
        libfribidi-dev \
        libfontconfig1-dev \
        libfreetype-dev \
        libpng-dev \
        libtiff-dev \
        libjpeg-dev \
        libcairo2-dev \
        libxt-dev \
        libtcl8.6 \
        libtk8.6 \
        tcl8.6-dev \
        tk8.6-dev \
        tcl-dev \
        tk-dev \
        libcurl4-openssl-dev \
        libxml2-dev \
        libssl-dev \
        libgit2-dev \
        zlib1g-dev \
        libbz2-dev \
        liblzma-dev \
        libsqlite3-dev \
    && \
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen en_US.UTF-8 && \
    ln -sf /usr/bin/rustc-1.89 /usr/local/bin/rustc && ln -sf /usr/bin/cargo-1.89 /usr/local/bin/cargo || exit 1; rustc --version && cargo --version && dpkg --compare-versions "$(rustc --version | cut -d ' ' -f2)" ge 1.88.0 && dpkg --compare-versions "$(cargo --version | cut -d ' ' -f2)" ge 1.88.0 || { echo "[SYS-DEPS] Rust/Cargo >= 1.88.0 required; R installation stopped." >&2; exit 1; };  \
    apt-get clean && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
