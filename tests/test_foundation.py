from __future__ import annotations

import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from khan_core.agent_adapters import AgentAdapterRegistry, AgentCommand, JsonlAgentAdapter
from khan_core.agents import AgentSessionRunner
from khan_core.adoption import AdoptionError, AdoptionManager
from khan_core.attention import AttentionRouter
from khan_core.cli import app
from khan_core.codex_cli import CodexCLI, CodexCancelled
from khan_core.config import load_config
from khan_core.cross_review import CrossReviewRunner
from khan_core.daemon import DaemonSupervisor
from khan_core.duel import DuelRunner
from khan_core.loop_engine import LoopEngine
from khan_core.models import AgentSessionEvent, ProjectConfig, RunProcess, TaskCapsule
from khan_core.queue_worker import QueueWorker, QueueWorkerError
from khan_core.store import RunLockedError, Store


FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
if args[0] == "review":
    print("VERDICT: PASS")
    raise SystemExit(0)
out = pathlib.Path(args[args.index("--output-last-message") + 1])
workspace = pathlib.Path(args[args.index("-C") + 1])
if os.environ.get("FAKE_HANG"):
    print(json.dumps({"type": "started"}), flush=True)
    while True: time.sleep(.1)
change = os.environ.get("FAKE_CHANGE")
if change:
    path = workspace / change
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
print(json.dumps({"type": "thread.started", "thread_id": "fake-session", "text": "working"}), flush=True)
out.write_text(json.dumps({
    "status": "done", "summary": "done", "changed_files": ["reported-lie.txt"],
    "tests_run": [], "open_risks": [], "next_action": ""
}))
"""

FAKE_CURSOR = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if "--version" in args:
    print("cursor-agent fake")
    raise SystemExit(0)
workspace = pathlib.Path(args[args.index("--workspace") + 1])
prompt = args[-1]
change = os.environ.get("FAKE_CURSOR_CHANGE")
if change:
    path = workspace / change
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("cursor changed\n")
print(json.dumps({"type": "session.started", "chatId": "cursor-chat", "text": "started"}), flush=True)
print(json.dumps({"type": "message", "text": f"cursor done: {prompt[:20]}"}), flush=True)
"""


class FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.fake_cursor = self.root / "fake-cursor-agent"
        self.fake_cursor.write_text(FAKE_CURSOR)
        self.fake_cursor.chmod(self.fake_cursor.stat().st_mode | stat.S_IEXEC)
        self.say_log = self.root / "say.log"
        self.fake_say = self.root / "fake-say"
        self.fake_say.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            f"pathlib.Path({str(self.say_log)!r}).write_text(' '.join(sys.argv[1:]))\n"
        )
        self.fake_say.chmod(self.fake_say.stat().st_mode | stat.S_IEXEC)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migrations_and_run_lock(self) -> None:
        store = Store(self.root / "state")
        with store._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
        task = store.create_task("p", "t", "p", "s", None)
        run = store.create_run(task.id, "p", str(self.root))
        with store.run_lock(run.id):
            with self.assertRaises(RunLockedError):
                with store.run_lock(run.id):
                    pass
        store.start_process(RunProcess(run_id=run.id, pid=123, command=["x"],
                                       started_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC)))
        self.assertEqual(store.get_run(run.id).process_id, 123)
        store.finish_process(run.id, 123, 0)
        self.assertIsNone(store.get_run(run.id).process_id)

    def test_legacy_database_migrates(self) -> None:
        state = self.root / "legacy"
        state.mkdir()
        with sqlite3.connect(state / "orch.db") as conn:
            conn.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, project TEXT, title TEXT, prompt TEXT,
                  success_criteria TEXT, profile TEXT, created_at TEXT);
                CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT, project TEXT, status TEXT,
                  iteration INTEGER, workspace TEXT, summary TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, phase TEXT,
                  message TEXT, payload TEXT);
            """)
        store = Store(state)
        with store._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        self.assertIn("process_id", columns)

    def test_task_capsules_are_persisted(self) -> None:
        store = Store(self.root / "state")
        capsule = TaskCapsule(
            objective="objective",
            acceptance_criteria=["done"],
            allowed_paths=["docs"],
            verification=["make test"],
            conflict_domains=["docs"],
        )
        task = store.create_task("p", "title", "prompt", "success", None, capsule)
        loaded = store.get_task_capsule(task.id)
        self.assertEqual(loaded.allowed_paths, ["docs"])
        self.assertEqual(loaded.conflict_domains, ["docs"])

        store.set_task_capsule(task.id, capsule.model_copy(update={"blast_radius": "medium"}))
        self.assertEqual(store.get_task_capsule(task.id).blast_radius, "medium")

    def test_agent_session_store_lifecycle(self) -> None:
        store = Store(self.root / "state")
        session = store.create_agent_session("codex", "p", str(self.root), "prompt")
        store.start_agent_session(session.id, 456)
        store.update_agent_session_external_id(session.id, "external-1")
        store.append_agent_session_event(
            AgentSessionEvent(
                session_id=session.id,
                ts=datetime.now(UTC),
                stream="stdout",
                message="started",
                payload={"type": "session.started"},
            )
        )
        store.finish_agent_session(session.id, "succeeded", "done")
        loaded = store.get_agent_session(session.id)
        self.assertEqual(loaded.status, "succeeded")
        self.assertEqual(loaded.external_id, "external-1")
        self.assertIsNone(loaded.process_id)
        self.assertEqual(store.list_agent_sessions()[0].id, session.id)
        events = store.list_agent_session_events(session.id)
        self.assertEqual(events[0].message, "started")

    def test_streaming_and_cancellation(self) -> None:
        project = ProjectConfig(name="p", path=self.root, workspace_mode="in_place")
        schema = self.root / "schema.json"
        schema.write_text("{}")
        output = self.root / "result.json"
        events = []
        result, returned = CodexCLI(str(self.fake)).exec_task(
            self.root, "prompt", schema, output, project, extra_env={"FAKE_CHANGE": "x.txt"},
            on_event=events.append,
        )
        self.assertEqual(result.summary, "done")
        self.assertEqual(events, returned)

        started = time.monotonic()
        with self.assertRaises(CodexCancelled):
            CodexCLI(str(self.fake)).exec_task(
                self.root, "prompt", schema, output, project, extra_env={"FAKE_HANG": "1"},
                commands=lambda: [(1, "cancel")] if time.monotonic() - started > .2 else [],
            )

    def test_engine_uses_git_diff_for_protected_paths(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: true
  say_bin: {self.fake_say}
  phrase: Nirmal, Khan needs your input.
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects:
  p:
    name: p
    path: {repo}
    workspace_mode: in_place
    protected_paths: [protected]
    env:
      FAKE_CHANGE: protected/secret.txt
""")
        engine = LoopEngine(config)
        task = engine.store.create_task("p", "title", "prompt", "success", None)
        run_id = engine.run_task(task.id)
        run = engine.store.get_run(run_id)
        self.assertEqual(run.status, "needs_human")
        self.assertEqual(run.session_id, "fake-session")
        self.assertIn("Protected path", run.summary)
        self.assertIn("Protected path changed", self.say_log.read_text())
        events = engine.store.list_events(run_id, limit=20)
        self.assertTrue(any(event.phase == "notify" and event.payload["sent"] for event in events))

    def test_engine_enforces_capsule_allowed_paths(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects:
  p:
    name: p
    path: {repo}
    workspace_mode: in_place
    env:
      FAKE_CHANGE: src/outside.txt
""")
        engine = LoopEngine(config)
        capsule = TaskCapsule(objective="prompt", acceptance_criteria=["success"], allowed_paths=["docs"])
        task = engine.store.create_task("p", "title", "prompt", "success", None, capsule)
        run_id = engine.run_task(task.id)
        run = engine.store.get_run(run_id)
        self.assertEqual(run.status, "needs_human")
        self.assertIn("outside", run.summary)
        events = engine.store.list_events(run_id, limit=20)
        needs_human = [event for event in events if event.phase == "needs_human"][-1]
        self.assertEqual(needs_human.payload["outside_allowed_paths"], ["src/outside.txt"])

    def test_conflict_domain_blocks_active_runs(self) -> None:
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 5
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects:
  p:
    name: p
    path: {self.root}
    workspace_mode: in_place
""")
        engine = LoopEngine(config)
        capsule = TaskCapsule(objective="prompt", acceptance_criteria=["success"], conflict_domains=["docs"])
        first = engine.store.create_task("p", "first", "prompt", "success", None, capsule)
        second = engine.store.create_task("p", "second", "prompt", "success", None, capsule)
        active = engine.store.create_run(first.id, "p", str(self.root))
        engine.store.update_run(active.id, "running", "active")
        with self.assertRaisesRegex(RuntimeError, "conflicts with active run"):
            engine.run_task(second.id)

    def test_attention_router_and_metrics(self) -> None:
        store = Store(self.root / "state")
        task = store.create_task("p", "title", "prompt", "success", None)
        run = store.create_run(task.id, "p", str(self.root))
        store.update_run(run.id, "needs_human", "review required")
        session = store.create_agent_session("cursor-agent", "p", str(self.root), "prompt")
        store.start_agent_session(session.id, 1234)
        item = store.enqueue_task(task.id)
        daemon = store.create_daemon(12345, ["khan", "daemon", "run"], 2.0, 900.0)
        duel = store.create_duel("p", "prompt", ["codex", "cursor-agent"])
        store.update_duel(duel.id, "awaiting_decision", "choose a provider")
        store.create_adoption_decision(
            target_type="duel",
            target_id=duel.id,
            provider="codex",
            project="p",
            source_workspace=str(self.root / "source"),
            destination_workspace=str(self.root),
            status="adopted",
            changed_files=["docs/x.md"],
            summary="adopted",
        )
        cross_review = store.create_cross_review(duel.id)
        store.update_cross_review(cross_review.id, "awaiting_decision", "review complete")

        router = AttentionRouter(store)
        cards = router.cards()
        self.assertEqual(cards[0].classification, "decision_required")
        self.assertEqual(cards[0].subject_type, "run")
        metrics = router.metrics()
        self.assertEqual(metrics["runs"]["needs_human"], 1)
        self.assertEqual(metrics["sessions"]["active"], 1)
        self.assertEqual(metrics["queue"]["queued"], 1)
        self.assertEqual(metrics["daemons"]["running"], 1)
        self.assertEqual(metrics["duels"]["awaiting_decision"], 1)
        self.assertEqual(metrics["adoptions"]["adopted"], 1)
        self.assertEqual(metrics["cross_reviews"]["awaiting_decision"], 1)
        self.assertTrue(any(card.subject_type == "queue" and card.run_id == item.id for card in cards))
        self.assertTrue(any(card.subject_type == "daemon" and card.run_id == daemon.id for card in cards))
        self.assertTrue(any(card.subject_type == "duel" and card.run_id == duel.id for card in cards))
        self.assertTrue(any(card.subject_type == "cross_review" and card.run_id == cross_review.id for card in cards))

    def test_queue_claim_requeue_and_cancel_lifecycle(self) -> None:
        store = Store(self.root / "state")
        task = store.create_task("p", "title", "prompt", "success", None)
        first = store.enqueue_task(task.id, priority=20)
        second = store.enqueue_session("cursor-agent", "p", "prompt", priority=10)
        claimed = store.claim_next_queue_item("worker-1")
        self.assertEqual(claimed.id, second.id)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(claimed.lease_owner, "worker-1")

        store.complete_queue_item(claimed.id, "session-result")
        completed = store.get_queue_item(claimed.id)
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.result_id, "session-result")

        store.cancel_queue_item(first.id)
        self.assertEqual(store.get_queue_item(first.id).status, "cancelled")

    def test_stale_queue_leases_are_reclaimed(self) -> None:
        store = Store(self.root / "state")
        task = store.create_task("p", "title", "prompt", "success", None)
        item = store.enqueue_task(task.id)
        claimed = store.claim_next_queue_item("worker-1")
        with store._connect() as conn:
            conn.execute(
                "UPDATE queue_items SET leased_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", claimed.id),
            )
        reclaimed = store.reclaim_stale_queue_items(older_than_seconds=60)
        self.assertEqual(reclaimed, 1)
        loaded = store.get_queue_item(item.id)
        self.assertEqual(loaded.status, "queued")
        self.assertIsNone(loaded.lease_owner)
        self.assertIsNone(loaded.leased_at)

    def test_daemon_store_lifecycle_and_worker_stop(self) -> None:
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: false
projects: {{}}
""")
        store = Store(state)
        daemon = store.create_daemon(123, ["khan", "daemon", "run"], 0.1, 60.0, daemon_id="daemon-1")
        self.assertEqual(store.get_daemon(daemon.id).status, "running")
        store.request_daemon_stop(daemon.id)
        worker = QueueWorker(config, worker_id="test-worker", daemon_id=daemon.id)
        worker.run_forever(poll_seconds=0.01)
        stopped = store.get_daemon(daemon.id)
        self.assertEqual(stopped.status, "stopped")
        self.assertIsNotNone(stopped.stopped_at)

    def test_daemon_supervisor_start_status_stop(self) -> None:
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects: {{}}
""")
        supervisor = DaemonSupervisor(config)
        daemon = supervisor.start(poll_seconds=0.1, lease_timeout_seconds=1.0)
        try:
            time.sleep(0.3)
            statuses = {record.id: record.status for record in supervisor.status()}
            self.assertEqual(statuses[daemon.id], "running")
        finally:
            stopped = supervisor.stop(daemon.id)
        self.assertIn(stopped.status, {"stopping", "stopped"})

    def test_queue_worker_processes_session_items(self) -> None:
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects:
  p:
    name: p
    path: {self.root}
    workspace_mode: in_place
