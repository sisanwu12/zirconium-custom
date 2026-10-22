from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client import cli, core, local, nix, system, chat
from client.nix_progress import NixProgress, run_nix


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def candidate(age, number=1, **kwargs):
    return {"digest": "sha256:" + f"{number:064x}", "input_id": f"{number:064x}",
            "built_at": (NOW - timedelta(days=age)).isoformat(), "tag": f"candidate-{number}",
            "inputs": {}, **kwargs}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.os = Mock(), Mock(), Mock()
        self.app = core.Change("code", "1", "2")
        self.system = core.Change("system", "old", "new", system=True)
        self.plan = [(self.a, self.app), (self.os, self.system)]
        self.output = patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)

    def test_decline_prevents_every_install_including_system(self):
        with patch.object(cli, "confirm", return_value=False):
            self.assertEqual(cli.execute(self.plan, []), 0)
        self.a.apply.assert_not_called()
        self.os.apply.assert_not_called()

    def test_system_is_last_even_when_plan_order_is_reversed(self):
        events = []
        self.a.apply.side_effect = lambda _: events.append("app")
        self.os.apply.side_effect = lambda _: events.append("system")
        with patch.object(cli, "confirm", side_effect=lambda _: events.append("confirm") or True):
            self.assertEqual(cli.execute(self.plan[::-1], []), 0)
        self.assertEqual(events, ["confirm", "app", "confirm", "system"])

    def test_application_failure_skips_system_but_continues_other_apps(self):
        self.a.apply.side_effect = RuntimeError("download failed")
        with patch.object(cli, "confirm", return_value=True):
            code = cli.execute([*self.plan, (self.b, self.app)], [])
        self.assertEqual(code, 1)
        self.b.apply.assert_called_once()
        self.os.apply.assert_not_called()

    def test_incomplete_detection_prevents_system(self):
        with patch.object(cli, "confirm", return_value=True):
            self.assertEqual(cli.execute([(self.os, self.system)], ["nix"]), 1)
        self.os.apply.assert_not_called()

    def test_system_only_requires_its_own_confirmation(self):
        with patch.object(cli, "confirm", return_value=False) as confirm:
            cli.execute([(self.os, self.system)], [])
        confirm.assert_called_once()
        self.os.apply.assert_not_called()

    def test_noninteractive_input_never_authorizes(self):
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.input") as input_:
            self.assertFalse(core.confirm("install?"))
        input_.assert_not_called()

    def test_check_and_notify_never_apply(self):
        for flags in (["--check"], ["--notify"], ["--check", "--notify"]):
            with tempfile.TemporaryDirectory() as directory, \
                 patch.dict("os.environ", {"XDG_STATE_HOME": directory}), \
                 patch.object(cli.os, "geteuid", return_value=1000), \
                 patch.object(cli, "providers", return_value=([], True)), \
                 patch.object(cli, "collect", return_value=(self.plan, [])), \
                 patch.object(cli, "capture"), patch.object(cli, "execute") as execute:
                self.assertEqual(cli.main([*flags, "--config", directory + "/missing"]), 0)
                execute.assert_not_called()

    def test_selectors_are_exact_deduplicated_and_reject_unknown(self):
        self.assertEqual(cli.selectors(["vscode,xmcl", "code", "wechat"]), ["code", "xmcl", "chat:wechat"])
        for bad in ("--all", "$(touch /tmp/bad)", "home-manager:kitty", "coed"):
            with self.assertRaises(ValueError):
                cli.selectors([bad])

    def test_selected_software_does_not_enroll_unrelated_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            backends, all_mode = cli.providers(["code", "chat:wechat"], Path(directory), {})
        self.assertFalse(all_mode)
        self.assertEqual([b.key for b in backends], ["code", "chat:wechat"])
        self.assertEqual(backends[-1].selected, ["wechat"])


