"""Shared plan, transport, progress and process primitives; no shell evaluation."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request


@dataclass
class Change:
    key: str
    old: str
    new: str
    details: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    system: bool = False


def say(message):
    # Metadata is untrusted terminal text, not terminal control sequences.
    print("".join(c if c in "\n\t" or ord(c) >= 32 and ord(c) != 127 else "?"
                  for c in str(message)), flush=True)


def capture(args, *, cwd=None, timeout=180, env=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} 失败 ({result.returncode}): "
                           + (result.stderr or result.stdout)[-3000:].strip())
    return result.stdout.strip()


def live(args, *, cwd=None, env=None):
    """Inherit the terminal, including native percentage bars and sudo prompts."""
    say("执行：" + " ".join(map(str, args)))
    process = subprocess.Popen([str(x) for x in args], cwd=cwd, env=env)
    try:
        code = process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    if code:
        raise RuntimeError(f"{args[0]} 失败 ({code})")


def request(url, headers=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("下载地址必须使用 HTTPS")
    response = urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": "zirconium-updater/2", "Cache-Control": "no-cache", **(headers or {})}), timeout=45)
    if urllib.parse.urlsplit(response.geturl()).scheme != "https":
        response.close()
        raise ValueError("下载重定向离开 HTTPS")
    return response


def get_json(url):
    with request(url) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("元数据过大")
    return json.loads(data)


class Progress:
    def __init__(self, label, total=0):
        self.label, self.total = label, total
        self.started = time.monotonic()
        self.last = -1

    def show(self, done, *, finished=False):
        percent = min(100 if finished else 99, int(done * 100 / self.total)) if self.total else None
        bucket = percent // (1 if sys.stdout.isatty() else 10) if percent is not None else int(done / 10485760)
        if bucket == self.last and not finished:
            return
        self.last = bucket
        elapsed = max(time.monotonic() - self.started, .001)
        amount = f"{done / 1048576:.1f} MiB"
        if self.total:
            amount += f" / {self.total / 1048576:.1f} MiB"
        status = f"{percent}%" if percent is not None else "总量未知"
        say(f"  {self.label}: {status} · {amount} · {done / elapsed / 1048576:.1f} MiB/s")


def download(url, path, label, expected=None):
    digest = hashlib.sha256()
    with request(url) as response, path.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        progress = Progress(label + " 下载", total)
        done = 0
        progress.show(0)
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            progress.show(done)
        if not done or total and total != done:
            raise ValueError("下载不完整")
        if expected and digest.hexdigest().lower() != expected.lower():
            raise ValueError("SHA-256 校验失败，保留旧版本")
        progress.show(done, finished=True)
    return digest.hexdigest()


def atomic_link(link, target):
    temporary = link.with_name(link.name + ".update-new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(link)


def confirm(prompt):
    if not sys.stdin.isatty():
        say("非交互终端：未授权安装。请在终端运行 update，或使用 --check 查看。")
        return False
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def versions(value):
    import re
    if not re.fullmatch(r"v?\d+(?:\.\d+){1,3}", value):
        raise ValueError("无法识别正式版本：" + value)
    return tuple(map(int, value.removeprefix("v").split(".")))


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
