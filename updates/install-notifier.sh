#!/usr/bin/env bash
# Bootstrap notifications on the currently installed image, without waiting for a new OS.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install -Dm755 "$script_dir/notify.py" "$HOME/.local/lib/zirconium-custom/notify.py"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/zirconium-custom-update-notify.service" <<'EOF'
[Unit]
Description=Notify about aged Zirconium Custom updates (no installation)

[Service]
Type=oneshot
ExecStart=/usr/bin/bash -c 'if test -x /usr/bin/zirconium-custom-update-check; then exec /usr/bin/zirconium-custom-update-check --notify; else exec /usr/bin/python3 "%h/.local/lib/zirconium-custom/notify.py" --notify; fi'
TimeoutStartSec=90
EOF
install -m644 "$script_dir/systemd/zirconium-custom-update-notify.timer" \
  "$HOME/.config/systemd/user/zirconium-custom-update-notify.timer"
systemctl --user daemon-reload
systemctl --user enable --now zirconium-custom-update-notify.timer
printf '%s\n' '通知已启用；不会自动安装或重启。首次成功构建后，候选镜像需要等待 14 天。'