class SystemTests(unittest.TestCase):
    def test_countdown_boundaries_and_blocked(self):
        items = [candidate(13, 1), candidate(1, 2), candidate(12, 3, blocked=True)]
        result = system.pending_candidates({"candidates": items}, NOW, "old")
        self.assertEqual([seconds for _, seconds in result], [86400, 13 * 86400])
        item = candidate(14)
        self.assertEqual(system.pending_candidates({"candidates": [item]}, NOW, "old")[0][1], 0)
        self.assertEqual(system.pending_candidates({"candidates": [item]}, NOW - timedelta(seconds=1), "old")[0][1], 1)

    def test_forged_ready_date_does_not_shorten_wait(self):
        item = candidate(1, ready_at="2000-01-01T00:00:00Z")
        result = system.pending_candidates({"candidates": [item]}, NOW, "old")
        self.assertEqual(result[0][1], 13 * 86400)

    def test_installed_and_older_candidates_do_not_count_down(self):
        a, b, c = candidate(12, 1), candidate(10, 2), candidate(1, 3)
        result = system.pending_candidates({"candidates": [a, b, c]}, NOW, b["input_id"], b["built_at"])
        self.assertEqual([item for item, _ in result], [c])

    def test_stable_not_yet_promoted_is_not_an_install(self):
        item = candidate(20)
        self.assertIsNone(system.notify.available_update({"candidates": [item]}, NOW, "old"))

    def test_blocking_after_preview_prevents_any_sudo(self):
        provider = system.System({"repository": "example/custom"})
        provider.current_id, provider.built_at, provider.digest = "old", "", "old-digest"
        change = core.Change("system", "old", "new", data=candidate(20), system=True)
        with patch.object(system.notify, "fetch_state", return_value={}), \
             patch.object(system, "live") as live:
            with self.assertRaises(RuntimeError):
                provider.apply(change)
        live.assert_not_called()

    def test_pins_reviewed_digest_and_reauthenticates_without_reboot(self):
        item = candidate(30)
        provider = system.System({"repository": "example/custom"})
        provider.current_id, provider.built_at, provider.digest = "old", "", "old-digest"
        provider.staged = None
        with patch.object(system.notify, "fetch_state", return_value={"stable": item}), \
             patch.object(system.notify, "available_update", return_value=item), \
             patch.object(provider, "local_status", return_value="old-digest"), \
             patch.object(system, "capture", return_value='{"status":{"staged":null}}'), \
             patch.object(system, "live") as live, patch("sys.stdout", io.StringIO()):
            provider.apply(core.Change("system", "old", "new", data=item))
        self.assertEqual(live.call_args_list[0].args[0], ["sudo", "-k", "-v"])
        self.assertEqual(live.call_args_list[1].args[0],
                         ["sudo", "bootc", "switch", "ghcr.io/example/custom@" + item["digest"]])

    def test_snapshot_diff_reports_add_remove_and_version(self):
        old = {"rpm": {"a": ["a-1"], "b": ["b-1"]}, "flclash": {"version": "1"}}
        new = {"rpm": {"a": ["a-2"], "c": ["c-1"]}, "flclash": {"version": "2"}}
        output = "\n".join(system.image_changes(old, new))
        self.assertIn("a-1 → a-2", output)
        self.assertIn("b-1 → 移除", output)
        self.assertIn("FlClash: 1 → 2", output)


