"""Version-pinned local applications with staged, atomic activation."""
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

from .core import Change, Progress, atomic_link, download, get_json, say, versions


SPECS = {
    "code": ("vscode", None, None, "VSCode-linux-x64/code"),
    "xmcl": ("XMCL", "Voxelum/x-minecraft-launcher", r"xmcl-[0-9.]+-x86_64\.AppImage", None),
    "localsend": ("LocalSend", "localsend/localsend", r"LocalSend-[0-9.]+-linux-x86-64\.tar\.gz", "localsend_app"),
}


class Local:
    def __init__(self, key, home=None):
        self.key = key
        self.directory, self.repo, self.pattern, self.executable = SPECS[key]
        self.base = (home or Path.home()) / ".local/opt" / self.directory

    def available(self):
        return (self.base / "current").is_symlink()

    def current(self):
        target = (self.base / "current").resolve()
        record = target / ".update-version.json" if self.executable else target.with_name(target.name + ".json")
        if record.is_file():
            return json.loads(record.read_text())["version"]
        if self.key == "code":
            package = target / "VSCode-linux-x64/resources/app/package.json"
            if package.is_file():
                return json.loads(package.read_text())["version"]
        match = re.search(r"\d+\.\d+(?:\.\d+){1,3}", target.name)
        return match.group() if match else "未知"

    def plan(self):
        old = self.current()
        if self.key == "code":
            data = get_json("https://update.code.visualstudio.com/api/update/linux-x64/stable/latest")
            new, sha = data["productVersion"], data.get("sha256hash")
            # Commit URL, never /latest: the confirmed release cannot drift during download.
            if not re.fullmatch(r"[a-f0-9]{40}", data["version"]):
                raise ValueError("VS Code commit 无效")
            url = f"https://update.code.visualstudio.com/commit:{data['version']}/linux-x64/stable"
            notes = "https://code.visualstudio.com/updates/v" + ".".join(new.split(".")[:2]).replace(".", "_")
        else:
            data = get_json(f"https://api.github.com/repos/{self.repo}/releases/latest")
            if data.get("draft") or data.get("prerelease"):
                raise ValueError("不是正式发布")
            new = data["tag_name"].lstrip("v")
            assets = [a for a in data["assets"] if re.fullmatch(self.pattern, a["name"]) and new in a["name"]]
            if len(assets) != 1:
                raise ValueError("找不到唯一的 x86_64 下载文件")
            url = assets[0]["browser_download_url"]
            sha = (assets[0].get("digest") or "").removeprefix("sha256:")
            notes = data.get("html_url", "")
        versions(new)
        if old != "未知" and versions(old) >= versions(new):
            return []
        if not sha or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
            raise ValueError("发布方未提供有效 SHA-256，无法生成可验证更新计划")
        return [Change(self.key, old, new, ["发布说明：" + notes], {"url": url, "sha256": sha})]

    def apply(self, change):
        if self.current() != change.old:
            raise RuntimeError("本地版本在预览后已改变，请重新检查")
        self.base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".update-", dir=self.base) as directory:
            scratch = Path(directory)
            archive = scratch / "download"
            sha = download(change.data["url"], archive, self.key, change.data["sha256"])
            if self.executable:
                staged = scratch / "app"
                staged.mkdir()
                with tarfile.open(archive) as tar:
                    members = tar.getmembers()
                    progress = Progress(self.key + " 解包", sum(m.size for m in members if m.isfile()))
                    done = 0
                    for member in members:
                        tar.extract(member, staged, filter="data")
                        done += member.size if member.isfile() else 0
                        progress.show(done)
                    progress.show(done, finished=True)
                if not (staged / self.executable).is_file():
                    children = list(staged.iterdir())
                    if len(children) == 1 and children[0].is_dir() and (children[0] / self.executable).is_file():
                        staged = children[0]
                if not (staged / self.executable).is_file() or not os.access(staged / self.executable, os.X_OK):
                    raise ValueError("归档缺少程序入口")
                (staged / ".update-version.json").write_text(json.dumps({"version": change.new}))
            else:
                staged = archive
                staged.chmod(0o755)
            # Unique content-addressed target; old installations are retained for rollback.
            target = self.base / f"{self.directory}-{change.new}-{sha[:12]}"
            if target.exists():
                raise RuntimeError(f"目标目录已存在，请检查后重试：{target}")
            staged.rename(target)
            if not self.executable:
                target.with_name(target.name + ".json").write_text(json.dumps({"version": change.new}))
            atomic_link(self.base / "current", target.name)
            say(f"  {self.key} 激活：100% · {change.new}；旧版本已保留")
