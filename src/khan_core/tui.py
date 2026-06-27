from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import DataTable, Footer, Header, Static

from .adoption import AdoptionError, AdoptionManager
from .agents import AgentSessionRunner
from .attention import AttentionRouter
from .config import load_config
from .inspection import InspectError, Inspector
from .store import Store


@dataclass(frozen=True)
class Selection:
    kind: str
    record_id: str
    provider: str | None = None


class KhanApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #body { layout: horizontal; height: 1fr; }
    .column { width: 1fr; layout: vertical; }
    .pane { border: solid $accent; padding: 1; height: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("d", "diff_selected", "Diff"),
        ("e", "evidence_selected", "Evidence"),
        ("a", "adopt_selected", "Adopt"),
        ("x", "reject_selected", "Reject"),
        ("c", "cancel_selected", "Cancel"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.store = Store(self.config.global_config.state_dir)
        self.router = AttentionRouter(self.store)
        self.inspector = Inspector()
        self.adoptions = AdoptionManager()
        self.selection: Selection | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="body"):
            with Container(classes="column"):
                yield DataTable(id="inbox", classes="pane")
            with Container(classes="column"):
                yield Static(id="decision", classes="pane")
                yield Static(id="evidence", classes="pane")
                yield Static(id="diff", classes="pane")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Khan"
        self.sub_title = "Multi-agent decision inbox"
        inbox = self.query_one("#inbox", DataTable)
        inbox.cursor_type = "row"
        inbox.add_columns("Score", "Type", "ID", "Class", "Summary", "Next")
        self.refresh_tables()
        self.set_interval(2, self.refresh_tables)

    def action_refresh(self) -> None:
        self.refresh_tables()

    def action_diff_selected(self) -> None:
        self._render_diff()

    def action_evidence_selected(self) -> None:
        self._render_evidence()

    def action_adopt_selected(self) -> None:
        if self.selection is None:
            self._set_decision("No record selected.")
            return
        try:
            decision = self.adoptions.adopt(self.selection.record_id, provider=self.selection.provider, validate=True)
            self._set_decision(f"Adopted {decision.target_type} {decision.target_id[:8]} as {decision.id[:8]}")
        except (InspectError, AdoptionError) as exc:
            self._set_decision(str(exc))
        self.refresh_tables()

    def action_reject_selected(self) -> None:
        if self.selection is None:
            self._set_decision("No record selected.")
            return
        try:
            decision = self.adoptions.reject(self.selection.record_id, provider=self.selection.provider)
            self._set_decision(f"Rejected {decision.target_type} {decision.target_id[:8]} as {decision.id[:8]}")
        except (InspectError, AdoptionError) as exc:
            self._set_decision(str(exc))
        self.refresh_tables()

    def action_cancel_selected(self) -> None:
        if self.selection is None:
            self._set_decision("No record selected.")
            return
        try:
            if self.selection.kind == "session":
                AgentSessionRunner().cancel_session(self.selection.record_id)
                self._set_decision(f"Cancel requested for session {self.selection.record_id[:8]}")
            elif self.selection.kind == "run":
                self.store.enqueue_command(self.selection.record_id, "cancel")
                self._set_decision(f"Cancel requested for run {self.selection.record_id[:8]}")
            else:
                self._set_decision("Cancel is only available for runs and sessions.")
        except Exception as exc:
            self._set_decision(str(exc))
        self.refresh_tables()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        record_id = self._row_key_value(event)
        try:
            ref = self.inspector.resolve(record_id)
            provider = self._recommended_provider(ref.kind, ref.id)
            self.selection = Selection(kind=ref.kind, record_id=ref.id, provider=provider)
        except InspectError:
            self.selection = Selection(kind="unknown", record_id=record_id)
        self._render_selection()

    def refresh_tables(self) -> None:
        inbox = self.query_one("#inbox", DataTable)
        inbox.clear()
        cards = [card for card in self.router.cards() if card.classification in {"decision_required", "watch", "stopped"}]
        for card in cards:
            next_action = card.primary_action or (card.recommended_actions[0] if card.recommended_actions else "-")
            inbox.add_row(str(card.score), card.subject_type, card.run_id[:8], card.classification, card.summary, next_action, key=card.run_id)
        if self.selection is None and cards:
            card = cards[0]
            self.selection = Selection(card.subject_type, card.run_id, card.recommended_provider)
        self._render_selection()

    def _render_selection(self) -> None:
        if self.selection is None:
            self._set_decision("No record selected.")
            self._set_evidence("No record selected.")
            self._set_diff("No record selected.")
            return
        try:
            self._set_decision(self.inspector.summary_markdown(self.selection.record_id))
        except InspectError as exc:
            self._set_decision(str(exc))
        self._render_evidence()
        self._render_diff()

    def _render_evidence(self) -> None:
        if self.selection is None:
            self._set_evidence("No record selected.")
            return
        try:
            self._set_evidence(self.inspector.evidence_markdown(self.selection.record_id, provider=self.selection.provider))
        except InspectError as exc:
            self._set_evidence(str(exc))

    def _render_diff(self) -> None:
        if self.selection is None:
            self._set_diff("No record selected.")
            return
        try:
            self._set_diff(self.inspector.diff_text(self.selection.record_id, provider=self.selection.provider))
        except InspectError as exc:
            self._set_diff(str(exc))

    def _recommended_provider(self, kind: str, record_id: str) -> str | None:
        if kind != "pipeline":
            return None
        try:
            return self.store.get_pipeline(record_id).recommended_provider
        except KeyError:
            return None

    def _set_decision(self, text: str) -> None:
        self.query_one("#decision", Static).update(text)

    def _set_evidence(self, text: str) -> None:
        self.query_one("#evidence", Static).update(text)

    def _set_diff(self, text: str) -> None:
        self.query_one("#diff", Static).update(text)

    @staticmethod
    def _row_key_value(event: Any) -> str:
        row_key = getattr(event, "row_key", None)
        if row_key is None:
            return ""
        if hasattr(row_key, "value"):
            return str(row_key.value)
        return str(row_key)
