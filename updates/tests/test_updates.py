import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


pipeline = module("pipeline")
notify = module("notify")
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def candidate(age, number, **overrides):
    built = NOW - timedelta(days=age)
    return {"digest": "sha256:" + f"{number:064x}", "input_id": f"{number:064x}",
            "built_at": pipeline.stamp(built),
            "ready_at": pipeline.stamp(built + timedelta(days=14)),
            "tag": "candidate-" + str(number), "blocked": False,
            "inputs": {"flclash": {"version": "0.8.99"}}, **overrides}


def state(*candidates, stable=None):
    return {"schema": 1, "repository": "example/custom", "wait_days": 14,
            "candidates": list(candidates), "stable": stable}


class PromotionTests(unittest.TestCase):
    def test_exact_14_day_boundary(self):
        update = candidate(14, 1)
        data = state(update)
        self.assertIsNone(pipeline.eligible_candidate(data, NOW - timedelta(seconds=1)))
        self.assertEqual(pipeline.eligible_candidate(data, NOW), update)

    def test_frequent_releases_do_not_reset_wait(self):
        old = candidate(15, 1)
        data = state(old, candidate(13, 2), candidate(1, 3))
        self.assertEqual(pipeline.eligible_candidate(data, NOW), old)

    def test_latest_aged_candidate_is_selected(self):
        newest_aged = candidate(14, 3)
        data = state(candidate(30, 1), candidate(20, 2), newest_aged, candidate(2, 4))
        self.assertEqual(pipeline.eligible_candidate(data, NOW), newest_aged)

    def test_blocked_image_is_skipped(self):
        good, bad = candidate(20, 1), candidate(15, 2)
        data = state(good, bad)
        pipeline.block_candidate(data, bad["digest"])
        self.assertEqual(pipeline.eligible_candidate(data, NOW), good)

    def test_does_not_downgrade_or_repromote(self):
        stable = candidate(20, 2)
        self.assertIsNone(pipeline.eligible_candidate(state(candidate(30, 1), stable, stable=stable), NOW))

    def test_cannot_block_an_already_promoted_image(self):
        stable = candidate(20, 1)
        with self.assertRaises(ValueError):
            pipeline.block_candidate(state(stable, stable=stable), stable["digest"])

    def test_promotion_reuses_digest_without_building(self):
        update = candidate(14, 1)
        data = state(update)
        with patch.object(pipeline, "run") as run:
            self.assertTrue(pipeline.promote(data, "ghcr.io/example/custom", NOW))
        args = run.call_args.args
        self.assertEqual(args[:4], ("docker", "buildx", "imagetools", "create"))
        self.assertEqual(args[-1], "ghcr.io/example/custom@" + update["digest"])
        self.assertEqual(data["stable"]["digest"], update["digest"])


