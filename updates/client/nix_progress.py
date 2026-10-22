"""Render Nix's structured activities using actual byte and task counters.

Protocol: Nix libutil logging.hh (ActivityType / ResultType). Unknown events
fall back to text so a newer Nix release does not silently hide diagnostics.
"""
import json
from pathlib import Path
import subprocess

from .core import Progress, say


class NixProgress:
    def __init__(self):
        self.activities = {}
        self.transfers = {}
        self.counters = {}

    def line(self, line):
        if not line.startswith("@nix "):
            if line.strip():
                say(line.rstrip())
            return
        try:
            event = json.loads(line[5:])
        except (ValueError, TypeError):
            say(line.rstrip())
            return
        action, identity = event.get("action"), event.get("id")
        if action == "start":
            self.activities[identity] = event
            if event.get("type") in (100, 105, 108):
                say("  Nix: " + (event.get("text") or self.label(identity)))
        elif action == "msg":
            say(event.get("msg", ""))
        elif action == "result":
            fields = event.get("fields", [])
            kind = event.get("type")
            activity = self.activities.get(identity, {})
            if kind == 105 and len(fields) >= 2 and all(isinstance(v, int) for v in fields[:2]):
                done, total = fields[:2]
                if activity.get("type") in (100, 101) and total:
                    progress = self.transfers.setdefault(identity, Progress(self.label(identity) + " 下载/写入", total))
                    progress.total = total
                    progress.show(done)
                elif activity.get("type") in (103, 104) and total:
                    value = (done, total)
                    if self.counters.get(identity) != value:
                        self.counters[identity] = value
                        say(f"  Nix 已完成任务：{done}/{total} ({min(100, done * 100 // total)}%)")
            elif kind in (101, 104, 107) and fields:
                say(f"  {self.label(identity)}: {fields[0]}")
        elif action == "stop":
            # stop also occurs on failure/cancellation; never infer success from it.
            pass

    def label(self, identity):
        visited = set()
        while identity in self.activities and identity not in visited:
            visited.add(identity)
            activity = self.activities[identity]
            for value in activity.get("fields", []):
                if isinstance(value, str) and value.startswith("/nix/store/"):
                    return Path(value).name.split("-", 1)[-1]
            identity = activity.get("parent")
        return "Nix 传输"


def run_nix(args):
    say("执行：" + " ".join(args))
    renderer = NixProgress()
    process = subprocess.Popen([*args, "--log-format", "internal-json", "-L"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in process.stdout:
            renderer.line(line)
        code = process.wait()
        if code:
            raise RuntimeError(f"Nix 下载/构建失败 ({code})，未激活新配置")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        process.stdout.close()
    say("  Nix 下载/构建任务：100%")
