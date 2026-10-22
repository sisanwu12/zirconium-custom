"""Observe cloud candidates without sudo; deploy only an approved, aged digest."""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import shutil

import notify
from . import core
from .core import Change, capture, live, say


def pending_candidates(state, now, current_id, built_at="", current_digest=""):
    stable = state.get("stable") or {}
    baseline = [notify.parse_time(x) for x in (built_at, stable.get("built_at")) if x and x != "local"]
    result = []
    for item in state.get("candidates", []):
        if item.get("blocked") or item["input_id"] == current_id or item["digest"] == current_digest:
            continue
        built = notify.parse_time(item["built_at"])
        if baseline and built <= max(baseline):
            continue
        remaining = max(0, math.ceil((built + timedelta(days=14) - now).total_seconds()))
        result.append((item, remaining))
    return sorted(result, key=lambda row: notify.parse_time(row[0]["built_at"]))


def image_changes(old, new):
    lines = []
    for field, label in (("base_digest", "官方基础镜像"), ("recipe", "构建配置"),
                         ("dependency_snapshot", "RPM 依赖快照")):
        if new.get(field) and old.get(field) != new[field]:
            lines.append(f"{label}: {old.get(field, '旧版本元数据不可用')} → {new[field]}")
    a, b = old.get("flclash", {}).get("version"), new.get("flclash", {}).get("version")
    if a != b and b:
        lines.append(f"FlClash: {a or '旧版本未知'} → {b}")
    a, b = old.get("rpm", {}), new.get("rpm", {})
    # Accept both build input snapshots and schema-1 published snapshots.
    a, b = a.get("direct", a), b.get("direct", b)
    for name in sorted(a.keys() | b.keys()):
        if a.get(name) != b.get(name):
            lines.append(f"{name}: {', '.join(a.get(name, ['旧版本未知']))} → "
                         f"{', '.join(b.get(name, ['移除']))}")
    return lines


class System:
    key = "system"

    def __init__(self, config):
        self.repository = config.get("repository", notify.read_info("repository", notify.DEFAULT_REPOSITORY))

    def available(self):
        return bool(shutil.which("bootc")) and Path("/run/ostree-booted").exists()

    def local_status(self):
        # bootc status needs root on some releases; rpm-ostree's read-only D-Bus API does not.
        status = json.loads(capture(["rpm-ostree", "status", "--json"]))
        self.booted = next(d for d in status["deployments"] if d.get("booted"))
        self.staged = next((d for d in status["deployments"] if d.get("staged")), None)
        return self.booted.get("container-image-reference-digest", "")

    def plan(self):
        state = notify.fetch_state(self.repository)
        digest = self.local_status()
        current = next((c for c in state.get("candidates", []) if c["digest"] == digest), {})
        if (state.get("stable") or {}).get("digest") == digest:
            current = state["stable"]
        current_id = current.get("input_id", notify.read_info("update-id"))
        built_at = current.get("built_at", notify.read_info("built-at"))
        old_inputs = current.get("inputs", {})
        snapshot = notify.INFO / "update-inputs.json"
        if not old_inputs and snapshot.is_file():
            old_inputs = json.loads(snapshot.read_text())
        if not old_inputs:
            # Older images predate embedded build metadata; still show actual RPM versions.
            rows = capture(["rpm", "-qa", "--qf", "%{NAME}\t%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n"])
            installed = {}
            for row in rows.splitlines():
                name, version = row.split("\t", 1)
                installed.setdefault(name, []).append(version)
            wanted = set()
            for item in [*state.get("candidates", []), state.get("stable") or {}]:
                packages = item.get("inputs", {}).get("rpm", {})
                wanted.update(packages.get("direct", packages))
            old_inputs = {"rpm": {name: installed[name] for name in wanted if name in installed}}
            flclash = next((name for name in installed if name.lower() == "flclash"), None)
            if flclash:
                old_inputs["flclash"] = {"version": capture(["rpm", "-q", "--qf", "%{VERSION}", flclash])}
        now = datetime.now(timezone.utc)
        previous_inputs = old_inputs
        pending = pending_candidates(state, now, current_id, built_at, digest)
        for item, seconds in (pending if core.VERBOSE else pending[:1]):
            if seconds:
                days, rem = divmod(seconds, 86400)
                hours, rem = divmod(rem, 3600)
                minutes = rem // 60
                say(f"  系统候选 {item['tag']}：还有 {days} 天 {hours} 小时 {minutes} 分钟"
                    f"（约 {math.ceil(seconds / 86400)} 天）完成 14 天观察")
            else:
                say(f"  系统候选 {item['tag']}：14 天已满，等待云端发布 stable（尚不可安装）")
            changes = image_changes(previous_inputs, item.get("inputs", {}))
            if core.VERBOSE:
                say("    变更（首项对比本机，其后对比上一候选）：")
                for line in changes:
                    say("    " + line)
            previous_inputs = item.get("inputs", {})
        if not core.VERBOSE and len(pending) > 1:
            say(f"  另有 {len(pending) - 1} 个系统候选等待发布；--verbose 查看各自倒计时和变更")
        stable = notify.available_update(state, now, current_id, built_at)
        if not stable or stable["digest"] == digest:
            return []
        if self.staged:
            say("  系统已有待重启部署；本次不覆盖。请先检查 bootc status 并处理该部署。")
            return []
        source = self.booted.get("container-image-reference", "")
        repository_image = "ghcr.io/" + self.repository.lower()
        if not re.fullmatch(r"(?:ostree-unverified-registry:|ostree-image-signed:docker://)?"
                            + re.escape(repository_image) + r"(?::(?:stable|latest)|@sha256:[a-f0-9]{64})", source):
            raise ValueError("当前系统未跟踪本仓库 stable/latest/固定 digest；请先明确 bootc switch 来源")
        details = image_changes(old_inputs, stable["inputs"])
        details += ["目标：" + repository_image + "@" + stable["digest"],
                    "系统最后单独确认并重新 sudo 授权；准备部署后自行重启",
                    "为保证安装预览中的版本，来源将固定到此 digest；以后仍用 update 获取正式版本"]
        self.current_id, self.built_at, self.digest = current_id, built_at, digest
        return [Change("system", digest or "当前系统", stable["tag"], details, stable, system=True)]

    def apply(self, change):
        # Revalidate publication/age/block status after the possibly long application phase.
        state = notify.fetch_state(self.repository)
        stable = notify.available_update(state, datetime.now(timezone.utc), self.current_id, self.built_at)
        if not stable or stable["digest"] != change.data["digest"]:
            raise RuntimeError("云端正式版本或阻止状态已改变，请重新运行 update system 查看计划")
        if self.local_status() != self.digest or self.staged:
            raise RuntimeError("本地系统部署在预览后改变，请重新检查")
        live(["sudo", "-k", "-v"])
        # Inspect again with the authoritative bootc interface after authentication.
        status = json.loads(capture(["sudo", "bootc", "status", "--json"]))["status"]
        if status.get("staged"):
            raise RuntimeError("bootc 已有待重启部署，取消覆盖")
        live(["sudo", "bootc", "switch", "ghcr.io/" + self.repository.lower() + "@" + stable["digest"]])
        say("系统部署已准备完成。保存工作后自行重启；本程序不会自动重启。")
