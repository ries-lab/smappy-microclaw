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
    "localizations_per_frame": {"description": "Localizations per frame over the last window of frames fitted", "unit": "", "kind": "gauge"},
    "localizations": {"description": "Localizations so far", "unit": "", "kind": "counter"},
    "frames_fitted": {"description": "Frames fitted so far", "unit": "frames", "kind": "counter"},
    "frames_behind": {"description": "Frames written but not yet fitted", "unit": "frames", "kind": "gauge"},
    "median_photons": {"description": "Median photons per localization over the window", "unit": "photons", "kind": "gauge"},
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
    final = sent[-1]
    assert final["values"]["frames_fitted"] == len(frames) == result["output"]["frames"]
    assert final["values"]["localizations"] == result["output"]["localizations"]
    assert final["frames"] == [len(frames) - 100 if len(frames) > 100 else 0, len(frames) - 1]
    # the window is the last 100 frames, or all of them: here all 60
    assert final["values"]["localizations_per_frame"] == pytest.approx(
        result["output"]["localizations_per_frame"], rel=0.01)
    assert final["values"]["frames_behind"] == 0


def test_the_released_manifest_sends_no_metrics(worker, frames, tmp_path):
    data = GrowingNDTiff(tmp_path / "acq", frames.shape[1:])
    data.write(frames)
    out = tmp_path / "out"
    out.mkdir()
    w = worker("fit_ndtiff", GAUSSIAN, data.folder, out)
    w.writer_finished()
    assert w.result()["state"] == "succeeded"
    assert not [m for m in w.seen if m["type"] == "metrics"]
