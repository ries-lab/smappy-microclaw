"""The worker under the protocol: every operation, from the outside.

What MicroClaw's guide asks a publisher's CI to show: one valid result from
`self_check`; a dataset fitted while it is still written, ended by the
writer's word; a cancel that leaves a readable partial file -- and, for the
window operation, a result after which the process keeps running.
"""
import hashlib
import time

import pytest

GAUSSIAN = {"psf": "gaussian", "channels": 1, "conversion_e_per_adu": 0.5}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_self_check_fits_simulated_frames_and_builds_the_gui(worker):
    w = worker("self_check")
    result = w.result()
    assert result["state"] == "succeeded", w.stderr
    assert result["output"]["found"] > 0.8
    assert result["output"]["median_error_nm"] < 15
    assert w.process.wait(30) == 0


def _fit(worker, frames, tmp_path, operation):
    from conftest import GrowingNDTiff
    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames[:20])
    out = tmp_path / "out"
    out.mkdir()
    w = worker(operation, GAUSSIAN, data.folder, out)
    return w, data, out


@pytest.mark.parametrize("operation", ["fit_ndtiff", "fit_and_view"])
def test_the_fit_waits_out_a_pause_and_ends_when_the_writer_has_finished(
        worker, frames, tmp_path, operation):
    w, data, out = _fit(worker, frames, tmp_path, operation)
    w.next("status")
    time.sleep(40)                       # longer than SMAPpy's 30 s idle stop
    assert w.process.poll() is None and not any(
        m["type"] == "result" for m in w.seen)
    data.write(frames[20:])
    w.writer_finished()
    result = w.result()
    assert result["state"] == "succeeded", result
    assert result["input_complete"] is True
    assert result["output"]["frames"] == len(frames)
    assert result["output"]["camera"]["conversion"]["from"].startswith("user")
    artifact, = result["artifacts"]
    assert artifact["validity"] == "final"
    assert artifact["sha256"] == digest(out / artifact["path"])
    if operation == "fit_and_view":
        # the job is over and the window is not: stdin closing is the
        # handoff, and the process lives until the window is closed
        w.process.stdin.close()
        time.sleep(3)
        assert w.process.poll() is None
        assert artifact["sha256"] == digest(out / artifact["path"])
    else:
        assert w.process.wait(30) == 0


@pytest.mark.parametrize("operation", ["fit_ndtiff", "fit_and_view"])
def test_a_cancel_keeps_a_readable_partial_file(worker, frames, tmp_path, operation):
    from smappy.io.hdf5 import load_localizations

    w, data, out = _fit(worker, frames, tmp_path, operation)
    w.next("status")
    time.sleep(8)                        # fitting the first frames
    w.cancel()
    result = w.result(timeout=10)        # MicroClaw's shutdown grace
    assert result["state"] == "cancelled" and result["input_complete"] is False
    artifact, = result["artifacts"]
    assert artifact["validity"] == "partial"
    load_localizations(out / artifact["path"])          # closed, and readable


def test_a_camera_nobody_knows_is_refused_before_anything_is_fitted(
        worker, frames, tmp_path):
    from conftest import GrowingNDTiff
    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames[:5])
    out = tmp_path / "out"
    out.mkdir()
    w = worker("fit_ndtiff", {"psf": "gaussian", "channels": 1}, data.folder, out)
    result = w.result()
    assert result["state"] == "failed"
    assert "conversion_e_per_adu" in result["failure"]["message"]
    assert result["artifacts"] == []


def test_a_spline_fit_without_a_calibration_is_refused(worker, frames, tmp_path):
    from conftest import GrowingNDTiff
    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames[:5])
    out = tmp_path / "out"
    out.mkdir()
    w = worker("fit_and_view", {"psf": "spline", "channels": 1,
                                "conversion_e_per_adu": 0.5}, data.folder, out)
    result = w.result()
    assert result["state"] == "failed"
    assert "calibration" in result["failure"]["message"]
