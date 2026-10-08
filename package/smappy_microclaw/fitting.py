"""What the worker fits, and how: MicroClaw's parameters onto SMAPpy's fitters.

Every operation fits with SMAPpy's own defaults, and the job's parameters
override them.  The four fitters are SMAPpy's Localize plugins, chosen by the
two parameters the skill always asks for -- the PSF model and the number of
channels -- because each needs something different and none of it can be
guessed:

| psf, channels | fitter | needs |
| --- | --- | --- |
| gaussian, 1 | Localize/Gaussian 2D | nothing more |
| gaussian, 2 | Localize/Gaussian 2D 2C | ``transform``: the channel transformation |
| spline, 1 | Localize/Spline 3D | ``calibration``: a bead calibration |
| spline, 2 | Localize/Spline 3D 2C | ``calibration``: a dual-colour one |

The two-channel Gaussian fitter can also calibrate its transformation from the
first frames of the movie, but it reads them before the fit and a live
dataset does not have them yet, so here a file is required.

**The camera is checked before anything is fitted.**  A wrong photon
conversion is invisible in the fit and wrong in every number after it, so the
worker waits for the first frame, resolves the camera as SMAPpy does --
SMAPpy's camera database, then the file's metadata, then the job's values --
and refuses to start when the conversion, the offset or the pixel size is
missing, or when EM gain was on (or the camera reports a gain and nobody says
whether it was on) and the gain is not known.  The refusal names what is
missing; the result says, for every value, where it came from.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOCS = "locs.hdf5"
FITTERS = {("gaussian", 1): "Localize/Gaussian 2D",
           ("gaussian", 2): "Localize/Gaussian 2D 2C",
           ("spline", 1): "Localize/Spline 3D",
           ("spline", 2): "Localize/Spline 3D 2C"}
# how long the first frame may take to appear: MicroClaw starts the worker
# when the dataset is created, which is before the camera has delivered
APPEAR_SECONDS = 120.0
# the area the density is given per, in camera pixels: what the lab compares
# by eye -- "5 to 50 localizations per frame in 250 x 250 pixels"
DENSITY_AREA_PX = 250 * 250
STATUS_SECONDS = 5.0             # at most one progress line this often


class Refused(ValueError):
    """A job the worker will not start, with the reason in words."""


# ------------------------------------------------------------- operations
def run(job: dict, channel) -> int:
    operation = job["operation"]
    if operation == "self_check":
        return self_check(channel)
    if operation not in ("fit_ndtiff", "fit_and_view"):
        raise ValueError(f"unknown operation {operation}")
    output_dir = Path(job["output_dir"])
    dataset = job["input"]["dataset"]
    try:
        plugin, values = fitter_and_values(job["parameters"], dataset,
                                           output_dir / LOCS)
        from smappy.gui.live_session import fit_settings
        settings = fit_settings(plugin, values)
        channel.status(f"{plugin.split('/')[-1]}: waiting for the first frame")
        camera = check_camera(dataset, settings, channel.stop)
    except InterruptedError:
        channel.result("cancelled", {}, complete=False)
        return 0
    except Refused as why:
        channel.result("failed", {}, complete=False, failure=str(why))
        return 0
    channel.status(_camera_line(camera))
    if operation == "fit_ndtiff":
        return headless(plugin, settings, camera, output_dir, channel)
    return window(plugin, settings, camera, output_dir, channel)


def headless(plugin, settings, camera, output_dir: Path, channel) -> int:
    from smappy import plugins
    from smappy.plugins import Context

    context = Context(stop=channel.stop, writer_finished=channel.writer_finished,
                      progress=_throttled(channel.status))
    try:
        result = plugins.get(plugin)().run(context, settings)
    except Exception as error:
        state = "cancelled" if channel.stop.is_set() else "failed"
        channel.result(state, _output(plugin, camera, channel),
                       _artifacts(output_dir, "partial"), complete=False,
                       failure=f"{type(error).__name__}: {error}")
        return 0
    stopped = bool(result.data.get("stopped"))
    complete = not stopped and channel.writer_finished.is_set()
    state = "cancelled" if stopped else "succeeded"
    channel.result(state, _output(plugin, camera, channel,
                                  result.data.get("stats"), result.locs),
                   _artifacts(output_dir, "final" if state == "succeeded" else "partial"),
                   complete=complete)
    return 0


def window(plugin, settings, camera, output_dir: Path, channel) -> int:
    from smappy.gui.live_session import LiveSession

    from .runner import detach_stderr

    def finished(outcome) -> None:
        locs = live.session.locs if live.session is not None else None
        validity = "final" if outcome.state == "succeeded" else "partial"
        channel.result(outcome.state,
                       _output(plugin, camera, channel, outcome.stats, locs),
                       _artifacts(output_dir, validity), complete=outcome.complete,
                       failure=outcome.error)
        detach_stderr(output_dir)          # the window outlives the job

    live = LiveSession(plugin, settings, on_finished=finished,
                       on_progress=_throttled(channel.status),
                       stop=channel.stop, writer_finished=channel.writer_finished)
    live.run()
    return 0


def self_check(channel) -> int:
    """A fit of simulated frames, and the GUI built offscreen.

    MicroClaw runs this headless at install time and never runs the window
    operation there, so the GUI is built here too -- otherwise a Qt that does
    not load would first show itself to somebody at the microscope.
    """
    import os

    from smappy.detect import DoGFilter, DynamicCutoff, PeakFinder
    from smappy.metadata import CameraMetadata
    from smappy.pipeline import FitSettings, fit_stack
    from smappy.psf import GaussianPSF
    from smappy.simulate import LabellingSettings, camera_frames

    frames, truth = camera_frames(n_frames=40, size_px=100, seed=1,
                                  labelling=LabellingSettings(efficiency=0.03))
    camera = CameraMetadata(conversion=0.5, offset=100.0, pixelsize_um=0.1)
    locs, _ = fit_stack([(0, frames)], camera,
                        PeakFinder(DoGFilter(1.2), DynamicCutoff(1.7)),
                        GaussianPSF(sigma=1.2), FitSettings(output_unit="nm"))
    found, error = _against_truth(locs, truth, field_nm=100 * 100.0)
    if found < 0.5 or error is None or error > 30:
        raise RuntimeError(f"the self-check fit found {found:.0%} of the clean "
                           f"simulated spots, {error} nm off: SMAPpy is not "
                           f"fitting correctly in this environment")

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import PySide6
    from PySide6.QtWidgets import QApplication
    from smappy.gui.app import ControlWindow, RenderWindow
    from smappy.session import Session
    app = QApplication.instance() or QApplication(["smappy-self-check"])
    session = Session()
    render = RenderWindow(session)
    control = ControlWindow(session, render)
    control.close()
    render.close()
    app.processEvents()
    channel.result("succeeded", {"localizations": int(len(locs)),
                                 "found": found, "median_error_nm": error,
                                 "smappy": smappy_version(),
                                 "pyside6": PySide6.__version__})
    return 0


# ----------------------------------------------------------- parameters
def fitter_and_values(params: dict, dataset: str, out: Path) -> Tuple[str, Dict[str, Any]]:
    """The fitter and its dotted settings, from the job's parameters."""
    psf, channels = params["psf"], int(params["channels"])
    plugin = FITTERS[(psf, channels)]
    # positions in nm, named rather than left to the fitter's default: smappy
    # 0.3.0's settings_from loses the default a fitter declares for its fit
    # part, and the result, SKILL.md and the precision figures are all in nm
    values: Dict[str, Any] = {"source.path": str(dataset), "source.live": True,
                              "output.path": str(out), "fit.output_unit": "nm"}
    calibration, transform = params.get("calibration"), params.get("transform")
    if psf == "spline":
        if not calibration:
            raise Refused("a spline fit needs a calibration: the bead calibration "
                          "(_3dcal.mat or .h5) saved with the bead images"
                          + (", made in dual-colour mode" if channels == 2 else ""))
        values["model.calibration"] = _existing(calibration, "calibration")
    elif calibration:
        raise Refused("calibration is for psf: spline; a Gaussian fit has none")
    if psf == "gaussian" and channels == 2:
        if not transform:
            raise Refused("a two-channel Gaussian fit needs transform: the channel "
                          "transformation file (Register/Calibrate transform, or a "
                          "dual-colour bead calibration)")
        values["transform.path"] = _existing(transform, "transform")
    elif transform:
        raise Refused("transform is only for psf: gaussian with channels: 2")

    camera = {"conversion_e_per_adu": "camera.conversion",
              "offset_adu": "camera.offset",
              "em_gain": "camera.emgain",
              "em_on": "camera.em_on",
              "pixel_size_um": "camera.pixelsize_um",
              "camera": "camera.camera"}
    fit = {"roi_size_px": "fit.roisize", "cutoff": "detection.cutoff"}
    for name, key in {**camera, **fit}.items():
        if params.get(name) is not None:
            values[key] = params[name]
    if params.get("psf_sigma_px") is not None:
        values["detection.sigma"] = float(params["psf_sigma_px"])
        if psf == "gaussian":
            values["model.sigma"] = float(params["psf_sigma_px"])
    for key in ("camera.conversion", "camera.offset", "camera.emgain",
                "camera.pixelsize_um", "detection.cutoff"):
        if key in values:
            values[key] = float(values[key])
    if "fit.roisize" in values:
        values["fit.roisize"] = int(values["fit.roisize"])
    return plugin, values


