# SMAPpy localization fitting

SMAPpy fits single-molecule localization microscopy (SMLM) data -- dSTORM,
PALM, PAINT -- by maximum-likelihood fitting of every blinking molecule. This
package runs SMAPpy on a NDTiff dataset that MicroClaw is acquiring or has
saved. It produces a localization table (`locs.hdf5`), and with
`fit_and_view` it shows the reconstruction growing in SMAPpy's own window
while the camera runs.

## When to use it

- The user acquires SMLM data and wants to see the super-resolution image
  while acquiring, to judge blinking density, focus, drift and labelling:
  `fit_and_view` through the `analysis` argument of the acquisition tool.
- The user wants an existing SMLM dataset fitted: `fit_and_view` (to look at
  it) or `fit_ndtiff` (no window) through `run_analysis_on_saved_dataset`.
- Use `fit_ndtiff` when MicroClaw refuses `fit_and_view` because this
  computer shows no desktop (a service, or a remote session), or when nobody
  will look at the window.

Not for: ordinary widefield or confocal images; data in which single
molecules are not separated in most frames; steering the acquisition (a
result never feeds back into MicroClaw).

## Before you call it: ask, do not guess

Three things decide the fit and cannot be read from the data. Ask the user
for them, every time, unless they told you in this session:

1. **PSF model** (`psf`): `gaussian` for a 2D fit, which needs nothing more;
   or `spline` for a 3D fit with an experimental PSF, which needs
   **`calibration`**: the absolute path of the bead calibration
   (`*_3dcal.mat`, or `.h5` from SMAPpy's bead calibration). It is usually
   saved in the folder with the bead images it was made from; ask the user
   which file, and do not pick one yourself.
2. **Channels** (`channels`): `1`, or `2` for a camera image split into two
   channels (two colours, or biplane).
   - Two channels with `spline`: `calibration` must be a dual-colour
     calibration.
   - Two channels with `gaussian`: **`transform`** is required, the absolute
     path of the channel transformation file (or a dual-colour bead
     calibration). It is usually saved with the bead data too.
3. **The camera's photon conversion.** SMAPpy needs the conversion
   (photoelectrons per count), the offset (baseline, counts) and the pixel
   size, and for an EMCCD whether EM gain was on and the gain. It takes them
   from SMAPpy's camera database when it recognises the camera, then from the
   dataset's metadata (Micro-Manager records pixel size and often the offset
   and EM gain), then from what you pass. Pass what the user tells you:
   `conversion_e_per_adu`, `offset_adu`, `pixel_size_um`, `em_on`, `em_gain`.
   **Never invent a conversion.** If you do not know it, do not pass it: the
   worker checks before fitting and refuses with a message naming what is
   missing. Then ask the user for exactly that value and run again.

Everything else uses SMAPpy's defaults, which suit most data. Change them only
when the user asks: `roi_size_px` (default 13; smaller for dense data),
`cutoff` (default 1.7: in each frame a candidate counts when it is brighter
than the median of the frame's local maxima plus 1.7 times their 20-80 %
spread; higher finds fewer, brighter molecules), `psf_sigma_px` (default 1.2 camera pixels).

## How to call it

The adapter is `ries-lab/smappy:<operation>`, with the `release_digest` you see
when this skill is loaded.

- **During an acquisition:** pass
  `analysis: {adapter: "ries-lab/smappy:fit_and_view", release_digest, parameters}`
  to `run_timelapse` or another acquisition tool. Fitting starts as frames are
  written and keeps going through pauses; it finishes once MicroClaw reports
  that the dataset is complete.
- **On a saved dataset:** `run_analysis_on_saved_dataset` with
  `adapter="ries-lab/smappy:fit_and_view"` (or `fit_ndtiff`), the
  `release_digest` and the parameters.

Examples of `parameters`:

- 2D, one channel, camera known to SMAPpy: `{"psf": "gaussian", "channels": 1}`
- 3D, one channel, sCMOS:
  `{"psf": "spline", "channels": 1, "calibration": "D:\\data\\beads\\beads_3dcal.mat", "conversion_e_per_adu": 0.48, "offset_adu": 100}`
- 2D, two channels, EMCCD:
  `{"psf": "gaussian", "channels": 2, "transform": "D:\\data\\beads\\transform.h5", "em_on": true, "em_gain": 100}`

Tell the user before the run that `fit_and_view` opens a SMAPpy window on the
microscope computer, and that the window stays open after the analysis ends
until they close it.

## What comes out

- **`locs.hdf5`** in the job's output folder: the localization table, final
  when the run succeeded, partial when it was cancelled. SMAPpy opens it
  (`smappy-gui locs.hdf5`, or File > Open).
- The **window** (`fit_and_view`): SMAPpy's own GUI, with the reconstruction
  updating every few seconds during the acquisition. When the fit is done the
  user can work on in it: filter, drift-correct, measure. Whatever they save
  goes to a new file; `locs.hdf5` stays exactly as the job recorded it.
  Closing the window before the fit is done cancels the fit and keeps what it
  fitted so far.
- **Progress** in status lines: the frame number, frames per second and the
  number of localizations so far.
- **The result's `output`**: `frames`, `localizations`,
  `localizations_per_frame`, `localizations_per_frame_per_250px` (the density:
  per frame in an area of 250 x 250 camera pixels, per channel for a
  two-channel fit), `median_photons`, `precision_nm`, `fit_seconds`, and
  `camera`: every camera value with where it came from ("user", "metadata",
  or the camera database). Report the camera's sources to the user. A value
  from "user" is one you passed.
- **`precision_nm`** is the localization precision as SMAPpy's Statistics
  plugin reports it: `histogram_max`, where the histogram of the
  localizations' precision peaks; `model_max`, the peak of the distribution
  the photon counts imply, fitted to that histogram; and `sigma_c`, that
  model's precision at the mean photon count. Judge the precision by
  `histogram_max` (or `sigma_c`), not by a mean or median, which dim
  localizations pull up.

How to read the numbers. Typical of a good dataset:

| technique | localizations per frame per 250 x 250 px | precision | photons |
| --- | --- | --- | --- |
| dSTORM, DNA-PAINT | about 5 to 50 | 2 to 8 nm | 1000 to 10000 |
| PALM | about 5 to 50 | 4 to 20 nm | 250 to 3000 |

Far more localizations per frame usually means the blinking is too dense to
fit (molecules overlap); near zero means too few molecules are on, or the
cutoff is too high. A precision well above the range, with photons in it,
points at defocus or a wrong camera conversion; photons well below the range
point at the laser power, the buffer or the dye. Say which numbers are out of
range and let the user decide; do not change parameters on your own.

## If it fails

The failure message says what was wrong. The common ones:

- *"... is in neither SMAPpy's camera database nor the dataset's metadata;
  pass it"*: ask the user for that camera value.
- *"a spline fit needs a calibration"* or *"... needs transform"*: ask the
  user for the file.
- *"... does not exist"* or *"must be an absolute path"*: the path is wrong.
- *"no frame appeared ... within 120 s"*: the camera wrote nothing.

Report anything else to the user as it is; problems with the fit itself go to
SMAPpy's publisher through *Report to publisher*.
