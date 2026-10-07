#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.85", "requests>=2.31"]
# ///
"""tester — a form for probing one inference host.

    Host:    <host>
    Models:  <filter>  + a list of the host's models (pick one)
    Payload: <message>
    Result:  [retry]   then one row per attempt:  [timing] [timestamp] outcome

Fully standalone: it does NOT import dyva or go through the router — it hits the raw
host as a plain OpenAI/Ollama client, so it's an honest cross-check of what a host
really does. Run:  ./tester.py   (or: uv run tester.py)

Host formats: http://1.2.3.4:11434 · 1.2.3.4:11434 · :443 -> https
"""
import time

import requests
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, RichLog
from textual.widgets.option_list import Option

CONNECT_TIMEOUT = 8
READ_TIMEOUT = 60            # give a slow/cold host real time; a dead one fails on connect
UA = "tester/1.0"


# ---- networking (pure, no dyva) ----------------------------------------------
def parse_host(line):
    line = line.strip()
    if line.startswith(("http://", "https://")):
        return line.rstrip("/")
    scheme = "https" if line.endswith(":443") else "http"
    return f"{scheme}://{line.rstrip('/')}"


def fetch_models(base):
    """-> (meta, notes). meta: {model_name: (chat_path, is_openai)}; notes: [(text,sev)]."""
    meta, notes = {}, []
    for path, chatpath, openai, extract in (
        ("/api/tags", "/api/chat", False,
         lambda d: [m.get("name") for m in (d.get("models") or []) if m.get("name")]),
        ("/v1/models", "/v1/chat/completions", True,
         lambda d: [m.get("id") for m in (d.get("data") or []) if m.get("id")]),
    ):
        try:
            t0 = time.time()
            r = requests.get(base + path, headers={"User-Agent": UA},
                             timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            dt = time.time() - t0
            if r.status_code == 200:
                got = extract(r.json())
                for m in got:
                    meta.setdefault(m, (chatpath, openai))   # first dialect seen wins
                notes.append((f"{path}  200  {dt:.2f}s  ({len(got)})", "ok"))
            else:
                notes.append((f"{path}  {r.status_code}", "warn"))
        except requests.RequestException as e:
            notes.append((f"{path}  {type(e).__name__}", "err"))
    return meta, notes


def send_chat(base, chatpath, model, openai, payload):
    """-> (ok, outcome_text, seconds)."""
    body = {"model": model, "messages": [{"role": "user", "content": payload}],
            "stream": False}
    t0 = time.time()
    try:
        r = requests.post(base + chatpath, json=body, headers={"User-Agent": UA},
                          timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {e}", time.time() - t0
    dt = time.time() - t0
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {' '.join(r.text.split())[:200]}", dt
    try:
        d = r.json()
    except ValueError:
        return False, f"non-JSON: {r.text[:120]!r}", dt
    if openai:
        ans = (((d.get("choices") or [{}])[0].get("message") or {}).get("content"))
    else:
        ans = (d.get("message") or {}).get("content")
    if ans is None and d.get("error"):
        return False, f"error: {d['error']}", dt
    return True, " ".join(str(ans).split())[:200], dt


# ---- the form ----------------------------------------------------------------
class Tester(App):
    TITLE = "tester"
    CSS = """
    Screen { background: $surface; }
    .field { height: auto; margin: 0 1; }
    .lbl { width: 10; color: $text-muted; padding: 1 0 0 0; }
    Input { border: round $primary-darken-1; }
    Input:focus { border: round $accent; }
    #models {
        height: 8; margin: 0 1 0 11; border: round $primary-darken-1;
        background: $panel;
    }
    #models:focus { border: round $accent; }
    #actions { height: auto; margin: 1 1 0 1; }
    #retry { margin-left: 11; }
    #target { padding: 1 0 0 2; color: $text-muted; }
    #results {
        border: round $primary; background: $panel; margin: 1 1 0 1;
        padding: 0 1; height: 1fr;
    }
    """
    BINDINGS = [("ctrl+r", "send", "Send/Retry"), ("ctrl+l", "clear", "Clear"),
                ("ctrl+c", "quit", "Quit"), ("escape", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(classes="field"):
            yield Label("Host:", classes="lbl")
            yield Input(placeholder="1.2.3.4:11434  (Enter to load models)", id="host")
        with Horizontal(classes="field"):
            yield Label("Models:", classes="lbl")
            yield Input(placeholder="filter…", id="modelq")
        yield OptionList(id="models")
        with Horizontal(classes="field"):
            yield Label("Payload:", classes="lbl")
            yield Input(placeholder="message to send  (Enter to send)", id="payload")
        with Horizontal(id="actions"):
            yield Button("↻ retry", id="retry", variant="primary")
            yield Label("", id="target")
        yield RichLog(id="results", wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.meta: dict = {}
        self.all_models: list[str] = []
        self.filtered: list[str] = []
        self.selected: str | None = None
        self.query_one("#host", Input).focus()
        self._retarget()

    # host entered -> load its models
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "host":
            self._load_models(parse_host(event.value))
        elif event.input.id == "payload":
            self.action_send()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "modelq":
            self._refilter(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self.filtered):
            self.selected = self.filtered[event.option_index]
            self._retarget()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "retry":
            self.action_send()

    # --- model loading / filtering ---
    @work(thread=True)
    def _load_models(self, base: str) -> None:
        meta, notes = fetch_models(base)
        self.call_from_thread(self._models_loaded, base, meta, notes)

    def _models_loaded(self, base, meta, notes) -> None:
        self.meta = meta
        self.all_models = sorted(meta)
        self.selected = self.all_models[0] if self.all_models else None
        self._refilter(self.query_one("#modelq", Input).value)
        self._retarget()
        log = self.query_one("#results", RichLog)
        for text, sev in notes:
            log.write(Text(f"· {base}  {text}",
                           style={"ok": "green", "warn": "yellow", "err": "red"}[sev]))
        if not meta:
            log.write(Text(f"· {base}  no models — unreachable or wrong port", "bold red"))

    def _refilter(self, q: str) -> None:
        q = (q or "").strip().lower()
        self.filtered = [m for m in self.all_models if q in m.lower()]
        ol = self.query_one("#models", OptionList)
        ol.clear_options()
        ol.add_options([Option(m) for m in self.filtered])
        if self.filtered:
            if self.selected not in self.filtered:
                self.selected = self.filtered[0]
            ol.highlighted = self.filtered.index(self.selected)
        self._retarget()

    def _retarget(self) -> None:
        host = self.query_one("#host", Input).value.strip() if self.is_mounted else ""
        tgt = f"→ {host or '(no host)'}  ::  {self.selected or '(no model)'}"
        self.query_one("#target", Label).update(tgt)

    # --- send / retry ---
    def action_send(self) -> None:
        host = self.query_one("#host", Input).value.strip()
        payload = self.query_one("#payload", Input).value
        if not host or not self.selected:
            self.query_one("#results", RichLog).write(
                Text("set a host and select a model first", "yellow"))
            return
        chatpath, openai = self.meta.get(self.selected, ("/v1/chat/completions", True))
        self._run_send(parse_host(host), chatpath, self.selected, openai, payload)

    @work(thread=True)
    def _run_send(self, base, chatpath, model, openai, payload) -> None:
        ok, outcome, dt = send_chat(base, chatpath, model, openai, payload)
        self.call_from_thread(self._result_row, model, ok, outcome, dt)

    def _result_row(self, model, ok, outcome, dt) -> None:
        ts = time.strftime("%H:%M:%S")
        row = Text()
        row.append(f"[{dt:6.2f}s] ", style="cyan")
        row.append(f"[{ts}] ", style="dim")
        row.append(f"{model}  ", style="dim")
        if ok:
            row.append("PASS  ", style="bold green")
            row.append(repr(outcome))
        else:
            row.append("FAIL  ", style="bold red")
            row.append(outcome, style="red")
        self.query_one("#results", RichLog).write(row)

    def action_clear(self) -> None:
        self.query_one("#results", RichLog).clear()


if __name__ == "__main__":
    Tester().run()
