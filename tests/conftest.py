"""Running the worker the way MicroClaw does, and a dataset that grows.

`Worker` starts ``python -u -m smappy_microclaw.runner`` with the release
directory on ``PYTHONPATH`` and the ``MICROCLAW_`` variables removed -- and, on
Windows, with the creation flags MicroClaw's supervisor uses (no console
window, below-normal priority) -- writes the job and the lifecycle lines, and
checks every line the worker prints with MicroClaw's own
`validate_worker_message`.  MicroClaw has no publisher-side check yet
(`R150`), so this is the check.

``microclaw`` must be importable: CI puts the commit the release workflow pins
on ``PYTHONPATH``; locally, point ``PYTHONPATH`` at a checkout.
"""
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "package"
PROTOCOL = "microclaw.analysis.v1"


class Worker:
    def __init__(self, operation, parameters=None, dataset=None, output_dir=None):
        from microclaw.skill_packages import validate_job
        self.operation = operation
        self.job_id = uuid.uuid4().hex
        job = {"protocol": PROTOCOL, "type": "job", "job_id": self.job_id,
               "release": {"publisher": "ries-lab", "package_id": "smappy",
                           "version": "0.0.0", "artifact_digest": "0" * 64},
               "operation": operation, "parameters": parameters or {}}
        if operation != "self_check":
            job["input"] = {"dataset": str(dataset)}
            job["output_dir"] = str(output_dir)
        validate_job(job)
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("MICROCLAW_")}
        env.update(PYTHONPATH=str(PACKAGE), PYTHONUNBUFFERED="1",
                   PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
        flags = 0x08000000 | 0x4000 if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "smappy_microclaw.runner"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(output_dir) if output_dir else None, env=env, creationflags=flags)
        self.messages = queue.Queue()
        self.seen = []
        self.stderr = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        self.send(job)

    def _read(self):
        from microclaw.skill_packages import validate_worker_message
        for raw in self.process.stdout:
            message = json.loads(raw)
            validate_worker_message(message, job_id=self.job_id, operation=self.operation)
            self.messages.put(message)
        self.messages.put(None)

    def _drain(self):
        for raw in self.process.stderr:
            self.stderr.append(raw.decode("utf-8", "replace"))

    def send(self, message):
        if message.get("type") != "job":
            message = {"protocol": PROTOCOL, "job_id": self.job_id, **message}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def writer_finished(self):
        self.send({"type": "acquisition", "outcome": "completed", "writer": "finished"})

    def cancel(self):
        self.send({"type": "cancel"})

    def next(self, kind, timeout=120.0):
        """The next message of ``kind``, keeping the others in ``seen``."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {kind} in {timeout} s; stderr:\n"
                                   + "".join(self.stderr[-40:]))
            message = self.messages.get(timeout=left)
            if message is None:
                raise EOFError(f"the worker ended before a {kind}; stderr:\n"
                               + "".join(self.stderr[-40:]))
            self.seen.append(message)
            if message["type"] == kind:
                return message

    def result(self, timeout=180.0):
        return self.next("result", timeout)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(10)


@pytest.fixture
def worker():
    started = []

    def start(*args, **kwargs):
        w = Worker(*args, **kwargs)
        started.append(w)
        return w
    yield start
    for w in started:
        w.close()


class GrowingNDTiff:
    """An NDTiff dataset written a frame at a time, by appending, as
    NDTiffStorage does: the pixels first, then the index record."""

    NAME = "Stack_NDTiffStack.tif"

    def __init__(self, folder, shape, offset=100):
        from smappy.io.ndtiff import INDEX_NAME, SUMMARY_MARKER, SUMMARY_OFFSET
        self.folder, self.index, self.offset = Path(folder), INDEX_NAME, offset
        self.folder.mkdir(parents=True)
        summary = json.dumps({"PixelSize_um": 0.1, "Height": shape[0],
                              "Width": shape[1]}).encode()
        (self.folder / self.NAME).write_bytes(
            b"\0" * SUMMARY_OFFSET + struct.pack("<II", SUMMARY_MARKER, len(summary))
            + summary)
        (self.folder / INDEX_NAME).write_bytes(b"")
        self.n = 0

    def write(self, frames):
        for frame in frames:
            stack = self.folder / self.NAME
            at = stack.stat().st_size
            pixels = np.ascontiguousarray(frame, dtype=np.uint16)
            metadata = json.dumps({"Core-Camera": "Camera",
                                   "Camera-Offset": str(self.offset),
                                   "ROI": f"0-0-{frame.shape[1]}-{frame.shape[0]}",
                                   "Exposure-ms": 20.0}).encode()
            with stack.open("ab") as f:
                f.write(pixels.tobytes() + metadata)
            axes = json.dumps({"time": self.n}).encode()
            record = (struct.pack("<I", len(axes)) + axes
                      + struct.pack("<I", len(self.NAME)) + self.NAME.encode()
                      + struct.pack("<8I", at, frame.shape[1], frame.shape[0], 1,
                                    0, at + pixels.nbytes, len(metadata), 0))
            with (self.folder / self.index).open("ab") as f:
                f.write(record)
            self.n += 1


@pytest.fixture(scope="session")
def frames():
    """Simulated camera frames: conversion 0.5 e-/ADU, offset 100, 100 nm pixels."""
    from smappy.simulate import LabellingSettings, camera_frames
    stack, truth = camera_frames(n_frames=60, size_px=32, seed=3,
                                 labelling=LabellingSettings(efficiency=0.1))
    return stack