def _existing(path: str, what: str) -> str:
    if not Path(path).is_absolute():
        raise Refused(f"{what} must be an absolute path, not {path}")
    if not Path(path).is_file():
        raise Refused(f"{what} {path} does not exist")
    return str(path)


# --------------------------------------------------------------- camera
def check_camera(dataset: str, settings, stop) -> Dict[str, Any]:
    """Where every camera value comes from; `Refused` when one is missing."""
    from smappy.io.tiff import resolve_camera
    from smappy.io.watch import WatchSettings, open_growing_stack

    try:
        source = open_growing_stack(dataset, WatchSettings(appear_timeout=APPEAR_SECONDS),
                                    stop_event=stop)
    except TimeoutError as error:
        if stop.is_set():
            raise InterruptedError from error
        raise Refused(f"no frame appeared in {dataset} within "
                      f"{APPEAR_SECONDS:.0f} s") from error
    if stop.is_set():
        raise InterruptedError
    resolution = resolve_camera(source, camera=settings.camera.camera,
                                overrides=settings.camera.overrides())
    values = resolution.values
    missing = list(resolution.missing)
    if values.get("em_on") and not values.get("emgain"):
        missing.append("em_gain")
    if values.get("em_on") is None and values.get("emgain"):
        missing.append("em_on")
    report = {name: {"value": _plain(values.get(name)),
                     "from": str(resolution.sources.get(name, "missing"))}
              for name in ("conversion", "offset", "pixelsize_um", "em_on", "emgain")}
    report["camera"] = resolution.camera_name or values.get("camera_name") or ""
    report["frame_px"] = [int(n) for n in source.shape]          # (height, width)
    if missing:
        names = {"conversion": "conversion_e_per_adu", "offset": "offset_adu",
                 "pixelsize_um": "pixel_size_um"}
        wanted = ", ".join(names.get(m, m) for m in missing)
        raise Refused(f"the camera of {report['camera'] or 'this dataset'} is not "
                      f"known well enough to fit: {wanted} is in neither SMAPpy's "
                      f"camera database nor the dataset's metadata; pass it")
    return report


