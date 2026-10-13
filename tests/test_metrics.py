"""Metrics while fitting: protocol 1.2, as docs/microclaw-feedback-spec.md proposes.

MicroClaw does not accept 1.2 yet, so the released manifest declares no
metrics and the worker sends none.  Here the worker runs under a 1.2 manifest
that declares them, and every metrics line is checked with the spec's own
validator: the interface can be produced before MicroClaw builds its side.
"""
import json
import shutil

import pytest

from conftest import PACKAGE, GrowingNDTiff

GAUSSIAN = {"psf": "gaussian", "channels": 1, "conversion_e_per_adu": 0.5}
METRICS = {
    "localizations_per_frame": {"description": "Localizations counted in each frame, those passing the count filter", "unit": "", "kind": "per_frame"},
    "localizations": {"description": "Localizations so far, all of them", "unit": "", "kind": "counter"},
    "frames_fitted": {"description": "Frames fitted so far", "unit": "frames", "kind": "counter"},
    "frames_behind": {"description": "Frames written but not yet read by the fit", "unit": "frames", "kind": "gauge"},
}
COUNT_FILTERS = {
    "count_min_photons": {"type": "number", "minimum": 0, "description": "Count only localizations with at least this many photons."},
    "count_max_precision_nm": {"type": "number", "minimum": 0, "description": "Count only localizations at least this precise (xy_err_nm)."},
}


def test_the_spec_declares_the_metrics_this_worker_sends():
    from conftest import SPEC
    text = SPEC.read_text(encoding="utf-8")
    example = text[text.index('"metrics": {'):]
    example = "{" + example[:example.index("\n}\n") + 2] + "}"
    assert json.loads(example)["metrics"] == METRICS


@pytest.fixture
def package_1_2(tmp_path):
    """The release with a protocol 1.2 manifest declaring the metrics."""
    folder = tmp_path / "release"
    shutil.copytree(PACKAGE, folder, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["protocol_version"] = "1.2"
    for op in manifest["operations"]:
        if op["name"] in ("fit_ndtiff", "fit_and_view"):
            op["metrics"] = METRICS
            op["input_schema"]["properties"].update(COUNT_FILTERS)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _smappy_streams_progress():
    import inspect
    from smappy.gui.live_session import LiveSession
    return "on_stream" in inspect.signature(LiveSession).parameters


@pytest.mark.skipif(not _smappy_streams_progress(),
                    reason="needs SMAPpy after 0.4.0 (the fit's 'fitted' event, "
                           "LiveSession on_stream); the lock pins 0.4.0")
@pytest.mark.parametrize("operation", ["fit_ndtiff", "fit_and_view"])
def test_metrics_follow_the_fit_and_agree_with_its_result(
        worker, frames, tmp_path, package_1_2, operation):
    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames)
    out = tmp_path / "out"
    out.mkdir()
    w = worker(operation, GAUSSIAN, data.folder, out, package=package_1_2, metrics=METRICS)
    w.next("status")
    w.writer_finished()
    result = w.result()
    assert result["state"] == "succeeded", result
    sent = [m for m in w.seen if m["type"] == "metrics"]
    assert sent, "no metrics before the result"
    # every frame exactly once, in order, zeros included
    counted = [n for m in sent for n in m["values"]["localizations_per_frame"]]
    assert len(counted) == len(frames) == result["output"]["frames"]
    assert sent[0]["frames"][0] == 0 and sent[-1]["frames"][1] == len(frames) - 1
    assert sum(counted) == result["output"]["localizations"]
    final = sent[-1]["values"]
    assert final["frames_fitted"] == len(frames)
    assert final["localizations"] == result["output"]["localizations"]
    assert final["frames_behind"] == 0


@pytest.mark.skipif(not _smappy_streams_progress(),
                    reason="needs SMAPpy after 0.4.0 (the fit's 'fitted' event)")
def test_only_the_localizations_passing_the_count_filter_are_counted(
        worker, frames, tmp_path, package_1_2):
    import numpy as np
    from smappy.io.hdf5 import load_localizations

    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames)
    out = tmp_path / "out"
    out.mkdir()
    w = worker("fit_ndtiff", {**GAUSSIAN, "count_min_photons": 300.0}, data.folder, out,
               package=package_1_2, metrics=METRICS)
    w.writer_finished()
    assert w.result()["state"] == "succeeded"
    counted = [n for m in w.seen if m["type"] == "metrics"
               for n in m["values"]["localizations_per_frame"]]
    locs = load_localizations(out / "locs.hdf5")
    bright = np.asarray(locs["photons"]) >= 300.0
    assert 0 < sum(counted) == int(bright.sum()) < len(locs)
    per_frame = np.bincount(np.asarray(locs["frame"])[bright], minlength=len(frames))
    assert counted == per_frame.tolist()


def test_the_released_manifest_sends_no_metrics(worker, frames, tmp_path):
    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames)
    out = tmp_path / "out"
    out.mkdir()
    w = worker("fit_ndtiff", GAUSSIAN, data.folder, out)
    w.writer_finished()
    assert w.result()["state"] == "succeeded"
    assert not [m for m in w.seen if m["type"] == "metrics"]
