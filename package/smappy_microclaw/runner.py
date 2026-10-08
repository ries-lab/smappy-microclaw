"""The microclaw.analysis.v1 worker: SMAPpy's localization fit, headless or in its GUI.

MicroClaw runs ``python -u -m smappy_microclaw.runner`` in this release's own
environment, writes the job as the first line of stdin and lifecycle
notifications after it, and reads protocol messages from stdout.  This module
is the protocol and nothing else; what is fitted, and how, is `fitting`.

Three things here are easy to undo by accident:

* **stdout belongs to the protocol.**  The real stdout is duplicated for the
  messages and file descriptor 1 is pointed at stderr, before anything is
  imported: Qt, the C++ fitter and smappy's own prints then land in the
  stderr tail MicroClaw keeps, and none of them can put a stray line into the
  stream MicroClaw parses as JSON (a worker failure, `design/83`).
* **Lifecycle messages are read on a thread** and turned into two
  `threading.Event`s -- ``stop`` and ``writer_finished`` -- which is all the
  fit needs to know.  Only an acquisition notification that says the writer
  has finished, or a later ``writer`` notification, completes the input;
  ``unterminated`` with ``writer: unknown`` does not, and the fit keeps
  waiting (the publishing guide: "do not claim the input is complete on that
  basis alone").  End of stdin before the result is a cancel; after it, for
  a window operation, it is the handoff and means nothing.
* **After the result of a window operation, stderr goes to a file** in
  ``output_dir``.  The window outlives the job; nobody reads the pipe any
  more, and a Qt warning written to it must not end the program the user is
  looking at.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

PROTOCOL = "microclaw.analysis.v1"
MAX_LINE = 65536
MAX_STATUS = 512
MAX_FAILURE = 1024
WINDOW_LOG = "smappy-window.log"


class Channel:
    """The two pipes: messages out, lifecycle in."""

    def __init__(self, out, job):
        self.out = out
        self.job = job
        self.job_id = job["job_id"]
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.writer_finished = threading.Event()
        self.acquisition = None            # the outcome, once one is known
        self.result_sent = False

    # ------------------------------------------------------------- out
    def emit(self, kind: str, **fields) -> None:
        message = dict(protocol=PROTOCOL, type=kind, job_id=self.job_id, **fields)
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(line.encode("utf-8")) + 1 > MAX_LINE:
            raise ValueError(f"a {kind} message is longer than {MAX_LINE} bytes")
        with self.lock:
            if self.result_sent:
                return                     # nothing may follow the result
            self.out.write(line + "\n")
            self.out.flush()
            if kind == "result":
                self.result_sent = True

    def status(self, text: str) -> None:
        self.emit("status", message=_line(text, MAX_STATUS))

    def result(self, state: str, output: dict, artifacts=(), complete=None,
               failure: str = "") -> None:
        fields = dict(state=state, output=output, artifacts=list(artifacts))
        if self.job["operation"] != "self_check":
            fields["input_complete"] = bool(complete)
        if state == "failed":
            fields["failure"] = {"message": _line(failure or "failed", MAX_FAILURE)}
        self.emit("result", **fields)

    # -------------------------------------------------------------- in
    def listen(self, stdin) -> threading.Thread:
        thread = threading.Thread(target=self._read, args=(stdin,), daemon=True,
                                  name="microclaw-lifecycle")
        thread.start()
        return thread

    def _read(self, stdin) -> None:
        for raw in stdin:
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(message, dict) or message.get("job_id") != self.job_id:
                continue
            kind = message.get("type")
            if kind == "cancel":
                self.stop.set()
            elif kind == "acquisition":
                self.acquisition = message.get("outcome")
                if message.get("writer") == "finished":
                    self.writer_finished.set()
            elif kind == "writer" and message.get("state") == "finished":
                self.writer_finished.set()
        if not self.result_sent:
            self.stop.set()                # EOF before the result: a cancel


def artifact(output_dir: Path, path: Path, validity: str) -> dict:
    """A descriptor for a closed file inside ``output_dir``."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return dict(path=Path(path).relative_to(output_dir).as_posix(),
                sha256=digest.hexdigest(), validity=validity)


def detach_stderr(output_dir: Path) -> None:
    """Point stderr at a file in ``output_dir`` once the pipe is no longer read."""
    try:
        log = os.open(output_dir / WINDOW_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        log = os.open(os.devnull, os.O_WRONLY)
    sys.stderr.flush()
    os.dup2(log, 2)
    os.close(log)


def _line(text: str, limit: int) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def _protocol_stdout():
    """The real stdout, for messages; fd 1 then goes to stderr."""
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return out


def main() -> int:
    out = _protocol_stdout()
    stdin = sys.stdin.buffer
    job = json.loads(stdin.readline(MAX_LINE))
    if job.get("protocol") != PROTOCOL or job.get("type") != "job":
        print("expected a microclaw.analysis.v1 job", file=sys.stderr)
        return 2
    channel = Channel(out, job)
    channel.status("starting smappy")      # before any import: the 60 s deadline
    channel.listen(stdin)
    try:
        from . import fitting
        return fitting.run(job, channel)
    except Exception as error:             # owed a result, whatever happened
        import traceback
        traceback.print_exc()
        if not channel.result_sent:
            channel.result("failed", {}, complete=False,
                           failure=f"{type(error).__name__}: {error}")
        return 0


def _exit(code: int) -> None:
    """End now, without the interpreter's shutdown.

    The lifecycle reader is a daemon thread blocked in a read of stdin, and
    finalizing the interpreter under it aborts the process ("could not acquire
    lock for <stdin> at interpreter shutdown") -- a non-zero exit after the
    result, which MicroClaw counts as a worker failure.  Every file is closed
    and the result sent by the time this runs, so nothing is lost by skipping
    the shutdown.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


if __name__ == "__main__":
    _exit(main())