class DetectionTests(unittest.TestCase):
    def test_rpm_probe_exports_metadata_without_loading_or_pushing_an_image(self):
        def export(*args):
            self.assertEqual(args[:3], ("docker", "buildx", "build"))
            self.assertNotIn("--load", args)
            self.assertNotIn("--push", args)
            output = Path(args[args.index("--output") + 1].split("dest=", 1)[1])
            output.mkdir()
            (output / "direct.txt").write_text("ZCUSTOM:waydroid|waydroid-0:1.2-1.noarch\n")
            (output / "dependencies.txt").write_text("ZCUSTOM:python3|python3-0:3.14-1.x86_64\n")
        with patch.object(pipeline, "run", side_effect=export):
            versions = pipeline.rpm_versions("base@sha256:" + "a" * 64, ["waydroid"])
        self.assertEqual(versions["direct"]["waydroid"], ["waydroid-0:1.2-1.noarch"])
        self.assertIn("python3", versions["dependencies"])

    def test_amd64_digest_selected_independently_of_other_platforms(self):
        import json
        manifest = {"manifests": [
            {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "amd64"}}]}
        with patch.object(pipeline, "run", return_value=json.dumps(manifest)):
            self.assertEqual(pipeline.base_digest(), "sha256:" + "b" * 64)

    def test_flclash_cannot_silently_downgrade(self):
        previous = candidate(1, 1, inputs={"flclash": {"version": "1.2.3"}})
        with self.assertRaises(ValueError):
            pipeline.check_flclash_version({"version": "1.2.2"}, state(previous))

    def test_upstream_app_and_dependency_changes_affect_fingerprint(self):
        original = {"base_digest": "base-a", "flclash": {"version": "1.0.0"},
                    "rpm": {"direct": {"waydroid": ["1.0"]}, "dependencies": {"library": ["1"]}}}
        for field in ("base", "flclash", "rpm", "dependency"):
            changed = copy.deepcopy(original)
            if field == "base":
                changed["base_digest"] = "base-b"
            elif field == "flclash":
                changed["flclash"]["version"] = "1.0.1"
            elif field == "rpm":
                changed["rpm"]["direct"]["waydroid"] = ["1.1"]
            else:
                changed["rpm"]["dependencies"]["library"] = ["2"]
            self.assertNotEqual(pipeline.fingerprint(original), pipeline.fingerprint(changed))

    def test_package_snapshot_ignores_logs_and_order(self):
        output = "Updating repositories\nZCUSTOM:waydroid|waydroid-0:1.2-1.noarch\n"
        output += "ZCUSTOM:libnotify|libnotify-0:1.0-1.x86_64\n"
        self.assertEqual(pipeline.parse_packages(output), {
            "libnotify": ["libnotify-0:1.0-1.x86_64"], "waydroid": ["waydroid-0:1.2-1.noarch"]})

    def test_unchanged_inputs_skip_actual_build(self):
        inputs = {"base_digest": "base", "flclash": {"version": "1.0.0"},
                  "rpm": {"direct": {}, "dependencies": {}}, "recipe": "recipe"}
        last = candidate(1, 1, input_id=pipeline.fingerprint(inputs))
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/custom", "GH_TOKEN": "test",
                                     "FORCE_REBUILD": "", "BLOCK_DIGEST": ""}), \
             patch.object(pipeline, "GitHub") as github, \
             patch.object(pipeline, "utcnow", return_value=NOW), \
             patch.object(pipeline, "base_digest", return_value="base"), \
             patch.object(pipeline, "flclash_release", return_value=inputs["flclash"]), \
             patch.object(pipeline, "rpm_versions", return_value=inputs["rpm"]), \
             patch.object(pipeline, "recipe_hash", return_value="recipe"), \
             patch.object(pipeline, "build_candidate") as build, \
             patch.object(pipeline, "summary"):
            github.return_value.load_state.return_value = state(last)
            pipeline.main()
            build.assert_not_called()
            github.return_value.save_state.assert_not_called()

    def test_missing_flclash_hash_fails_closed(self):
        class API:
            def api(self, path):
                return {"tag_name": "v1.2.3", "draft": False, "prerelease": False,
                        "assets": [{"name": "FlClash-1.2.3-linux-amd64.rpm", "digest": None}]}
        with self.assertRaises((ValueError, TypeError)):
            pipeline.flclash_release(API())


class NotificationTests(unittest.TestCase):
    def test_notification_cli_only_runs_notify_send(self):
        update = candidate(14, 1)
        with tempfile.TemporaryDirectory() as cache, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": cache}), \
             patch("sys.argv", ["notify.py", "--notify", "--repository", "example/custom"]), \
             patch.object(notify, "fetch_state", return_value=state(update, stable=update)), \
             patch.object(notify, "available_update", return_value=update), \
             patch.object(notify.subprocess, "run") as run:
            notify.main()
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], "notify-send")

    def test_unaged_or_blocked_stable_never_notified(self):
        for update in (candidate(13, 1), candidate(20, 1, blocked=True)):
            self.assertIsNone(notify.available_update(state(update, stable=update), NOW, "old"))

    def test_ready_at_cannot_bypass_wait(self):
        update = candidate(1, 1, ready_at="2020-01-01T00:00:00Z")
        self.assertIsNone(notify.available_update(state(update, stable=update), NOW, "old"))

    def test_installed_version_not_notified(self):
        update = candidate(14, 1)
        self.assertIsNone(notify.available_update(state(update, stable=update), NOW, update["input_id"]))

    def test_newer_installed_testing_image_not_downgraded(self):
        update = candidate(20, 1)
        self.assertIsNone(notify.available_update(state(update, stable=update), NOW, "testing",
                                                 pipeline.stamp(NOW - timedelta(days=1))))

    def test_eligible_update_is_notified(self):
        update = candidate(14, 1)
        self.assertEqual(notify.available_update(state(update, stable=update), NOW, "old"), update)

    def test_repeat_notification_at_most_once_per_day(self):
        previous = {"digest": "a", "date": NOW.date().isoformat()}
        self.assertFalse(notify.should_notify(previous, "a", NOW))
        self.assertTrue(notify.should_notify(previous, "b", NOW))
        self.assertTrue(notify.should_notify(previous, "a", NOW + timedelta(days=1)))


if __name__ == "__main__":
    unittest.main()
