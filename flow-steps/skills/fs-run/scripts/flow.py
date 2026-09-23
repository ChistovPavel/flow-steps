#!/usr/bin/env python3
"""Local flow state machine. Python 3.11+, Git, and bundled PyYAML only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True
VENDOR = Path(__file__).resolve().parent / "vendor/yaml"
spec = importlib.util.spec_from_file_location(
    "_flow_steps_yaml", VENDOR / "__init__.py", submodule_search_locations=[str(VENDOR)])
yaml = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = yaml
spec.loader.exec_module(yaml)

VERSION = 1
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class FlowError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def require(condition, code, message, **details):
    if not condition:
        raise FlowError(code, message, **details)


class StrictLoader(yaml.SafeLoader):
    """Safe YAML with duplicate keys and merge keys rejected."""

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str), "invalid_yaml", "YAML keys must be strings.")
            require(key not in result, "duplicate_key", f"Duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def read_yaml(path):
    try:
        return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise FlowError("invalid_yaml", f"Invalid YAML in {path.name}: {exc}") from exc


def mapping(value, label):
    require(isinstance(value, dict), "invalid_schema", f"{label} must be a mapping.")
    return value


def string(value, label):
    require(isinstance(value, str) and bool(value.strip()), "invalid_schema",
            f"{label} must be a non-empty string.")
    return value


def fields(value, allowed, label):
    mapping(value, label)
    require(not (value.keys() - allowed), "invalid_schema", f"Unknown fields in {label}.",
            fields=sorted(value.keys() - allowed))


def template_names(template):
    remainder = PLACEHOLDER.sub("", template)
    require("{{" not in remainder and "}}" not in remainder, "invalid_template",
            "Invalid placeholder; use {{variable_name}}.")
    return set(PLACEHOLDER.findall(template))


def validate_flow(flow):
    fields(flow, {"name", "start", "steps", "decisions"}, "flow")
    string(flow.get("name"), "name")
    string(flow.get("start"), "start")
    steps = mapping(flow.get("steps", {}), "steps")
    decisions = mapping(flow.get("decisions", {}), "decisions")
    require(not (steps.keys() & decisions.keys()), "invalid_schema",
            "Step and decision identifiers must be unique.")
    nodes = set(steps) | set(decisions)
    require(flow["start"] in nodes, "invalid_reference", "Unknown start node.")
    for node_id in nodes:
        string(node_id, "node identifier")
    for name, step in steps.items():
        fields(step, {"skill", "ask", "prompt", "commitMessage", "next"}, f"step {name}")
        template_names(string(step.get("prompt"), f"{name}.prompt"))
        for key in ("skill", "commitMessage", "next"):
            if key in step:
                string(step[key], f"{name}.{key}")
        if "commitMessage" in step:
            template_names(step["commitMessage"])
        if "next" in step:
            require(step["next"] in nodes, "invalid_reference", f"Unknown next in {name}.")
        questions = step.get("ask", [])
        require(isinstance(questions, list), "invalid_schema", f"{name}.ask must be a list.")
        seen = set()
        for question in questions:
            fields(question, {"name", "prompt", "saveToStore"}, f"{name}.ask item")
            key = string(question.get("name"), "ask.name")
            require(bool(VARIABLE.fullmatch(key)) and key not in seen, "invalid_schema",
                    "Question names must be unique variable identifiers within a step.")
            seen.add(key)
            string(question.get("prompt"), "ask.prompt")
            require(type(question.get("saveToStore", False)) is bool, "invalid_schema",
                    "saveToStore must be a boolean.")
    for name, decision in decisions.items():
        fields(decision, {"question", "options"}, f"decision {name}")
        string(decision.get("question"), f"{name}.question")
        options = decision.get("options")
        require(isinstance(options, list) and bool(options), "invalid_schema",
                f"{name}.options must be a non-empty list.")
        for option in options:
            fields(option, {"label", "goto"}, f"{name}.option")
            string(option.get("label"), "option.label")
            destination = string(option.get("goto"), "option.goto")
            require(destination in nodes, "invalid_reference", f"Unknown goto in {name}.")
    return flow


def render(template, values):
    missing = template_names(template) - values.keys()
    require(not missing, "missing_variable", "Missing template variables.", variables=sorted(missing))
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Engine:
    def __init__(self, project):
        self.root = Path(project).resolve()
        require(self.root.is_dir(), "invalid_project", "Project directory does not exist.")
        top = self.git("rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()
        require(Path(top).resolve() == self.root, "invalid_project",
                "Pass the Git working-tree root as --project.")
        self.home = self.root / ".flows"
        require(self.home.resolve() == self.home,
                "invalid_state_path", ".flows must be a real directory inside the project.")

    def git(self, *arguments, data=None, check=True):
        process = subprocess.run(["git", *arguments], cwd=self.root, input=data,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        if check and process.returncode:
            raise FlowError("git_error", "Git operation failed.",
                            operation=arguments[0], stderr=process.stderr.decode("utf-8", "replace").strip())
        return process

    def storage_ready(self):
        tracked = self.git("ls-files", "-z", "--", ".flows").stdout
        require(not tracked, "tracked_state", ".flows must not contain tracked files.")
        ignored = self.git("check-ignore", "--no-index", "-q", "--", ".flows/.probe", check=False)
        require(ignored.returncode == 0, "state_not_ignored",
                "Add /.flows/ to .gitignore and commit setup files before running a step.")
        visible = self.git("ls-files", "--others", "--exclude-standard", "-z", "--", ".flows").stdout
        require(not visible, "state_not_ignored", "All files under .flows/ must be ignored by Git.")

    @contextmanager
    def lock(self):
        self.storage_ready()
        self.home.mkdir(exist_ok=True)
        with (self.home / "lock").open("a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise FlowError("busy", "Another flow operation is running. Try again after it finishes.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def changes(self):
        raw = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        entries = iter(raw.split(b"\0"))
        result = []
        for entry in entries:
            if not entry:
                continue
            item = {"status": entry[:2].decode("ascii"), "path": entry[3:].decode("utf-8", "replace")}
            if "R" in item["status"] or "C" in item["status"]:
                item["previous_path"] = next(entries).decode("utf-8", "replace")
            result.append(item)
        return result

    def clean(self):
        changes = self.changes()
        require(not changes, "dirty_tree", "Commit or resolve existing changes before starting a new step.",
                files=changes)

    def load(self):
        pointer = self.home / "active"
        if not pointer.exists():
            return None
        require(pointer.resolve() == pointer, "invalid_state_path", "Active pointer must not be a symlink.")
        run_id = pointer.read_text(encoding="utf-8").strip()
        require(bool(re.fullmatch(r"[0-9a-f]{32}", run_id)), "invalid_state", "Invalid active run identifier.")
        self.directory = self.home / "runs" / run_id
        require(self.directory.resolve() == self.directory, "invalid_state_path", "Run path must not use symlinks.")
        self.state_path = self.directory / "state.yaml"
        require(self.state_path.resolve() == self.state_path and
                (self.directory / "flow.yaml").resolve() == self.directory / "flow.yaml",
                "invalid_state_path", "Run files must not be symlinks.")
        state = mapping(read_yaml(self.state_path), "state")
        require(state.get("schemaVersion") == VERSION and state.get("runId") == run_id,
                "invalid_state", "Unsupported or mismatched run state.")
        require(type(state.get("revision")) is int and state["revision"] >= 0,
                "invalid_state", "Invalid state revision.")
        flow_bytes = (self.directory / "flow.yaml").read_bytes()
        require(hashlib.sha256(flow_bytes).hexdigest() == state.get("flowHash"),
                "changed_snapshot", "The run's flow snapshot was modified.")
        self.flow = validate_flow(read_yaml(self.directory / "flow.yaml"))
        self.state = state
        nodes = set(self.flow.get("steps", {})) | set(self.flow.get("decisions", {}))
        current = string(state.get("current"), "state.current")
        status = string(state.get("status"), "state.status")
        require(current in nodes and status in {"not_started", "in_progress"},
                "invalid_state", "Invalid current node or status.")
        require(type(state.get("completed", False)) is bool, "invalid_state", "completed must be a boolean.")
        values = mapping(state.get("vars"), "state.vars")
        require(all(isinstance(v, str) for v in values.values()), "invalid_state", "Variables must be strings.")
        if state["current"] in self.flow.get("decisions", {}):
            require(state["status"] == "not_started", "invalid_state", "A decision cannot be in progress.")
        if state["status"] == "in_progress":
            rendered = mapping(state.get("rendered"), "state.rendered")
            string(rendered.get("prompt"), "rendered.prompt")
            step = self.flow["steps"][state["current"]]
            require(("commitMessage" in rendered) == ("commitMessage" in step),
                    "invalid_state", "Rendered commit message must come only from the step definition.")
            if "commitMessage" in rendered:
                string(rendered["commitMessage"], "rendered.commitMessage")
        if "pendingCommit" in state:
            pending = state["pendingCommit"]
            fields(pending, {"head", "tree", "commit"}, "pendingCommit")
            sha = lambda value: isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value))
            require("head" in pending and (pending["head"] is None or sha(pending["head"])) and
                    sha(pending.get("tree")) and ("commit" not in pending or sha(pending["commit"])),
                    "invalid_state", "Invalid pending commit identifiers.")
            require(status == "in_progress" and not state.get("completed"),
                    "invalid_state", "Pending commit requires an unconfirmed work step.")
        return state

    def token(self):
        return f'{self.state["runId"]}:{self.state["revision"]}'

    def save(self):
        self.state["revision"] += 1
        atomic_write(self.state_path, yaml.safe_dump(self.state, allow_unicode=True, sort_keys=False))

    def check_token(self, token):
        require(self.load() is not None, "no_active_run", "No active run. Invoke the run skill first.")
        require(token == self.token(), "stale_token", "State changed. Inspect again before acting.")

    def response(self, action, **data):
        return {"action": action, "runId": self.state["runId"], "step": self.state["current"],
                "token": self.token(), **data}

    def view(self):
        if self.state.get("completed"):
            return self.response("recover_confirmation", completed=True,
                                 instruction="Flow completed; call confirm to finish interrupted active-pointer cleanup, then stop.")
        if self.state.get("pendingCommit"):
            return self.response("recover_confirmation", instruction="Recover the interrupted confirmation with confirm; do not execute the step again.")
        node_id = self.state["current"]
        if node_id in self.flow.get("decisions", {}):
            node = self.flow["decisions"][node_id]
            return self.response("answer_decision", question=node["question"],
                                 options=[{"id": i + 1, "label": option["label"]}
                                          for i, option in enumerate(node["options"])])
        step = self.flow["steps"][node_id]
        if self.state["status"] == "in_progress":
            return self.response("review_step", prompt=self.state["rendered"]["prompt"],
                                 skill=step.get("skill"), questions=step.get("ask", []),
                                 files=self.changes(), choices=["confirm", "retry_same", "retry_new"])
        self.clean()
        return self.response("prepare_step", skill=step.get("skill"), questions=step.get("ask", []),
                             instruction="Check skill availability, ask every question in order, then call begin. Do not execute work yet.")

    def inspect(self):
        self.storage_ready()
        if self.load() is None:
            directory = self.root / "flows"
            paths = sorted(directory.glob("*.yaml")) if directory.exists() else []
            return {"action": "choose_flow" if paths else "no_flows", "flows": [path.name for path in paths],
                    "instruction": "If there is one flow, create it; otherwise ask the user to choose. Never invent a flow."}
        return self.view()

    def create(self, filename=None):
        active = self.load()
        if active and not active.get("completed"):
            raise FlowError("active_run_exists", "An active run already exists. Inspect it.")
        if active:
            (self.home / "active").unlink()
        candidates = sorted((self.root / "flows").glob("*.yaml"))
        if filename is None:
            require(len(candidates) == 1, "choose_flow", "Choose a flow filename.",
                    flows=[p.name for p in candidates])
            source = candidates[0]
        else:
            require(filename in [p.name for p in candidates], "unknown_flow", "Choose a file from flows/*.yaml.")
            source = self.root / "flows" / filename
        require(source.resolve().is_relative_to(self.root), "invalid_flow_path", "Flow must be inside the project.")
        flow = validate_flow(read_yaml(source))
        run_id = uuid.uuid4().hex
        self.directory = self.home / "runs" / run_id
        self.directory.mkdir(parents=True)
        self.state_path = self.directory / "state.yaml"
        # Save canonical YAML so the snapshot and its hash describe the same validated definition.
        snapshot = yaml.safe_dump(flow, allow_unicode=True, sort_keys=False)
        atomic_write(self.directory / "flow.yaml", snapshot)
        self.flow = flow
        self.state = {"schemaVersion": VERSION, "runId": run_id, "revision": 0,
                      "flow": flow["name"], "flowHash": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
                      "current": flow["start"], "status": "not_started", "vars": {}}
        self.save()
        atomic_write(self.home / "active", run_id + "\n")
        return self.view()

    def choose(self, token, option):
        self.check_token(token)
        require(not self.state.get("completed"), "completed", "Run is already completed.")
        node = self.flow.get("decisions", {}).get(self.state["current"])
        require(node is not None, "wrong_node", "Current node is not a decision.")
        require(type(option) is int and 1 <= option <= len(node["options"]), "invalid_option", "Choose a listed option id.")
        self.state["current"] = node["options"][option - 1]["goto"]
        self.state["status"] = "not_started"
        self.state.pop("rendered", None)
        self.save()
        return self.response("stop", instruction="Choice saved. Ask the user to invoke the run skill again. Do not process the next node.")

    def begin(self, token, answers, retry=None):
        self.check_token(token)
        require(not self.state.get("completed") and not self.state.get("pendingCommit"),
                "confirmation_pending", "Inspect the run and resolve its confirmation first.")
        step = self.flow.get("steps", {}).get(self.state["current"])
        require(step is not None, "wrong_node", "Current node is not a work step.")
        expected = "in_progress" if retry else "not_started"
        require(self.state["status"] == expected, "wrong_status", "Inspect before starting or retrying a step.")
        if not retry:
            self.clean()
        if retry != "same":
            questions = step.get("ask", [])
            require(isinstance(answers, dict) and set(answers) == {q["name"] for q in questions},
                    "invalid_answers", "Provide exactly one answer for each ask entry.")
            require(all(isinstance(value, str) for value in answers.values()),
                    "invalid_answers", "Every answer must be a string.")
            values = {**self.state["vars"], **answers}
            rendered = {"prompt": render(step["prompt"], values)}
            string(rendered["prompt"], "rendered.prompt")
            if "commitMessage" in step:
                rendered["commitMessage"] = render(step["commitMessage"], values)
                string(rendered["commitMessage"], "rendered.commitMessage")
            for question in questions:
                if question.get("saveToStore", False):
                    self.state["vars"][question["name"]] = answers[question["name"]]
            self.state["rendered"] = rendered
        else:
            require(not answers, "invalid_answers", "Same-data retry does not accept new answers.")
        self.state["status"] = "in_progress"
        self.save()
        return self.response("execute_step", skill=step.get("skill"), prompt=self.state["rendered"]["prompt"],
                             state_saved=True, after_execution="Stop. Present the result and ask the user to invoke the confirmation skill. No state update is needed.")

    def head(self):
        result = self.git("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.decode("ascii").strip() if result.returncode == 0 else None

    def advance(self):
        step = self.flow["steps"][self.state["current"]]
        self.state.pop("pendingCommit", None)
        self.state.pop("rendered", None)
        self.state["status"] = "not_started"
        if "next" in step:
            self.state["current"] = step["next"]
            self.save()
            return self.response("stop", instruction="Step confirmed. Invoke the run skill for the next node; a new session is recommended for a work step.")
        self.state["completed"] = True
        self.save()
        (self.home / "active").unlink(missing_ok=True)
        return self.response("completed", instruction="Flow completed. Stop.")

    def recover_commit(self):
        pending = self.state["pendingCommit"]
        current = self.head()
        if current == pending["head"] and "commit" not in pending:
            self.state.pop("pendingCommit")
            self.save()
            return False
        require(current is not None, "ambiguous_commit", "Cannot establish the interrupted commit result.")
        if "commit" in pending:
            known = current == pending["commit"]
        else:
            parent_line = self.git("rev-list", "--parents", "-n", "1", current).stdout.decode("ascii").split()
            parents = parent_line[1:]
            expected_parents = [pending["head"]] if pending["head"] else []
            tree = self.git("rev-parse", f"{current}^{{tree}}").stdout.decode("ascii").strip()
            known = parents == expected_parents and tree == pending["tree"]
        require(known, "ambiguous_commit", "HEAD does not match the interrupted commit. Resolve manually; no transition was made.")
        require(not self.changes(), "post_commit_changes", "The commit exists, but the working tree has changes. Resolve them before confirming again.")
        return True

    def confirm(self, token, message=None):
        self.check_token(token)
        if self.state.get("completed"):
            (self.home / "active").unlink(missing_ok=True)
            return self.response("completed", instruction="Flow completed. Stop.")
        require(self.state["current"] in self.flow.get("steps", {}) and self.state["status"] == "in_progress",
                "not_started", "Start a work step with the run skill before confirming it.")
        if self.state.get("pendingCommit") and self.recover_commit():
            return self.advance()
        if not self.changes():
            return self.advance()
        rendered = self.state["rendered"]
        if "commitMessage" in rendered:
            message = rendered["commitMessage"]
        elif message is None:
            return self.response("ask_commit_message", instruction="Ask the user for a non-empty commit message. Pass it to confirm via stdin for this attempt only. Do not save it.")
        require(isinstance(message, str) and bool(message.strip()) and "\0" not in message,
                "invalid_commit_message", "Provide a non-empty commit message without NUL characters.")
        before = self.head()
        self.git("add", "--all", "--", ".")
        tree = self.git("write-tree").stdout.decode("ascii").strip()
        self.state["pendingCommit"] = {"head": before, "tree": tree}
        self.save()
        # User-supplied message is never assigned to state, written to a file by this
        # program, or added to process arguments. Git itself stores commit metadata.
        result = self.git("-c", "i18n.commitEncoding=utf-8", "commit", "--cleanup=verbatim", "-F", "-",
                          data=message.encode("utf-8"), check=False)
        if result.returncode:
            if self.head() == before:
                self.state.pop("pendingCommit")
                self.save()
            raise FlowError("commit_failed", "Git commit failed. Inspect again. If the step has no commitMessage and changes remain, ask for a new message.",
                            stderr=result.stderr.decode("utf-8", "replace").strip())
        committed = self.head()
        require(committed is not None and committed != before, "ambiguous_commit", "Git returned success without an identifiable new commit.")
        self.state["pendingCommit"]["commit"] = committed
        self.save()
        require(not self.changes(), "post_commit_changes", "Commit created, but changes remain. Resolve them before confirming again.")
        return self.advance()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise FlowError("invalid_arguments", message)


def parse_args(argv):
    parser = Parser(description=__doc__)
    parser.add_argument("--project", required=True, help="Git working-tree root")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("inspect")
    commands.add_parser("create").add_argument("--flow", help="Filename from flows/*.yaml")
    for name in ("choose", "begin", "retry", "confirm"):
        command = commands.add_parser(name)
        command.add_argument("--token", required=True, help="Exact token from the most recent response")
        if name == "choose":
            command.add_argument("--option", type=int, required=True)
        if name in {"begin", "retry"}:
            command.add_argument("--answers", type=Path, help="UTF-8 JSON object; omitted means {}")
        if name == "retry":
            command.add_argument("--mode", choices=("same", "new"), required=True)
        if name == "confirm":
            command.add_argument("--message-stdin", action="store_true", help="Read a one-use commit message from stdin")
    return parser.parse_args(argv)


def execute(args):
    engine = Engine(args.project)
    if args.command == "inspect":
        return engine.inspect()
    # Read stdin before taking the short operation lock. This is data transport,
    # not an interactive prompt; the calling agent must supply and close stdin.
    message = sys.stdin.read() if args.command == "confirm" and args.message_stdin else None
    answers = {}
    if getattr(args, "answers", None):
        answers = json.loads(args.answers.read_text(encoding="utf-8-sig"))
    with engine.lock():
        if args.command == "create":
            return engine.create(args.flow)
        if args.command == "choose":
            return engine.choose(args.token, args.option)
        if args.command in {"begin", "retry"}:
            return engine.begin(args.token, answers, args.mode if args.command == "retry" else None)
        return engine.confirm(args.token, message)


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        require(sys.version_info >= (3, 11), "python_version", "Python 3.11+ is required.")
        result = execute(parse_args(argv))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except FlowError as exc:
        result = {"action": "blocked", "code": exc.code, "message": exc.message, **exc.details}
    except (OSError, ValueError, RecursionError) as exc:
        result = {"action": "blocked", "code": "io_or_data_error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
