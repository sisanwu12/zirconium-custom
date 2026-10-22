"""Home Manager snapshots and Nix runtime updates are separate transactions."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .core import Change, capture, config_home, live, say, versions, detail
from .nix_progress import run_nix


NIX = ["nix", "--extra-experimental-features", "nix-command flakes"]


def tree_hash(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if ".git" in path.relative_to(directory).parts or not path.is_file():
            continue
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def store_packages(paths):
    result = {}
    for path in paths:
        name = Path(path).name.split("-", 1)[-1]
        if name == "home-manager-path" or name.startswith("dummy-"):
            continue
        match = re.match(r"(.+?)-(\d.*)$", name)
        key, version = match.groups() if match else (name, "构建变化")
        version = re.sub(r"-(bin|man|dev|doc|info|terminfo|lib|out)$", "", version)
        previous = result.get(key, (version, ()))
        result[key] = (version, tuple(sorted(set(previous[1]) | {path})))
    return result


class HomeManager:
    key = "home-manager"

    def __init__(self, workspace, config):
        self.directory = Path(config.get("home_manager_dir", config_home() / "home-manager")).expanduser()
        self.output = config.get("home_manager_output", "bootstrap")
        self.snapshot = workspace / "home-manager"
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.profile = state / "nix/profiles/home-manager"
        self.state = state / "zirconium-updater"

    def available(self):
        return bool(shutil.which("nix")) and (self.directory / "flake.nix").is_file()

    def plan(self):
        if not self.profile.exists():
            # Older Home Manager installations use the legacy per-user profile.
            self.profile = Path("/nix/var/nix/profiles/per-user") / os.environ["USER"] / "home-manager"
        if not self.profile.exists():
            raise ValueError("找不到已激活的 Home Manager generation；请先完成首次部署")
        lock = self.directory / "flake.lock"
        if not lock.is_file() or lock.is_symlink():
            raise ValueError("Home Manager 需要普通文件 flake.lock")
        before = tree_hash(self.directory)
        shutil.copytree(self.directory, self.snapshot, ignore=shutil.ignore_patterns(".git"))
        self.original_lock = lock.read_bytes()
        self.original_hash = before
        self.generation = str(self.profile.resolve())
        detail("  Home Manager: 在临时副本解析新锁文件和软件版本；可能下载 Nix 求值元数据")
        capture([*NIX, "flake", "update", "--flake", "path:" + str(self.snapshot)], timeout=900)
        ref = f"path:{self.snapshot}#homeConfigurations.{self.output}"
        self.ref = ref
        target = capture([*NIX, "eval", "--impure", "--raw", ref + ".activationPackage.outPath"], timeout=900)
        if target == self.generation and (self.snapshot / "flake.lock").read_bytes() == self.original_lock:
            return []
        old_paths = capture(["nix-store", "--query", "--references", str(self.profile / "home-path")]).splitlines()
        new_paths = json.loads(capture([*NIX, "eval", "--impure", "--json", ref + ".config", "--apply",
            "c: builtins.concatMap (p: map (o: toString (builtins.getAttr o p)) "
            "(builtins.filter (o: builtins.hasAttr o p) "
            "((p.meta.outputsToInstall or [ (p.outputName or \"out\") ]) ++ c.home.extraOutputsToInstall))) c.home.packages"],
            timeout=900))
        old, new = store_packages(old_paths), store_packages(new_paths)
        details = []
        for name in sorted(old.keys() | new.keys()):
            a, b = old.get(name), new.get(name)
            if a != b:
                details.append(f"{name}: {a[0] if a else '未安装'} → {b[0] if b else '移除'}"
                               + ("（依赖/构建变化）" if a and b and a[0] == b[0] else ""))
        old_nodes = json.loads(self.original_lock)["nodes"]
        new_nodes = json.loads((self.snapshot / "flake.lock").read_text())["nodes"]
        for name in sorted(old_nodes.keys() | new_nodes.keys()):
            a, b = old_nodes.get(name, {}).get("locked", {}), new_nodes.get(name, {}).get("locked", {})
            if a != b:
                details.append(f"flake/{name}: {a.get('rev', a.get('narHash', '无'))} → "
                               f"{b.get('rev', b.get('narHash', '无'))}")
        if not details:
            details.append("Home Manager 配置/激活脚本变化")
        details.append("共享锁文件：上述软件和配置将作为一次完整 generation 更新")
        return [Change(self.key, Path(self.generation).name[:12], Path(target).name[:12], details,
                       {"target": target})]

    def apply(self, change):
        self.check_unchanged()
        say("  Home Manager 下载/构建：按实际传输字节、完成任务及构建阶段显示进度")
        run_nix([*NIX, "build", "--impure", "--no-write-lock-file", "--out-link", str(self.snapshot / "result"),
                 self.ref + ".activationPackage"])
        target = (self.snapshot / "result").resolve()
        if str(target) != change.data["target"]:
            raise RuntimeError("构建输出与预览不符，取消激活")
        self.check_unchanged()
        self.state.mkdir(parents=True, exist_ok=True)
        backup = self.state / "home-manager-previous-lock.json"
        backup.write_bytes(self.original_lock)
        (self.state / "home-manager-previous-generation").write_text(self.generation + "\n")
        say("  Home Manager 激活：执行配置步骤（无总量百分比）")
        live([str(target / "activate")])
        # Persist only the exact lock used for the successfully activated generation.
        if tree_hash(self.directory) != self.original_hash:
            raise RuntimeError("激活期间源配置被修改；新 generation 已激活，但未覆盖 flake.lock")
        temporary = self.directory / ".flake.lock.update-new"
        temporary.write_bytes((self.snapshot / "flake.lock").read_bytes())
        temporary.replace(self.directory / "flake.lock")
        say("  Home Manager 安装：100%；旧 generation 和锁文件已保留")

    def check_unchanged(self):
        if tree_hash(self.directory) != self.original_hash or str(self.profile.resolve()) != self.generation:
            raise RuntimeError("Home Manager 配置或已安装 generation 在预览后改变，请重新检查")


class NixRuntime:
    key = "nix"

    def available(self):
        return bool(shutil.which("nix"))

    def plan(self):
        installed = capture(["nix", "--version"])
        if "Determinate" in installed:
            text = capture(["determinate-nixd", "version"], timeout=90)
            old = re.search(r"Determinate Nix ([0-9.]+)", installed)
            latest = re.search(r"Latest version:\s*v?([0-9.]+)", text)
            if old and not latest and "You are running the latest version of Determinate Nix." in text:
                reported = re.findall(r"^Determinate Nixd (?:daemon|client) version:\s*([0-9.]+)\s*$", text, re.MULTILINE)
                if len(reported) == 2 and all(version == old[1] for version in reported):
                    return []
            if not old or not latest:
                raise ValueError("无法读取 Determinate Nix 最新版本；未假定已是最新")
            old, new = old[1], latest[1]
            if versions(old) >= versions(new):
                return []
            executable = shutil.which("determinate-nixd")
            return [Change(self.key, old, new, ["更新 Nix daemon，需要单独 sudo 授权"],
                           {"command": ["sudo", executable, "upgrade", "--version", new]})]
        raise ValueError("当前不是 Determinate Nix。请按 Nix 的实际安装来源升级；未假定已是最新。")

    def apply(self, change):
        from .core import confirm
        current = capture(["nix", "--version"])
        if f"Determinate Nix {change.new})" in current:
            say("  Nix 已是目标版本")
            return
        if f"Determinate Nix {change.old})" not in current:
            raise RuntimeError("Nix 版本在预览后改变，请重新检查")
        if not confirm(f"单独授权更新 Nix {change.old} → {change.new}？"):
            raise RuntimeError("用户跳过 Nix 更新")
        live(["sudo", "-k", "-v"])
        live(change.data["command"])
        installed = capture(["nix", "--version"])
        if f"Determinate Nix {change.new})" not in installed:
            raise RuntimeError("Nix 命令所指版本与目标不符，请检查 PATH")
