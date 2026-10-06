FROM ghcr.io/zirconium-dev/zirconium:latest

# Determinate Nix's OSTree planner mounts persistent /var/lib/nix here.
RUN mkdir -p /nix

ARG FLCLASH_VERSION=0.8.99
ARG FLCLASH_SHA256=09cb8eac7b3a6249026be56636c3b723cd0a795f5d7af358e7ef0be978182af3

RUN set -eux; \
    rpm_file="/tmp/FlClash-${FLCLASH_VERSION}-linux-amd64.rpm"; \
    curl --fail --location --retry 3 \
      "https://github.com/chen08209/FlClash/releases/download/v${FLCLASH_VERSION}/FlClash-${FLCLASH_VERSION}-linux-amd64.rpm" \
      --output "${rpm_file}"; \
    echo "${FLCLASH_SHA256}  ${rpm_file}" | sha256sum --check; \
    dnf5 install -y waydroid "${rpm_file}"; \
    dnf5 clean all; \
    rm -f "${rpm_file}"
