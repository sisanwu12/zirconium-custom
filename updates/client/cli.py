"""Discover -> plan -> review -> confirm -> apply applications -> confirm OS."""
import argparse
import fcntl
import html
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from . import core
from .core import capture, config_home, confirm, say, detail, status, clear_status
from .local import Local, SPECS
from .chat import Chat
from .chat_sources import PACKAGES
from .nix import HomeManager, NixRuntime
from .system import System


ALIASES = {"vscode": "code", "hm": "home-manager", "os": "system", "qq": "chat:linuxqq",
           "wechat": "chat:wechat", "feishu": "chat:bytedance-feishu-stable", "wemeet": "chat:wemeet"}
KEYS = [*SPECS, "chat", "home-manager", "nix", "system"]


def selectors(values):
    result = []
    for raw in values:
        for value in raw.split(","):
            value = ALIASES.get(value, value)
            if value not in KEYS and value not in ["chat:" + p for p in [*PACKAGES, "apt"]]:
                raise ValueError(f"未知更新目标：{value}。使用 update --list 查看；Home Manager 按完整配置更新。")
            if value not in result:
                result.append(value)
    return result


def providers(selected, workspace, config):
    all_mode = not selected
    selected = selected or KEYS
    result = [Local(k) for k in SPECS if k in selected]
    chat = [key.split(":", 1)[1] for key in selected if key.startswith("chat:")]
    if "chat" in selected or chat:
        packages = ["apt", *PACKAGES] if "chat" in selected else chat
        result.extend(Chat([package], explicit="chat:" + package in selected) for package in packages)
    if "home-manager" in selected:
        result.append(HomeManager(workspace, config))
    if "nix" in selected:
        result.append(NixRuntime())
    if "system" in selected:
        result.append(System(config))
    return result, all_mode


def collect(backends, all_mode):
    plan, errors = [], []
    say("检查更新…")
    for backend in backends:
        detail(f"检查 {backend.key}…")
        status(f"  检查 {backend.key}…")
        stop = threading.Event()
        started = time.monotonic()
        def heartbeat():
            while not stop.wait(20):
                status(f"  检查 {backend.key} · {int(time.monotonic() - started)} 秒…")
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            if not backend.available():
                if not all_mode:
                    raise ValueError("该更新目标未安装或环境不适用")
                detail("  未安装/不适用，跳过")
                continue
            changes = backend.plan()
            if not changes:
                detail("  无可执行更新")
            plan.extend((backend, change) for change in changes)
        except Exception as error:
            errors.append(backend.key)
            say(f"  {backend.key} 检查失败：{error}")
        finally:
            stop.set()
            thread.join()
    clear_status()
    return plan, errors


def show_plan(plan, errors):
    say("\n更新计划（尚未安装）")
    for _, change in plan:
        if change.key == "home-manager" and not core.VERBOSE:
            say("  home-manager: 更新完整配置与软件")
        elif change.system and not core.VERBOSE:
            say(f"  system: {change.new}（最后单独确认并授权 sudo）")
        else:
            say(f"  {change.key}: {change.old} → {change.new}")
        lines = change.details
        if not core.VERBOSE:
            # Keep user-visible package changes; hashes and rebuilds remain in --verbose.
            visible = [s for s in lines if not s.startswith(("flake/", "发布说明：", "目标："))
                       and "（依赖/构建变化）" not in s and "构建变化 →" not in s
                       and "sha256:" not in s]
            lines = visible[:5]
        for line in lines:
            say("    " + line)
        if len(lines) < len(change.details):
            say(f"    另有 {len(change.details) - len(lines)} 项细节；--verbose 查看全部")
    if not plan:
        say("  没有可执行的更新。")
    if errors:
        say("检测未完成：" + ", ".join(errors) + "。这些目标不会被当作已是最新。")


def execute(plan, errors):
    applications = [(b, c) for b, c in plan if not c.system]
    systems = [(b, c) for b, c in plan if c.system]
    failures = list(errors)
    if applications:
        if not confirm(f"安装上述 {len(applications)} 项软件更新？"):
            say("已取消本次更新；系统更新也未执行。")
            return 1 if failures else 0
        for index, (backend, change) in enumerate(applications, 1):
            say(f"\n[{index}/{len(applications)}] {change.key} · {change.old} → {change.new}")
            try:
                backend.apply(change)
                say(f"完成：{change.key}")
            except Exception as error:
                failures.append(change.key)
                say(f"失败：{change.key}: {error}")
    if systems and failures:
        say("软件更新或检测存在失败，系统更新已跳过。修复后可执行 update system。")
    elif systems:
        for backend, change in systems:
            if confirm(f"软件阶段已完成。现在单独授权更新系统至 {change.new}？随后将重新请求 sudo 认证"):
                try:
                    backend.apply(change)
                except Exception as error:
                    failures.append(change.key)
                    say(f"系统更新失败：{error}")
            else:
                say("已跳过系统更新。")
    say("\n更新结束。" + ("失败/未完成：" + ", ".join(failures) if failures else ""))
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="先预览变更，再确认安装；系统最后单独确认并授权 sudo。")
    parser.add_argument("targets", nargs="*", help="软件名或组，可指定多个，也支持逗号分隔")
    parser.add_argument("--check", action="store_true", help="只检查；可刷新元数据和 Nix 求值缓存，不安装")
    parser.add_argument("--notify", action="store_true", help="只检查并发送桌面通知，兼容旧定时器")
    parser.add_argument("--only", action="append", default=[], help="兼容旧用法；可重复指定")
    parser.add_argument("--list", action="store_true", help="列出支持的更新目标")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示完整变更、命令及构建日志")
    parser.add_argument("--config", type=Path, default=config_home() / "zirconium-updater/config.json")
    args = parser.parse_args(argv)
    core.configure_output(args.verbose)
    if args.list:
        say("\n".join(KEYS + ["chat:" + p for p in [*PACKAGES, "apt"]]))
        say("别名：" + ", ".join(f"{a}={b}" for a, b in ALIASES.items()))
        return 0
    try:
        selected = selectors(args.targets + args.only)
        if not selected and args.notify and Path(sys.argv[0]).name == "opt-updater":
            # The existing eight-minute notification service keeps its original scope.
            selected = [*SPECS, "chat"]
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        if os.geteuid() == 0:
            raise ValueError("请用普通用户运行 update；仅在明确需要时由程序请求 sudo")
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "zirconium-updater"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("另一个 update 检查或安装正在进行") from None
            with tempfile.TemporaryDirectory(prefix="plan-", dir=state) as directory:
                backends, all_mode = providers(selected, Path(directory), config)
                plan, errors = collect(backends, all_mode)
                show_plan(plan, errors)
                if args.notify and (plan or errors):
                    message = "\n".join(f"{c.key}: {c.old} → {c.new}" for _, c in plan)
                    if errors:
                        message += "\n检查失败：" + ", ".join(errors)
                    capture(["notify-send", "--app-name=统一更新", "软件更新检查",
                             html.escape(message + "\n在终端运行 update 查看并确认。")])
                if args.check or args.notify:
                    return 1 if errors else 0
                return execute(plan, errors)
    except KeyboardInterrupt:
        say("\n已中断；已完成的更新不会自动回退，尚未开始的步骤不会执行。")
        return 130
    except Exception as error:
        say(f"update: {error}")
        return 1
