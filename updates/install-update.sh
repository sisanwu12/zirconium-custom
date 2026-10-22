#!/usr/bin/env bash
# Install only the updater, never install application or OS updates.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ${EUID} -eq 0 ]]; then
  echo '请以普通用户安装 update。' >&2
  exit 1
fi
prefix="${UPDATE_INSTALL_PREFIX:-$HOME/.local}"
mkdir -p "$prefix/lib/zirconium-updater" "$prefix/bin"
release="$(mktemp -d "$prefix/lib/zirconium-updater/release.XXXXXXXX")"
trap 'rm -rf -- "$release"' ERR
cp -R "$script_dir/client" "$release/client"
install -m644 "$script_dir/notify.py" "$release/notify.py"
install -m755 "$script_dir/update" "$release/update"
# Keep exact originals, including symlinks, before replacing either old entry point.
for name in update opt-updater; do
  if [[ -e "$prefix/bin/$name" || -L "$prefix/bin/$name" ]]; then
    if [[ ! -e "$prefix/bin/$name.before-unified-update" && ! -L "$prefix/bin/$name.before-unified-update" ]]; then
      cp -P "$prefix/bin/$name" "$prefix/bin/$name.before-unified-update"
    fi
  fi
done
ln -s "$release" "$prefix/lib/zirconium-updater/current.new"
mv -Tf "$prefix/lib/zirconium-updater/current.new" "$prefix/lib/zirconium-updater/current"
for name in update opt-updater; do
  ln -s "$prefix/lib/zirconium-updater/current/update" "$prefix/bin/$name.new"
  mv -Tf "$prefix/bin/$name.new" "$prefix/bin/$name"
done
printf '%s\n' "已安装：$prefix/bin/update" \
  '旧入口备份为 *.before-unified-update；没有安装任何软件更新或系统更新。' \
  '运行 update --check 查看变更，运行 update 预览后交互确认。'
