from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import DataTable, Footer, Header, Static

from .config import load_config
from .store import Store


class KhanApp(App[None]):
    CSS = """
    Screen {
      layout: vertical;
    }
    #body {
      layout: horizontal;
      height: 1fr;
    }
    .pane {
      width: 1fr;
      border: solid $accent;
      padding: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.store = Store(self.config.global_config.state_dir)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="body"):
            yield DataTable(id="projects", classes="pane")
            yield DataTable(id="runs", classes="pane")
            yield DataTable(id="sessions", classes="pane")
            yield Static(id="details", classes="pane")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Khan"
        self.sub_title = "Agent orchestration control plane"
        self.query_one("#projects", DataTable).add_columns("Project", "Path", "Validate")
        self.query_one("#runs", DataTable).add_columns("Run", "Project", "Status", "Iteration")
        self.query_one("#sessions", DataTable).add_columns("Session", "Provider", "Project", "Status")
        self.refresh_tables()
        self.set_interval(2, self.refresh_tables)

    def action_refresh(self) -> None:
        self.refresh_tables()

    def refresh_tables(self) -> None:
        projects_table = self.query_one("#projects", DataTable)
        runs_table = self.query_one("#runs", DataTable)
        sessions_table = self.query_one("#sessions", DataTable)
        projects_table.clear()
        runs_table.clear()
        sessions_table.clear()
        for name, project in self.config.projects.items():
            validate = ", ".join(project.validate_commands[:2]) or "-"
            projects_table.add_row(name, str(project.path), validate)
        for run in self.store.list_runs()[:20]:
            runs_table.add_row(run.id[:8], run.project, run.status, str(run.iteration))
        for session in self.store.list_agent_sessions()[:20]:
            sessions_table.add_row(session.id[:8], session.provider, session.project, session.status)
        details = self.query_one("#details", Static)
        active = self.store.list_runs(active_only=True)
        active_sessions = self.store.list_agent_sessions(active_only=True)
        details.update(
            "Active runs:\n"
            + ("\n".join(f"- {run.id[:8]} {run.project} {run.status}" for run in active) if active else "- none")
            + "\n\nActive sessions:\n"
            + (
                "\n".join(
                    f"- {session.id[:8]} {session.provider} {session.project} {session.status}"
                    for session in active_sessions
                )
                if active_sessions else "- none"
            )
        )