class DownloadTests(unittest.TestCase):
    def test_complete_local_update_keeps_old_install_and_switches_atomically(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            provider = local.Local("localsend", Path(directory))
            old = provider.base / "LocalSend-1.0.0"
            old.mkdir(parents=True)
            (old / "localsend_app").write_text("old executable")
            (provider.base / "current").symlink_to(old)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                info = tarfile.TarInfo("localsend_app")
                info.mode = 0o755
                info.size = 3
                archive.addfile(info, io.BytesIO(b"new"))
            payload = buffer.getvalue()
            digest = hashlib.sha256(payload).hexdigest()
            with patch.object(core, "request", return_value=self.response(payload)):
                provider.apply(core.Change("localsend", "1.0.0", "2.0.0",
                                           data={"url": "https://example.org/app", "sha256": digest}))
            self.assertEqual(provider.current(), "2.0.0")
            self.assertEqual((provider.base / "current/localsend_app").read_text(), "new")
            self.assertEqual((old / "localsend_app").read_text(), "old executable")

    def response(self, payload, length=None):
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(length if length is not None else len(payload))}
        return response

    def test_truncated_download_and_hash_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            path = Path(directory) / "download"
            for response, sha in ((self.response(b"abc", 100), None), (self.response(b"abc"), "0" * 64)):
                with patch.object(core, "request", return_value=response), self.assertRaises(ValueError):
                    core.download("https://example.org/file", path, "test", sha)

    def test_real_byte_progress_reaches_100_only_after_verification(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch.object(core, "request", return_value=self.response(b"abc")):
            core.download("https://example.org/file", Path(directory) / "download", "test",
                          hashlib.sha256(b"abc").hexdigest())
            self.assertIn("100%", output.getvalue())

    def test_vscode_uses_exact_commit_and_reads_real_installed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = local.Local("code", Path(directory))
            target = provider.base / "code-stable-x64"
            package = target / "VSCode-linux-x64/resources/app/package.json"
            package.parent.mkdir(parents=True)
            package.write_text('{"version":"1.0.0"}')
            (provider.base / "current").symlink_to(target)
            with patch.object(local, "get_json", return_value={"productVersion": "1.1.0", "version": "a" * 40,
                                                               "sha256hash": "b" * 64}):
                change = provider.plan()[0]
            self.assertEqual(change.old, "1.0.0")
            self.assertIn("commit:" + "a" * 40, change.data["url"])
            self.assertNotIn("latest", change.data["url"])

    def test_bad_archive_never_replaces_current(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            provider = local.Local("localsend", Path(directory))
            target = provider.base / "LocalSend-1.0.0"
            target.mkdir(parents=True)
            (provider.base / "current").symlink_to(target)
            def archive(_, path, *args):
                with tarfile.open(path, "w") as tar:
                    info = tarfile.TarInfo("../../escape")
                    info.size = 3
                    tar.addfile(info, io.BytesIO(b"bad"))
                return "a" * 64
            with patch.object(local, "download", side_effect=archive), self.assertRaises(tarfile.FilterError):
                provider.apply(core.Change("localsend", "1.0.0", "2.0.0", data={"url": "https://x", "sha256": "a" * 64}))
            self.assertEqual((provider.base / "current").resolve(), target)
            self.assertFalse((provider.base / "escape").exists())


class HomeManagerTests(unittest.TestCase):
    def test_runtime_drift_prevents_sudo_and_downgrade(self):
        with patch.object(nix, "capture", return_value="nix (Determinate Nix 5.0.0) 2.99"), \
             patch.object(nix, "live") as live, self.assertRaises(RuntimeError):
            nix.NixRuntime().apply(core.Change("nix", "3.0.0", "4.0.0"))
        live.assert_not_called()

    def test_preview_never_changes_original_lock_or_activates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            original = '{"nodes":{"nixpkgs":{"locked":{"rev":"old"}}}}'
            (source / "flake.nix").write_text("{}")
            (source / "flake.lock").write_text(original)
            generation = root / "old-generation"
            generation.mkdir()
            provider = nix.HomeManager(root, {"home_manager_dir": str(source)})
            provider.profile = generation
            def command(args, **kwargs):
                if "update" in args:
                    (provider.snapshot / "flake.lock").write_text('{"nodes":{"nixpkgs":{"locked":{"rev":"new"}}}}')
                    return ""
                if "--raw" in args:
                    return "/nix/store/new-generation"
                if "--references" in args:
                    return "/nix/store/aaa-kitty-1.0"
                return '["/nix/store/bbb-kitty-2.0"]'
            with patch.object(nix, "capture", side_effect=command), patch.object(nix, "live") as live, \
                 patch("sys.stdout", io.StringIO()):
                changes = provider.plan()
            self.assertEqual((source / "flake.lock").read_text(), original)
            self.assertIn("kitty: 1.0 → 2.0", changes[0].details)
            live.assert_not_called()

    def test_source_edit_prevents_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "flake.lock").write_text("old")
            provider = nix.HomeManager(root / "work", {"home_manager_dir": str(source)})
            provider.original_hash = nix.tree_hash(source)
            provider.generation = str(provider.profile.resolve())
            (source / "flake.lock").write_text("new")
            with patch.object(nix, "live") as live, self.assertRaises(RuntimeError):
                provider.apply(core.Change("home-manager", "old", "new"))
            live.assert_not_called()

    def test_failed_build_does_not_activate_or_change_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "flake.lock").write_text("old")
            provider = nix.HomeManager(root / "work", {"home_manager_dir": str(root)})
            provider.ref = "ref"
            with patch.object(provider, "check_unchanged"), \
                 patch.object(nix, "run_nix", side_effect=RuntimeError("build failed")) as live, \
                 patch("sys.stdout", io.StringIO()), self.assertRaises(RuntimeError):
                provider.apply(core.Change("home-manager", "old", "new"))
            self.assertEqual((root / "flake.lock").read_text(), "old")
            live.assert_called_once()

    def test_same_version_rebuild_is_detectable(self):
        a = nix.store_packages(["/nix/store/aaaa-kitty-1.0"])
        b = nix.store_packages(["/nix/store/bbbb-kitty-1.0"])
        self.assertEqual(a["kitty"][0], b["kitty"][0])
        self.assertNotEqual(a["kitty"], b["kitty"])

    def test_outputs_are_merged_instead_of_overwriting_versions(self):
        packages = nix.store_packages(["/nix/store/aaaa-kitty-1.0", "/nix/store/bbbb-kitty-1.0-terminfo",
                                       "/nix/store/cccc-home-manager-path"])
        self.assertEqual(packages["kitty"][0], "1.0")
        self.assertEqual(len(packages["kitty"][1]), 2)
        self.assertNotIn("home-manager-path", packages)


