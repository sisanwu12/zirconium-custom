#!/usr/bin/env python3
"""Detect immutable inputs, build only on change, promote the same image after 14d."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
BASE = "ghcr.io/zirconium-dev/zirconium"
STATE_BRANCH = "update-state"
STATE_FILE = "updates.json"
WAIT_DAYS = 14
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def run(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository = repository
        self.token = token
        self.state_sha = None

    def api(self, path, data=None, method=None):
        request = urllib.request.Request(
            "https://api.github.com/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": "zirconium-custom-updates"}, method=method)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def load_state(self):
        try:
            result = self.api(f"repos/{self.repository}/contents/{STATE_FILE}?ref={STATE_BRANCH}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            # A 404 is initialization only if the state branch itself is absent.
            # Never silently reset a branch whose state file was lost/deleted.
            try:
                self.api(f"repos/{self.repository}/git/ref/heads/{STATE_BRANCH}")
            except urllib.error.HTTPError as branch_error:
                if branch_error.code != 404:
                    raise
                return {"schema": 1, "wait_days": WAIT_DAYS,
                        "repository": self.repository, "candidates": [], "stable": None}
            raise RuntimeError("State branch exists but updates.json is missing") from error
        self.state_sha = result["sha"]
        state = json.loads(base64.b64decode(result["content"]))
        if state.get("schema") != 1 or state.get("repository") != self.repository:
            raise ValueError("Unexpected update state")
        return state

    def save_state(self, state):
        # Keep all waiting images and recent history; old history stays in git.
        recent = {item["digest"] for item in state["candidates"][-20:]}
        stable_digest = (state.get("stable") or {}).get("digest")
        state["candidates"] = [item for item in state["candidates"]
                               if item["digest"] in recent or item["digest"] == stable_digest
                               or parse_time(item["built_at"]) + timedelta(days=WAIT_DAYS) > utcnow()]
        if self.state_sha is None:
            # Use the existing main commit to initialize a separate metadata branch.
            ref = self.api(f"repos/{self.repository}/git/ref/heads/"
                           + os.environ.get("GITHUB_REF_NAME", "main"))
            self.api(f"repos/{self.repository}/git/refs",
                     {"ref": "refs/heads/" + STATE_BRANCH, "sha": ref["object"]["sha"]})
        payload = {"message": "Update candidate and stable image metadata",
                   "branch": STATE_BRANCH,
                   "content": base64.b64encode(
                       (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode()
                   ).decode()}
        if self.state_sha:
            payload["sha"] = self.state_sha
        result = self.api(f"repos/{self.repository}/contents/{STATE_FILE}", payload, method="PUT")
        self.state_sha = result["content"]["sha"]


def base_digest():
    manifest = json.loads(run("docker", "buildx", "imagetools", "inspect",
                              BASE + ":latest", "--format", "{{json .Manifest}}", capture=True))
    if "manifests" in manifest:
        matches = [item["digest"] for item in manifest["manifests"]
                   if item.get("platform", {}).get("os") == "linux"
                   and item.get("platform", {}).get("architecture") == "amd64"]
        if len(matches) != 1:
            raise ValueError("Expected exactly one linux/amd64 base manifest")
        digest = matches[0]
    else:
        digest = manifest["digest"]
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("Invalid base digest")
    return digest


def flclash_release(github):
    release = github.api("repos/chen08209/FlClash/releases/latest")
    version = release["tag_name"].removeprefix("v")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("FlClash latest release is not a supported stable version")
    name = f"FlClash-{version}-linux-amd64.rpm"
    assets = [item for item in release["assets"] if item["name"] == name]
    if len(assets) != 1:
        raise ValueError("Stable FlClash release has no unique amd64 RPM")
    asset = assets[0]
    digest = asset.get("digest") or ""
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("FlClash asset has no published SHA-256; manual review required")
    return {"version": version, "sha256": digest.removeprefix("sha256:"),
            "url": asset["browser_download_url"]}


def package_names():
    names = (ROOT / "updates/packages.txt").read_text().split()
    if not names or any(not re.fullmatch(r"[a-zA-Z0-9+_.-]+", name) for name in names):
        raise ValueError("Invalid RPM package list")
    return names


def check_flclash_version(release, state):
    """A changed GitHub 'latest' pointer must not silently downgrade the application."""
    pinned = re.search(r"^ARG FLCLASH_VERSION=(\d+\.\d+\.\d+)$",
                       (ROOT / "Containerfile").read_text(), re.MULTILINE)
    if not pinned:
        raise ValueError("Missing default FlClash version")
    versions = [pinned.group(1)]
    if state["candidates"]:
        versions.append(state["candidates"][-1]["inputs"]["flclash"]["version"])
    key = lambda value: tuple(map(int, value.split(".")))
    if key(release["version"]) < max(map(key, versions)):
        raise ValueError("FlClash latest points to an older version; manual review required")


def parse_packages(output):
    packages = {}
    for line in output.splitlines():
        if not line.startswith("ZCUSTOM:"):
            continue
        name, nevra = line.removeprefix("ZCUSTOM:").split("|", 1)
        if not re.fullmatch(r"[a-zA-Z0-9+_.:-]+", nevra):
            raise ValueError("Unexpected RPM version")
        packages.setdefault(name, set()).add(nevra)
    return {name: sorted(versions) for name, versions in sorted(packages.items())}


def rpm_versions(base, names):
    # Docker's classic image store cannot import upstream's large layer chain.
    # Use the same BuildKit snapshotter as the OS build and export only text.
    query = ["repoquery", "--available", "--arch=x86_64,noarch", "--latest-limit=1",
             "--queryformat=ZCUSTOM:%{name}|%{full_nevra}\\n"]
    direct_command = shlex.join(["dnf5", "--refresh", *query, *names])
    dependency_command = shlex.join(["dnf5", *query, "--providers-of=requires", "--recursive", *names])
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "Containerfile"
        output = Path(directory) / "output"
        probe.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE} AS query\n"
                         "RUN mkdir -p /query && " + direct_command + " > /query/direct.txt && "
                         + dependency_command + " > /query/dependencies.txt\n"
                         "FROM scratch\nCOPY --from=query /query/ /\n")
        run("docker", "buildx", "build", "--file", str(probe), "--platform", "linux/amd64",
            "--build-arg", "BASE_IMAGE=" + base, "--no-cache", "--pull",
            "--output", "type=local,dest=" + str(output), directory)
        direct = parse_packages((output / "direct.txt").read_text())
        dependencies = parse_packages((output / "dependencies.txt").read_text())
    if set(names) - direct.keys():
        raise ValueError("RPM packages unavailable: " + ", ".join(sorted(set(names) - direct.keys())))
    return {"direct": direct, "dependencies": dependencies}


def recipe_hash():
    paths = [ROOT / "Containerfile", ROOT / ".dockerignore"] + [path for path in (ROOT / "updates").rglob("*")
                                             if path.is_file() and "__pycache__" not in path.parts]
    return fingerprint({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(paths)})


def eligible_candidate(state, now):
    eligible = [item for item in state["candidates"]
                if not item.get("blocked", False)
                and parse_time(item["built_at"]) + timedelta(days=WAIT_DAYS) <= now]
    stable = state.get("stable")
    if stable:
        eligible = [item for item in eligible
                    if parse_time(item["built_at"]) > parse_time(stable["built_at"])]
    return max(eligible, key=lambda item: parse_time(item["built_at"]), default=None)


def block_candidate(state, digest):
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("block_digest must be a full sha256 digest")
    if (state.get("stable") or {}).get("digest") == digest:
        raise ValueError("Already promoted; manually select a known-good image instead")
    matches = [item for item in state["candidates"] if item["digest"] == digest]
    if not matches:
        raise ValueError("Unknown candidate digest")
    for item in matches:
        item["blocked"] = True


def promote(state, image, now):
    candidate = eligible_candidate(state, now)
    if not candidate:
        print("No new candidate has completed the 14-day wait.", flush=True)
        return False
    # Retag the existing bytes. Rebuilding here would invalidate the waiting period.
    run("docker", "buildx", "imagetools", "create", "--prefer-index=false",
        "--tag", image + ":stable", "--tag", image + ":latest", image + "@" + candidate["digest"])
    state["stable"] = {**candidate, "promoted_at": stamp(now)}
    print("Promoted aged candidate: " + candidate["tag"], flush=True)
    return True


def build_candidate(image, inputs, input_id, repository):
    started = utcnow()
    tag = "candidate-" + started.strftime("%Y%m%dT%H%M%SZ") + "-" + os.environ["GITHUB_RUN_ID"]
    direct = inputs["rpm"]["direct"]
    # Pin the application's resolved NEVRAs; dependency resolution remains DNF's job.
    packages = " ".join(version for versions in direct.values() for version in versions)
    with tempfile.TemporaryDirectory() as directory:
        metadata = str(Path(directory) / "build.json")
        args = ["docker", "buildx", "build", "--file", "Containerfile", "--platform", "linux/amd64",
                "--pull", "--no-cache", "--push", "--metadata-file", metadata,
                "--tag", image + ":" + tag, "--tag", image + ":testing"]
        build_args = {"BASE_IMAGE": BASE + "@" + inputs["base_digest"],
                      "CUSTOM_PACKAGES": packages, "CUSTOM_UPDATE_ID": input_id,
                      "CUSTOM_BUILD_TIME": stamp(started), "CUSTOM_REPOSITORY": repository,
                      "FLCLASH_VERSION": inputs["flclash"]["version"],
                      "FLCLASH_SHA256": inputs["flclash"]["sha256"]}
        for name, value in build_args.items():
            args.extend(["--build-arg", name + "=" + value])
        run(*args, ".")
        digest = json.loads(Path(metadata).read_text())["containerimage.digest"]
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("Build did not return an image digest")
    finished = utcnow()
    return {"input_id": input_id, "digest": digest, "tag": tag,
            "built_at": stamp(finished), "ready_at": stamp(finished + timedelta(days=WAIT_DAYS)),
            "blocked": False,
            "inputs": {**inputs, "rpm": inputs["rpm"]["direct"],
                       "dependency_snapshot": fingerprint(inputs["rpm"]["dependencies"])},
            "run_url": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"}


def summary(state, message):
    stable = state.get("stable")
    lines = ["## Zirconium Custom updates", "", message,
             "", "Stable: " + (stable["tag"] if stable else "No candidate has aged 14 days yet."),
             "", "| Candidate | Built (UTC) | Eligible (UTC) | Blocked |",
             "| --- | --- | --- | --- |"]
    for item in state["candidates"][-20:]:
        lines.append(f"| {item['tag']} | {item['built_at']} | {item['ready_at']} | {item['blocked']} |")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write("\n".join(lines) + "\n")


def main():
    os.chdir(ROOT)
    repository = os.environ["GITHUB_REPOSITORY"].lower()
    image = "ghcr.io/" + repository
    github = GitHub(repository, os.environ["GH_TOKEN"])
    state = github.load_state()
    if os.environ.get("BLOCK_DIGEST"):
        block_candidate(state, os.environ["BLOCK_DIGEST"].strip())
        github.save_state(state)
    # Promotion is independent: constant new releases must not starve older candidates.
    if promote(state, image, utcnow()):
        github.save_state(state)
    try:
        digest = base_digest()
        inputs = {"base_digest": digest, "flclash": flclash_release(github),
                  "rpm": rpm_versions(BASE + "@" + digest, package_names()),
                  "recipe": recipe_hash()}
        check_flclash_version(inputs["flclash"], state)
        input_id = fingerprint(inputs)
        latest = state["candidates"][-1] if state["candidates"] else None
        if latest and latest["input_id"] == input_id and os.environ.get("FORCE_REBUILD") != "true":
            message = "Inputs unchanged; skipped image build."
        else:
            candidate = build_candidate(image, inputs, input_id, repository)
            state["candidates"].append(candidate)
            github.save_state(state)
            message = "Built a candidate; its 14-day waiting period starts after the successful build."
        print(message, flush=True)
        summary(state, message)
    except Exception:
        summary(state, "Detection/build failed. No new candidate was admitted; see the job log.")
        raise


if __name__ == "__main__":
    main()
