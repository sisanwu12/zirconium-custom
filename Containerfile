FROM ghcr.io/zirconium-dev/zirconium:latest

# Keep the Nix store writable and persistent on bootc's read-only root.
RUN set -eux; \
    if [ -d /nix ] && [ ! -L /nix ]; then rmdir /nix; fi; \
    ln -s /var/nix /nix; \
    printf 'd /var/nix 0755 root root -\n' > /usr/lib/tmpfiles.d/nix.conf

ARG FLCLASH_VERSION=0.8.98
ARG FLCLASH_SHA256=aa14bf9c9b2a723b426b5875f1c1dea058f709ef0411547c134e9b0dee6e44ba

RUN set -eux; \
    rpm_file="/tmp/FlClash-${FLCLASH_VERSION}-linux-amd64.rpm"; \
    curl --fail --location --retry 3 \
      "https://github.com/chen08209/FlClash/releases/download/v${FLCLASH_VERSION}/FlClash-${FLCLASH_VERSION}-linux-amd64.rpm" \
      --output "${rpm_file}"; \
    echo "${FLCLASH_SHA256}  ${rpm_file}" | sha256sum --check; \
    dnf5 install -y "${rpm_file}"; \
    dnf5 clean all; \
    rm -f "${rpm_file}"
