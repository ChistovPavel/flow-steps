"""Behavioral tests, outside the two installable skill directories."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "flow-steps/skills/fs-run/scripts/flow.py"
spec = importlib.util.spec_from_file_location("flow_engine", SCRIPT)
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


def simple_flow(commit=False):
    step = {"prompt": "Do the work", "next": "second"}
    if commit:
        step["commitMessage"] = "Configured message"
    return {"name": "example", "start": "first", "steps": {
        "first": step, "second": {"prompt": "Second task"}}}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flow-steps-кириллица ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Tests never use or modify the developer's Git identity/configuration.
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Flow Test")
        self.git("config", "user.email", "flow@example.invalid")
        self.git("config", "commit.gpgSign", "false")
        self.git("config", "core.autocrlf", "false")
        (self.root / ".gitignore").write_text("/.flows/\n", encoding="utf-8")
        self.write_flow(simple_flow())
        self.commit_setup()

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.root, check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write_flow(self, definition):
        (self.root / "flows").mkdir(exist_ok=True)
        (self.root / "flows/example.yaml").write_text(
            flow.yaml.safe_dump(definition, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def commit_setup(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "Setup")

    def cli(self, *args, data=None, ok=True):
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--project", str(self.root), *args],
                                input=data.encode("utf-8") if data is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        decoded = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0 if ok else 1, (decoded, result.stderr.decode("utf-8", "replace")))
        return json.loads(decoded)

    def start(self):
        created = self.cli("create")
        self.assertEqual(created["action"], "prepare_step")
        return self.cli("begin", "--token", created["token"])

    def state_path(self):
        run_id = (self.root / ".flows/active").read_text().strip()
        return self.root / ".flows/runs" / run_id / "state.yaml"

    def read_state(self):
        return flow.read_yaml(self.state_path())

    def assert_error(self, code, callable, *args):
        with self.assertRaises(flow.FlowError) as caught:
            callable(*args)
        self.assertEqual(caught.exception.code, code)

    def test_initial_inspect_and_empty_step_completion(self):
        self.assertEqual(self.cli("inspect")["flows"], ["example.yaml"])
        started = self.start()
        self.assertEqual(self.read_state()["status"], "in_progress")
        self.assertNotIn("commitMessage", self.read_state()["rendered"])
        next_step = self.cli("confirm", "--token", started["token"])
        self.assertEqual(next_step["step"], "second")
        self.assertNotIn("rendered", self.read_state())
        next_run = self.cli("begin", "--token", next_step["token"])
        self.assertEqual(self.cli("confirm", "--token", next_run["token"])["action"], "completed")
        self.assertFalse((self.root / ".flows/active").exists())
        states = list((self.root / ".flows/runs").glob("*/state.yaml"))
        self.assertTrue(flow.read_yaml(states[0])["completed"])
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), b"1")

    def test_manual_message_is_asked_every_attempt_and_never_stored(self):
        started = self.start()
        (self.root / "результат с пробелами.txt").write_text("result", encoding="utf-8")
        asked = self.cli("confirm", "--token", started["token"])
        self.assertEqual(asked["action"], "ask_commit_message")
        self.assertEqual(self.cli("confirm", "--token", asked["token"])["action"], "ask_commit_message")
        message = "Уникальный заголовок 'quotes' $() `literal`\n\nВторая строка"
        confirmed = self.cli("confirm", "--token", asked["token"], "--message-stdin", data=message)
        self.assertEqual(confirmed["step"], "second")
        for file in (self.root / ".flows").rglob("*"):
            if file.is_file():
                self.assertNotIn(message.encode("utf-8"), file.read_bytes())
                self.assertNotIn("Уникальный".encode("utf-8"), file.read_bytes())
        self.assertEqual(self.git("log", "-1", "--format=%B").stdout.decode("utf-8").strip(), message)
        self.assertEqual(self.git("status", "--porcelain").stdout, b"")

    def test_configured_message_is_rendered_with_local_answer(self):
        definition = simple_flow()
        definition["steps"]["first"].update({
            "ask": [{"name": "ticket", "prompt": "Ticket?", "saveToStore": True},
                    {"name": "feedback", "prompt": "Feedback?"}],
            "prompt": "{{ticket}} / {{feedback}}",
            "commitMessage": "{{ticket}}: {{feedback}}"})
        self.write_flow(definition)
        self.commit_setup()
        created = self.cli("create")
        answers = self.root / ".flows/answers.json"
        answers.write_text(json.dumps({"ticket": "P-42", "feedback": "Fix {{literal}}"}), encoding="utf-8")
        started = self.cli("begin", "--token", created["token"], "--answers", str(answers))
        self.assertEqual(started["prompt"], "P-42 / Fix {{literal}}")
        self.assertEqual(self.read_state()["vars"], {"ticket": "P-42"})
        self.assertEqual(self.read_state()["rendered"]["commitMessage"], "P-42: Fix {{literal}}")
        (self.root / "result").write_text("done")
        self.cli("confirm", "--token", started["token"])
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), b"P-42: Fix {{literal}}")

    def test_missing_variable_does_not_start_or_save_partial_answers(self):
        definition = simple_flow()
        definition["steps"]["first"]["prompt"] = "{{missing}}"
        self.write_flow(definition)
        self.commit_setup()
        created = self.cli("create")
        before = self.state_path().read_bytes()
        self.assertEqual(self.cli("begin", "--token", created["token"], ok=False)["code"], "missing_variable")
        self.assertEqual(self.state_path().read_bytes(), before)

    def test_dirty_tree_blocks_new_step_but_not_retry(self):
        created = self.cli("create")
        (self.root / "untracked").write_text("existing")
        self.assertEqual(self.cli("begin", "--token", created["token"], ok=False)["code"], "dirty_tree")
        (self.root / "untracked").unlink()
        started = self.cli("begin", "--token", created["token"])
        (self.root / "untracked").write_text("result")
        self.assertEqual(self.cli("inspect")["action"], "review_step")
        repeated = self.cli("retry", "--token", started["token"], "--mode", "same")
        self.assertEqual(repeated["prompt"], started["prompt"])
        self.assertEqual((self.root / "untracked").read_text(), "result")

    def test_new_data_retry_asks_and_updates_only_saved_variables(self):
        definition = simple_flow()
        definition["steps"]["first"].update({"prompt": "{{value}}/{{local}}", "ask": [
            {"name": "value", "prompt": "Value?", "saveToStore": True},
            {"name": "local", "prompt": "Local?"}]})
        self.write_flow(definition)
        self.commit_setup()
        created = self.cli("create")
        engine = flow.Engine(self.root)
        with engine.lock():
            first = engine.begin(created["token"], {"value": "one", "local": "old"})
        (self.root / "partial").write_text("partial result")
        view = self.cli("inspect")
        self.assertEqual(len(view["questions"]), 2)
        with engine.lock():
            second = engine.begin(first["token"], {"value": "two", "local": "new"}, "new")
        self.assertEqual(second["prompt"], "two/new")
        self.assertEqual(self.read_state()["vars"], {"value": "two"})

    def test_decision_chain_and_cycle_each_require_new_run(self):
        definition = {"name": "branch", "start": "a", "steps": {"work": {"prompt": "Work", "next": "a"}},
                      "decisions": {"a": {"question": "A?", "options": [{"label": "B", "goto": "b"}]},
                                    "b": {"question": "B?", "options": [{"label": "Work", "goto": "work"}]}}}
        self.write_flow(definition)
        self.commit_setup()
        (self.root / "dirty").write_text("unrelated")
        first = self.cli("create")
        self.assertEqual(first["action"], "answer_decision")
        picked = self.cli("choose", "--token", first["token"], "--option", "1")
        self.assertEqual(picked["action"], "stop")
        second = self.cli("inspect")
        self.assertEqual(second["question"], "B?")
        picked = self.cli("choose", "--token", second["token"], "--option", "1")
        self.assertEqual(picked["action"], "stop")
        self.assertEqual(self.cli("inspect", ok=False)["code"], "dirty_tree")
        (self.root / "dirty").unlink()
        started = self.cli("begin", "--token", picked["token"])
        self.cli("confirm", "--token", started["token"])
        self.assertEqual(self.cli("inspect")["question"], "A?")

    def test_stale_token_cannot_confirm_another_step(self):
        started = self.start()
        self.cli("confirm", "--token", started["token"])
        self.assertEqual(self.cli("confirm", "--token", started["token"], ok=False)["code"], "stale_token")
        current = self.cli("inspect")
        self.assertEqual(self.cli("confirm", "--token", current["token"], ok=False)["code"], "not_started")

    def test_failed_commit_does_not_save_message_or_advance(self):
        started = self.start()
        (self.root / "result").write_text("done")
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        failed = self.cli("confirm", "--token", started["token"], "--message-stdin", data="DO NOT SAVE ME", ok=False)
        self.assertEqual(failed["code"], "commit_failed")
        state = self.read_state()
        self.assertEqual(state["current"], "first")
        self.assertEqual(state["status"], "in_progress")
        self.assertNotIn("DO NOT SAVE ME", self.state_path().read_text(encoding="utf-8"))
        inspected = self.cli("inspect")
        self.assertEqual(self.cli("confirm", "--token", inspected["token"])["action"], "ask_commit_message")
        hook.unlink()

    def test_snapshot_survives_source_change_and_detects_snapshot_change(self):
        created = self.cli("create")
        definition = simple_flow()
        definition["steps"]["first"]["prompt"] = "Changed source"
        self.write_flow(definition)
        self.commit_setup()
        started = self.cli("begin", "--token", created["token"])
        self.assertEqual(started["prompt"], "Do the work")
        snapshot = self.state_path().parent / "flow.yaml"
        snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n# changed", encoding="utf-8")
        self.assertEqual(self.cli("inspect", ok=False)["code"], "changed_snapshot")

    def test_no_state_ignore_is_an_explicit_error(self):
        (self.root / ".gitignore").write_text("")
        self.assertEqual(self.cli("create", ok=False)["code"], "state_not_ignored")
        self.assertFalse((self.root / ".flows").exists())

    def test_rename_staged_and_untracked_changes_are_listed(self):
        (self.root / "old.txt").write_text("tracked")
        self.commit_setup()
        self.start()
        self.git("mv", "old.txt", "new.txt")
        (self.root / "новый.txt").write_text("new", encoding="utf-8")
        files = self.cli("inspect")["files"]
        self.assertTrue(any(item.get("previous_path") == "old.txt" for item in files))
        self.assertTrue(any(item["path"] == "новый.txt" for item in files))

    def test_state_write_failure_never_returns_execute_step(self):
        created = self.cli("create")
        engine = flow.Engine(self.root)
        with engine.lock(), patch.object(flow, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                engine.begin(created["token"], {})
        self.assertEqual(self.read_state()["status"], "not_started")

    def test_commit_then_interruption_recovers_without_duplicate_commit(self):
        started = self.start()
        (self.root / "result").write_text("done")
        engine = flow.Engine(self.root)
        original_save = engine.save
        calls = 0

        def save():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("interruption after Git commit before receipt")
            original_save()

        with engine.lock(), patch.object(engine, "save", side_effect=save):
            with self.assertRaises(OSError):
                engine.confirm(started["token"], "Ephemeral recovery text")
        before = self.git("rev-list", "--count", "HEAD").stdout
        view = self.cli("inspect")
        self.assertEqual(view["action"], "recover_confirmation")
        recovered = self.cli("confirm", "--token", view["token"])
        self.assertEqual(recovered["step"], "second")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout, before)
        for file in (self.root / ".flows").rglob("*"):
            if file.is_file():
                self.assertNotIn(b"Ephemeral recovery text", file.read_bytes())

    def test_interruption_before_git_commit_requires_new_message(self):
        started = self.start()
        (self.root / "result").write_text("done")
        engine = flow.Engine(self.root)
        original_git = engine.git

        def git(*args, **kwargs):
            if "commit" in args:
                raise OSError("interrupted before commit")
            return original_git(*args, **kwargs)

        with engine.lock(), patch.object(engine, "git", side_effect=git):
            with self.assertRaises(OSError):
                engine.confirm(started["token"], "lost message")
        view = self.cli("inspect")
        asked = self.cli("confirm", "--token", view["token"])
        self.assertEqual(asked["action"], "ask_commit_message")
        self.assertNotIn("lost message", self.state_path().read_text(encoding="utf-8"))

    def test_concurrent_mutation_is_rejected_and_lock_releases(self):
        engine = flow.Engine(self.root)
        with engine.lock():
            self.assertEqual(self.cli("create", ok=False)["code"], "busy")
        self.assertEqual(self.cli("create")["action"], "prepare_step")

    def test_active_run_is_not_overwritten(self):
        created = self.cli("create")
        self.assertEqual(self.cli("create", ok=False)["code"], "active_run_exists")
        self.assertEqual(self.cli("inspect")["token"], created["token"])

    def test_ambiguous_recovery_does_not_advance(self):
        self.start()
        (self.root / "result").write_text("expected")
        engine = flow.Engine(self.root)
        with engine.lock():
            engine.load()
            base = engine.head()
            engine.git("add", "--all")
            tree = engine.git("write-tree").stdout.decode().strip()
            engine.state["pendingCommit"] = {"head": base, "tree": tree}
            engine.save()
        # A different tree is committed outside the interrupted operation.
        (self.root / "result").write_text("unexpected")
        self.commit_setup()
        inspected = self.cli("inspect")
        failed = self.cli("confirm", "--token", inspected["token"], ok=False)
        self.assertEqual(failed["code"], "ambiguous_commit")
        self.assertEqual(self.read_state()["current"], "first")

    def test_successful_commit_receipt_recovers_after_transition_failure(self):
        started = self.start()
        (self.root / "result").write_text("done")
        engine = flow.Engine(self.root)
        with engine.lock(), patch.object(engine, "advance", side_effect=OSError("interrupted transition")):
            with self.assertRaises(OSError):
                engine.confirm(started["token"], "Receipt test")
        self.assertIn("commit", self.read_state()["pendingCommit"])
        inspected = self.cli("inspect")
        recovered = self.cli("confirm", "--token", inspected["token"])
        self.assertEqual(recovered["step"], "second")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), b"2")

    def test_completed_pointer_cleanup_is_recoverable(self):
        definition = simple_flow()
        del definition["steps"]["first"]["next"]
        self.write_flow(definition)
        self.commit_setup()
        started = self.start()
        engine = flow.Engine(self.root)
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "active":
                raise OSError("interrupted pointer cleanup")
            return original_unlink(path, *args, **kwargs)

        with engine.lock(), patch.object(Path, "unlink", new=unlink):
            with self.assertRaises(OSError):
                engine.confirm(started["token"])
        inspected = self.cli("inspect")
        self.assertEqual(inspected["action"], "recover_confirmation")
        self.assertTrue(inspected["completed"])
        self.assertEqual(self.cli("confirm", "--token", inspected["token"])["action"], "completed")
        self.assertFalse((self.root / ".flows/active").exists())

    def test_corrupt_pending_receipt_returns_structured_error(self):
        self.start()
        state = self.read_state()
        state["pendingCommit"] = {"head": "invalid"}
        self.state_path().write_text(flow.yaml.safe_dump(state), encoding="utf-8")
        self.assertEqual(self.cli("inspect", ok=False)["code"], "invalid_state")

    def test_negated_ignore_cannot_leak_state_into_git(self):
        self.start()
        # A partial pattern can ignore .probe but fail to hide another state file.
        (self.root / ".gitignore").write_text("/.flows/*\n!/.flows/leak.txt\n", encoding="utf-8")
        (self.root / ".flows/leak.txt").write_text("state data")
        self.assertEqual(self.cli("inspect", ok=False)["code"], "state_not_ignored")

    def test_interrupted_lock_is_released_by_operating_system(self):
        program = (
            "import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('engine',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "e=m.Engine(sys.argv[2]); lock=e.lock(); lock.__enter__(); "
            "print('locked',flush=True); sys.stdin.read()")
        process = subprocess.Popen([sys.executable, "-B", "-c", program, str(SCRIPT), str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stdout.readline().strip(), b"locked")
            process.kill()
            process.wait(timeout=10)
            self.assertEqual(self.cli("create")["action"], "prepare_step")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


class ValidationTests(unittest.TestCase):
    def test_invalid_references_and_duplicate_node_names(self):
        definition = simple_flow()
        definition["steps"]["first"]["next"] = "missing"
        with self.assertRaises(flow.FlowError):
            flow.validate_flow(definition)
        definition = simple_flow()
        definition["decisions"] = {"first": {"question": "?", "options": []}}
        with self.assertRaises(flow.FlowError):
            flow.validate_flow(definition)

    def test_duplicate_yaml_keys_are_rejected(self):
        with self.assertRaises(flow.FlowError):
            flow.yaml.load("a: one\na: two\n", Loader=flow.StrictLoader)

    def test_python_yaml_tags_are_rejected(self):
        with self.assertRaises(flow.yaml.YAMLError):
            flow.yaml.load("!!python/object/apply:os.system ['echo unexpected']", Loader=flow.StrictLoader)

    def test_local_value_overrides_store_without_recursive_rendering(self):
        values = {**{"x": "saved", "y": "expanded"}, **{"x": "{{y}}"}}
        self.assertEqual(flow.render("{{x}}", values), "{{y}}")

    def test_vendored_package_integrity(self):
        import hashlib
        vendor = SCRIPT.parent / "vendor"
        manifest = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
        for path, expected in manifest["files"].items():
            self.assertEqual(hashlib.sha256((vendor / path).read_bytes()).hexdigest(), expected)

    def test_shipped_example_is_valid_and_skill_frontmatter_is_minimal(self):
        package = SCRIPT.parents[2]
        definition = flow.validate_flow(flow.read_yaml(package / "fs-run/templates/example-flow.yaml"))
        self.assertEqual(definition["steps"]["design_fix"]["next"], "design_review")
        self.assertNotIn("commitMessage", definition["steps"]["design_fix"])
        for name in ("fs-run", "fs-confirm"):
            text = (package / name / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = flow.yaml.load(text.split("---", 2)[1], Loader=flow.StrictLoader)
            self.assertEqual(frontmatter["name"], name)
            self.assertEqual(set(frontmatter), {"name", "description"})


if __name__ == "__main__":
    unittest.main()
