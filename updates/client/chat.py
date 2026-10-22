"""Existing chat Distrobox: APT plus individually selected vendor DEBs."""
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.error

from . import chat_sources as source
from .core import Change, capture, download, live, say, detail


APT = ["apt-get", "-y", "--no-remove", "-o", "Dpkg::Options::=--force-confold",
       "-o", "Dpkg::Progress-Fancy=1"]


def container_command(args):
    return ["podman", "exec", *(["--tty"] if sys.stdout.isatty() else []), "--user", "0", "--env", "LC_ALL=C",
            "--env", "DEBIAN_FRONTEND=noninteractive", "chat", *args]


class Chat:
    key = "chat"

    def __init__(self, selected=None, explicit=False):
        self.selected = selected
        self.explicit = explicit
        if selected and len(selected) == 1:
            self.key = "chat:" + selected[0]

    def available(self):
        if not shutil.which("podman"):
            return False
        return "chat" in capture(["podman", "ps", "-a", "--format", "{{.Names}}"]).splitlines()

    def plan(self):
        source.ensure_container()
        installed = source.installed_packages()
        changes = []
        if self.selected is None or "apt" in self.selected:
            detail("  chat: 刷新 APT 元数据（不安装软件）")
            source.refresh_apt()
            rows = source.apt_updates()
            if rows:
                changes.append(Change("chat:apt", "已安装", f"{len(rows)} 个包",
                                      [f"{name}: {old} → {new}" for name, old, new in rows],
                                      {"packages": [f"{name}={new}" for name, _, new in rows],
                                       "rows": rows}))
        for package, label in source.PACKAGES.items():
            if self.selected is not None and package not in self.selected:
                continue
            if package not in installed:
                if self.explicit:
                    raise ValueError(f"chat:{package} 未安装")
                continue
            latest = source.official_release(package)
            detail(f"  {label}: 已安装 {installed[package]}；官方 {latest['Version']}")
            if source.is_newer(installed[package], latest["Version"]):
                changes.append(Change("chat:" + package, installed[package], latest["Version"],
                                      [label, "官方 DEB；APT 将验证并安装必要依赖"], latest))
        return changes

    def apply(self, change):
        if change.key == "chat:apt":
            # Re-simulate the exact pinned package set; fail if the dependency plan drifted.
            output = source.in_chat("apt-get", "-s", "--no-remove", "install", *change.data["packages"])
            rows = [(n, o or "未安装", v) for n, o, v in
                    re.findall(r"^Inst (\S+) (?:\[([^]]+)\] )?\((\S+)", output, re.MULTILINE)]
            if sorted(rows) != sorted(change.data["rows"]):
                raise RuntimeError("APT 依赖计划已改变，请重新检查")
            live(container_command([*APT, "install", *change.data["packages"]]))
            return
        package = change.key.split(":", 1)[1]
        current = source.installed_packages().get(package)
        if current == change.new:
            say(f"  {package} 已由 APT 更新至目标版本")
            return
        if current != change.old:
            raise RuntimeError("容器软件版本在预览后改变，请重新检查")
        with tempfile.TemporaryDirectory(prefix="update-chat-") as directory:
            path = Path(directory) / (package + ".deb")
            if package == "linuxqq":
                # The user may spend a long time reviewing or updating other applications.
                # Renew authorization for the same installer immediately before downloading.
                for attempt in range(2):
                    url = source.qq_signed_url(change.data["source_url"])
                    try:
                        download(url, path, change.key)
                        break
                    except urllib.error.HTTPError as error:
                        error.close()
                        if error.code != 403 or attempt:
                            raise
                        detail("  QQ 下载签名失效，重新获取后重试一次")
            else:
                download(change.data["url"], path, change.key)
            source.validate_download(path, package, change.new, change.data.get("md5"))
            container_tmp = source.in_chat("mktemp", "-d", "/var/tmp/opt-updater.XXXXXX")
            if not re.fullmatch(r"/var/tmp/opt-updater\.[a-zA-Z0-9]+", container_tmp):
                raise ValueError("容器临时路径无效")
            destination = container_tmp + "/" + path.name
            try:
                live(["podman", "cp", str(path), "chat:" + destination])
                live(container_command([*APT, "install", destination]))
                if source.installed_packages().get(package) != change.new:
                    raise RuntimeError("安装后版本与计划不符")
                say(f"  {change.key} 安装：100%")
            finally:
                source.in_chat("rm", "-rf", "--", container_tmp)