""")
        store = Store(state)
        item = store.enqueue_session("cursor-agent", "p", "queued prompt")
        daemon = store.create_daemon(123, ["khan", "daemon", "run"], 0.1, 60.0, daemon_id="daemon-1")
        worker = QueueWorker(config, worker_id="test-worker", daemon_id=daemon.id)
        processed = worker.process_once()
        self.assertEqual(processed.id, item.id)
        self.assertEqual(processed.status, "succeeded")
        self.assertTrue(processed.result_id)
        session = store.get_agent_session(processed.result_id)
        self.assertEqual(session.status, "succeeded")
        self.assertEqual(session.external_id, "cursor-chat")
        self.assertEqual(store.get_daemon(daemon.id).last_queue_item_id, item.id)

    def test_queue_worker_records_failures(self) -> None:
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects: {{}}
""")
        store = Store(state)
        item = store.enqueue_item("session", {"provider": "cursor-agent", "project": "missing", "prompt": ""})
        worker = QueueWorker(config, worker_id="test-worker")
        with self.assertRaises(QueueWorkerError):
            worker.process_once()
        failed = store.get_queue_item(item.id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("missing provider", failed.error)

    def test_codex_and_cursor_agent_sessions_are_recorded(self) -> None:
        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects:
  p:
    name: p
    path: {self.root}
    workspace_mode: in_place
""")
        runner = AgentSessionRunner(config)
        codex_session_id = runner.start_session("codex", "p", "codex prompt")
        cursor_session_id = runner.start_session("cursor-agent", "p", "cursor prompt")

        codex_session = runner.store.get_agent_session(codex_session_id)
        cursor_session = runner.store.get_agent_session(cursor_session_id)
        self.assertEqual(codex_session.status, "succeeded")
        self.assertEqual(codex_session.external_id, "fake-session")
        self.assertIn('"summary": "done"', codex_session.summary)
        self.assertEqual(cursor_session.status, "succeeded")
        self.assertEqual(cursor_session.external_id, "cursor-chat")
        self.assertIn("cursor done", cursor_session.summary)
        cursor_events = runner.store.list_agent_session_events(cursor_session_id)
        self.assertTrue(any(event.message == "started" for event in cursor_events))

    def test_provider_duel_runs_both_agents_and_writes_report(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 3
notifications:
  input_needed: false
projects:
  p:
    name: p
    path: {repo}
    default_branch: main
    workspace_mode: in_place
    validate_commands:
      - test -f base.txt
    env:
      FAKE_CHANGE: docs/codex.txt
      FAKE_CURSOR_CHANGE: docs/cursor.txt
""")
        runner = DuelRunner(config)
        duel = runner.run_duel("p", "Implement the feature.", validate=True)
        self.assertEqual(duel.status, "awaiting_decision")
        self.assertTrue(duel.report_path)
        self.assertTrue(Path(duel.report_path).exists())
        self.assertIn("Provider Comparison", Path(duel.report_path).read_text())

        participants = runner.store.list_duel_participants(duel.id)
        self.assertEqual({participant.provider for participant in participants}, {"codex", "cursor-agent"})
        for participant in participants:
            self.assertEqual(participant.status, "succeeded")
            self.assertTrue(participant.session_id)
            self.assertNotEqual(Path(participant.workspace), repo)
            self.assertTrue(Path(participant.workspace).exists())
            self.assertTrue(participant.validation_ok)
            self.assertTrue(Path(participant.artifact_path).exists())

        by_provider = {participant.provider: participant for participant in participants}
        self.assertEqual(by_provider["codex"].changed_files, ["docs/codex.txt"])
        self.assertEqual(by_provider["cursor-agent"].changed_files, ["docs/cursor.txt"])
        artifacts = [path.name for path in runner.store.list_artifacts(duel.id)]
        self.assertIn("duel-report.md", artifacts)
        self.assertIn("codex-result.md", artifacts)
        self.assertIn("cursor-agent-result.md", artifacts)

    def test_cross_review_runs_each_provider_against_other_diff(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 4
notifications:
  input_needed: false
projects:
  p:
    name: p
    path: {repo}
    default_branch: main
    workspace_mode: in_place
    env:
      FAKE_CHANGE: docs/codex.txt
      FAKE_CURSOR_CHANGE: docs/cursor.txt
""")
        duel_runner = DuelRunner(config)
        duel = duel_runner.run_duel("p", "Implement competing changes.", validate=False)
        review = CrossReviewRunner(config).run_cross_review(duel.id)
        self.assertEqual(review.status, "awaiting_decision")
        self.assertTrue(Path(review.report_path).exists())
        self.assertIn("Cross-Review Report", Path(review.report_path).read_text())

        critiques = duel_runner.store.list_cross_review_critiques(review.id)
        pairs = {(critique.reviewer_provider, critique.subject_provider) for critique in critiques}
        self.assertEqual(pairs, {("codex", "cursor-agent"), ("cursor-agent", "codex")})
        for critique in critiques:
            self.assertEqual(critique.status, "succeeded")
            self.assertTrue(critique.session_id)
            self.assertTrue(critique.findings)
            self.assertTrue(Path(critique.artifact_path).exists())
        artifacts = [path.name for path in duel_runner.store.list_artifacts(review.id)]
        self.assertIn("cross-review-report.md", artifacts)
        self.assertIn("codex-reviews-cursor-agent.md", artifacts)
        self.assertIn("cursor-agent-reviews-codex.md", artifacts)

    def test_adopt_and_reject_duel_participants(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 3
notifications:
  input_needed: false
projects:
  p:
    name: p
    path: {repo}
    default_branch: main
    workspace_mode: in_place
    env:
      FAKE_CHANGE: docs/codex.txt
      FAKE_CURSOR_CHANGE: docs/cursor.txt
""")
        duel = DuelRunner(config).run_duel("p", "Implement both options.", validate=False)
        manager = AdoptionManager(config)
        adopted = manager.adopt(duel.id, provider="codex")
        self.assertEqual(adopted.status, "adopted")
        self.assertEqual(adopted.changed_files, ["docs/codex.txt"])
        self.assertEqual((repo / "docs" / "codex.txt").read_text(), "changed\n")
        self.assertEqual(manager.store.get_duel(duel.id).status, "adopted")
        self.assertEqual(manager.store.get_duel_participant(duel.id, "codex").status, "adopted")

        with self.assertRaisesRegex(AdoptionError, "Destination worktree is dirty"):
            manager.adopt(duel.id, provider="cursor-agent")
        failed = manager.store.list_adoption_decisions()[0]
        self.assertEqual(failed.status, "failed")
        self.assertIn("dirty", failed.error)
        self.assertFalse((repo / "docs" / "cursor.txt").exists())

        cursor_workspace = Path(manager.store.get_duel_participant(duel.id, "cursor-agent").workspace)
        self.assertTrue(cursor_workspace.exists())
        rejected = manager.reject(duel.id, provider="cursor-agent")
        self.assertEqual(rejected.status, "rejected")
        self.assertFalse(cursor_workspace.exists())
        self.assertEqual(manager.store.get_duel_participant(duel.id, "cursor-agent").status, "rejected")
        self.assertEqual(manager.store.get_duel(duel.id).status, "adopted")

    def test_duel_cli_run_path_form_auto_discovers_project(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 3
notifications:
  input_needed: false
projects: {{}}
""")
        result = CliRunner().invoke(app, ["duel", "run", str(repo), "Implement via CLI.", "--config", str(config)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("finished with status awaiting_decision", result.output)
        loaded = load_config(config)
        self.assertEqual(len(loaded.projects), 1)
        store = Store(state)
        duel = store.list_duels()[0]
        self.assertEqual(duel.status, "awaiting_decision")

    def test_ask_cli_auto_discovers_project_and_runs_task(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "Makefile").write_text("test:\n\ttrue\n")
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 3
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects: {{}}
""")
        result = CliRunner().invoke(
            app,
            ["ask", str(repo), "Implement a broad local task.", "--config", str(config)],
            env={"FAKE_CHANGE": "docs/ask.txt"},
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("finished with status succeeded", result.output)
        loaded = load_config(config)
        self.assertEqual(len(loaded.projects), 1)
        project = next(iter(loaded.projects.values()))
        self.assertEqual(project.validate_commands, ["make test"])
        store = Store(state)
        task = store.list_tasks()[0]
        capsule = store.get_task_capsule(task.id)
        self.assertEqual(capsule.verification, ["make test"])
        self.assertEqual(capsule.conflict_domains, [project.name])
        run = store.list_runs()[0]
        self.assertEqual(run.status, "succeeded")

    def test_ask_cli_enqueue_mode_creates_queue_item(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 3
notifications:
  input_needed: false
projects: {{}}
""")
        result = CliRunner().invoke(
            app,
            ["ask", str(repo), "Queue this local task.", "--enqueue", "--priority", "5", "--config", str(config)],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("queued item", result.output)
        store = Store(state)
        items = store.list_queue_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "task")
        self.assertEqual(items[0].priority, 5)

    def test_custom_agent_adapter_can_be_registered(self) -> None:
        class FakeAgentAdapter(JsonlAgentAdapter):
            name = "fake-agent"

            def build_command(self, **kwargs) -> AgentCommand:
                return AgentCommand(
                    argv=[
                        sys.executable,
                        "-c",
                        "import json; print(json.dumps({'session_id': 'custom-session', 'text': 'custom done'}))",
                    ]
                )

        state = self.root / "state"
        config = self.root / "config.yaml"
        config.write_text(f"""global:
  codex_bin: {self.fake}
  cursor_agent_bin: {self.fake_cursor}
  state_dir: {state}
  max_concurrent_runs: 2
notifications:
  input_needed: false
profiles:
  default:
    max_iterations: 1
    auto_review: false
projects:
  p:
    name: p
    path: {self.root}
    workspace_mode: in_place
""")
        registry = AgentAdapterRegistry()
        registry.register(FakeAgentAdapter())
        runner = AgentSessionRunner(config, registry=registry)
        session_id = runner.start_session("fake-agent", "p", "custom prompt")
        session = runner.store.get_agent_session(session_id)
        self.assertEqual(session.provider, "fake-agent")
        self.assertEqual(session.external_id, "custom-session")
        self.assertEqual(session.summary, "custom done")


if __name__ == "__main__":
    unittest.main()