class ProgressTests(unittest.TestCase):
    def test_nix_transfer_uses_byte_counters_and_parent_package(self):
        renderer = NixProgress()
        events = [
            {"action": "start", "id": 1, "type": 100, "fields": ["/nix/store/aaa-kitty-1.0"]},
            {"action": "start", "id": 2, "type": 101, "parent": 1},
            {"action": "result", "id": 2, "type": 105, "fields": [50, 100, 0, 0]},
            {"action": "stop", "id": 2}]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            for event in events:
                renderer.line("@nix " + json.dumps(event))
        self.assertIn("kitty-1.0 下载/写入: 50%", output.getvalue())
        self.assertNotIn("100%", output.getvalue())

    def test_nix_unknown_lines_are_visible(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            renderer = NixProgress()
            renderer.line("real error")
            renderer.line("@nix broken-json")
        self.assertIn("real error", output.getvalue())
        self.assertIn("broken-json", output.getvalue())

    def test_nix_process_failure_is_not_reported_as_success(self):
        process = Mock()
        process.stdout = io.StringIO('@nix {"action":"msg","msg":"build failed"}\n')
        process.wait.return_value = 1
        with patch("client.nix_progress.subprocess.Popen", return_value=process), \
             patch("sys.stdout", new_callable=io.StringIO) as output, self.assertRaises(RuntimeError):
            run_nix(["nix", "build", "example"])
        self.assertNotIn("100%", output.getvalue())


class ChatTests(unittest.TestCase):
    def test_vendor_failure_does_not_discard_other_plans(self):
        good, bad = Mock(), Mock()
        good.key, bad.key = "chat:wechat", "chat:feishu"
        good.available.return_value = bad.available.return_value = True
        good.plan.return_value = [core.Change("chat:wechat", "1", "2")]
        bad.plan.side_effect = RuntimeError("HTTP 403")
        with patch("sys.stdout", io.StringIO()):
            plan, errors = cli.collect([good, bad], True)
        self.assertEqual(len(plan), 1)
        self.assertEqual(errors, ["chat:feishu"])

    def test_apt_dependency_drift_prevents_install(self):
        provider = chat.Chat(["apt"])
        change = core.Change("chat:apt", "old", "new", data={"packages": ["a=2"], "rows": [("a", "1", "2")]})
        with patch.object(chat.source, "in_chat", return_value="Inst a [1] (2 repo)\nInst b (3 repo)"), \
             patch.object(chat, "live") as live, self.assertRaises(RuntimeError):
            provider.apply(change)
        live.assert_not_called()


class InstallerTests(unittest.TestCase):
    def test_installer_keeps_original_and_installs_working_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            (prefix / "bin").mkdir()
            (prefix / "bin/update").write_text("original")
            import os
            env = {**os.environ, "UPDATE_INSTALL_PREFIX": directory}
            if os.geteuid() == 0:
                self.skipTest("user installer intentionally refuses root")
            subprocess.run(["bash", str(root / "install-update.sh")], env=env, check=True, capture_output=True)
            result = subprocess.run([str(prefix / "bin/update"), "--list"], check=True, text=True, capture_output=True)
            self.assertIn("home-manager", result.stdout)
            self.assertEqual((prefix / "bin/update.before-unified-update").read_text(), "original")
            subprocess.run(["bash", str(root / "install-update.sh")], env=env, check=True, capture_output=True)
            self.assertEqual((prefix / "bin/update.before-unified-update").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
