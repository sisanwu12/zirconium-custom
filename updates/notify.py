#!/usr/bin/env python3
"""Read public update metadata and notify; never invoke bootc or gain privileges."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request


INFO = Path("/usr/share/zirconium-custom")
DEFAULT_REPOSITORY = "sisanwu12/zirconium-custom"
WAIT_DAYS = 14


def read_info(name, default=""):
    try:
        return (INFO / name).read_text().strip()
    except FileNotFoundError:
        return default


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone")
    return result


def fetch_state(repository):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid repository")
    url = f"https://raw.githubusercontent.com/{repository}/update-state/updates.json"
    request = urllib.request.Request(url, headers={"User-Agent": "zirconium-custom-notifier",
                                                 "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Update metadata is too large")
    state = json.loads(data)
    if state.get("schema") != 1 or state.get("repository") != repository:
        raise ValueError("Unexpected update metadata")
    return state


def available_update(state, now, current_id, current_built_at=""):
    stable = state.get("stable")
    if not stable:
        return None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", stable["digest"]):
        raise ValueError("Invalid stable image digest")
    if not re.fullmatch(r"[0-9a-f]{64}", stable["input_id"]):
        raise ValueError("Invalid stable input ID")
    # Recheck age locally instead of relying solely on the server's ready_at label.
    if stable.get("blocked") or parse_time(stable["built_at"]) + timedelta(days=WAIT_DAYS) > now:
        return None
    if any(item["digest"] == stable["digest"] and item.get("blocked")
           for item in state.get("candidates", [])):
        return None
    if stable["input_id"] == current_id:
        return None
    if current_built_at and current_built_at != "local":
        if parse_time(current_built_at) >= parse_time(stable["built_at"]):
            return None  # Never advertise a downgrade to a user testing a newer image.
    return stable


def should_notify(previous, digest, now):
    return previous.get("digest") != digest or previous.get("date") != now.date().isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notify", action="store_true", help="Show a desktop notification")
    parser.add_argument("--repository", default=read_info("repository", DEFAULT_REPOSITORY))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    state = fetch_state(args.repository)
    update = available_update(state, now, read_info("update-id"), read_info("built-at"))
    if not update:
        print("没有需要安装的已满 14 天的更新。")
        return
    local_time = parse_time(update["built_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    message = (f"定制镜像 {update['tag']} 已观察至少 14 天。\n"
               f"构建时间：{local_time}\n"
               f"FlClash：{update['inputs']['flclash']['version']}\n"
               "请在终端手动执行 sudo bootc upgrade，完成后自行重启。")
    print(message)
    if not args.notify:
        return
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    suffix = hashlib.sha256(args.repository.encode()).hexdigest()[:12]
    cache = cache_home / "zirconium-custom" / ("notification-" + suffix + ".json")
    try:
        previous = json.loads(cache.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
    if not should_notify(previous, update["digest"], now):
        return
    subprocess.run(["notify-send", "--app-name=Zirconium Custom", "--icon=software-update-available",
                    "系统更新可手动安装", html.escape(message)], check=True, timeout=20)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps({"digest": update["digest"], "date": now.date().isoformat()}))
    temporary.replace(cache)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"检查更新失败（未执行安装）：{error}", file=sys.stderr)
        sys.exit(1)