def _camera_line(camera: Dict[str, Any]) -> str:
    parts = []
    for name in ("conversion", "offset", "pixelsize_um", "emgain"):
        entry = camera[name]
        if entry["value"] is not None:
            parts.append(f"{name} {entry['value']} ({entry['from'].split(':')[0]})")
    return f"camera {camera['camera'] or '?'}: " + ", ".join(parts)


# --------------------------------------------------------------- output
def _output(plugin, camera, channel, stats=None, locs=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"fitter": plugin, "camera": camera,
                           "smappy": smappy_version()}
    if channel.acquisition is not None:
        out["acquisition"] = channel.acquisition
    if stats:
        out["frames"] = int(stats.get("frames", 0))
        out["fit_seconds"] = round(float(stats.get("fit_seconds", 0.0)), 1)
    if locs is not None and len(locs):
        import numpy as np
        out["localizations"] = int(len(locs))
        if "photons" in locs:
            out["median_photons"] = _plain(np.median(locs["photons"]))
        if "xy_err_nm" in locs:
            out["precision_nm"] = precision(locs["xy_err_nm"])
        if out.get("frames"):
            per_frame = len(locs) / out["frames"]
            out["localizations_per_frame"] = round(per_frame, 2)
            height, width = camera.get("frame_px") or (0, 0)
            # a two-channel fit gives one localization per molecule, on half
            # the chip: the density is per channel, as one would count it
            area = height * width / (2 if plugin.endswith("2C") else 1)
            if area:
                out["localizations_per_frame_per_250px"] = round(
                    per_frame * DENSITY_AREA_PX / area, 2)
    return out


