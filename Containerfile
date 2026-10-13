FROM ghcr.io/zirconium-dev/zirconium:latest

# Determinate Nix's OSTree planner mounts persistent /var/lib/nix here.
RUN mkdir -p /nix

# Complete the host virtualization stack; the base image provides KVM modules.
RUN set -eux; \
    dnf5 install -y \
      qemu-kvm \
      libvirt-daemon-kvm \
      libvirt-daemon-config-network \
      libvirt-client \
      virt-install \
      virt-manager \
      virt-viewer \
      edk2-ovmf \
      swtpm \
      swtpm-tools \
      virtiofsd; \
    dnf5 clean all; \
    printf '%s\n' \
      'd /var/lib/libvirt/images 0711 root root -' \
      'd /var/lib/swtpm-localca 0750 tss root -' \
      'd /var/log/libvirt 0700 root root -' \
      'd /var/log/libvirt/qemu 0700 root root -' \
      'Z /var/log/libvirt - - - -' \
      > /usr/lib/tmpfiles.d/zirconium-libvirt.conf; \
    systemctl enable virtqemud.service; \
    for driver in qemu network nodedev nwfilter secret storage; do \
      systemctl enable "virt${driver}d.socket"; \
    done; \
    systemctl enable virtlogd.socket virtlockd.socket

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

# Exercise first-boot TPM provisioning without persisting test keys in the image.
RUN --mount=type=tmpfs,target=/var --mount=type=tmpfs,target=/tmp --network=none \
    set -eux; \
    chmod 1777 /tmp; \
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/zirconium-libvirt.conf; \
    test -d /var/log/libvirt/qemu; \
    install -d -m 0700 -o tss -g tss /tmp/vtpm-check; \
    runuser -u tss -- swtpm_setup --tpm2 --tpmstate /tmp/vtpm-check \
      --create-ek-cert --create-platform-cert --lock-nvram --pcr-banks sha256

# Package scriptlets create runtime state that must not be shipped in bootc images.
RUN rm -rf /run/dnf /run/gluster /run/selinux-policy /tmp/* /var/* && \
    mkdir -p /var/tmp && \
    bootc container lint