def precision(xy_err_nm) -> Dict[str, Any]:
    """The localization precision as the lab reads it: where its histogram peaks.

    SMAPpy's Statistics plugin (`precision_distribution`): the histogram's
    maximum, the maximum of the model the exponential photon distribution
    implies, and that model's ``sigma_c`` (the precision at the mean photon
    count).  Not the median, which the long tail of dim localizations pulls up.
    """
    from smappy.plugins.statistics import precision_distribution
    stats = precision_distribution(xy_err_nm).stats
    return {"histogram_max": _plain(stats.get("histogram_max")),
            "model_max": _plain(stats.get("max")),
            "sigma_c": _plain(stats.get("sigma_c"))}


def _artifacts(output_dir: Path, validity: str):
    from .runner import artifact
    path = output_dir / LOCS
    return [artifact(output_dir, path, validity)] if path.is_file() else []


def _throttled(say, seconds: float = STATUS_SECONDS):
    last = [0.0]

    def report(text: str) -> None:
        now = time.monotonic()
        if now - last[0] >= seconds:
            last[0] = now
            say(text)
    return report


def _against_truth(locs, truth, field_nm: float, photons: float = 500.0,
                   within_nm: float = 50.0, margin_nm: float = 700.0):
    """How many clean true spots were found, and how far off: ``(found, error)``.

    Clean as SMAPpy's own tests define it -- brighter than ``photons`` and no
    other spot within `ISOLATED_NM` in the same frame -- because crowded spots
    are pulled towards each other and dim ones say nothing about the fitter.
    The simulated structure may be larger than the camera's field
    (``field_nm`` square), so only spots ``margin_nm`` inside it count: about
    half an ROI, the closest to the edge a spot can be fitted.
    """
    import numpy as np
    from smappy.simulate import ISOLATED_NM

    x, y = np.asarray(truth["x_nm"]), np.asarray(truth["y_nm"])
    inside = ((x > margin_nm) & (x < field_nm - margin_nm) &
              (y > margin_nm) & (y < field_nm - margin_nm))
    clean = (np.asarray(truth["photons"]) > photons) & inside & \
            (np.asarray(truth["neighbour_nm"]) > ISOLATED_NM)
    errors, n = [], int(clean.sum())
    for frame in np.unique(np.asarray(truth["frame"])[clean]):
        mine = np.asarray(locs["frame"]) == frame
        if not mine.any():
            continue
        true = clean & (np.asarray(truth["frame"]) == frame)
        dx = np.asarray(truth["x_nm"])[true][:, None] - np.asarray(locs["x_nm"])[mine][None, :]
        dy = np.asarray(truth["y_nm"])[true][:, None] - np.asarray(locs["y_nm"])[mine][None, :]
        nearest = np.sqrt(dx ** 2 + dy ** 2).min(axis=1)
        errors.extend(nearest[nearest < within_nm])
    found = len(errors) / n if n else 0.0
    return round(found, 3), (_plain(np.median(errors)) if errors else None)


def _plain(value):
    """A JSON value: numpy scalars and sequences as Python ones."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else None
    return value


def smappy_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("smappy-smlm")
    except PackageNotFoundError:
        return "unknown"
