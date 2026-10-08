# smappy-microclaw

[SMAPpy](https://github.com/ries-lab/SMAPpy)'s single-molecule localization
fitting as a [MicroClaw](https://github.com/Micro-Claw/microclaw) skill
package: MicroClaw acquires, and SMAPpy fits the dataset as it is written,
with the reconstruction growing in SMAPpy's own window.

| operation | what it does |
| --- | --- |
| `self_check` | fits simulated frames and builds SMAPpy's GUI offscreen; run by MicroClaw at install |
| `fit_and_view` | opens SMAPpy on the dataset with the fit running (`opens_window`, protocol 1.1); the window stays open after the job |
| `fit_ndtiff` | the same fit without a window, for a computer without a desktop |

Both fits write `locs.hdf5` to the job's output folder, wait out pauses in the
acquisition, and finish when MicroClaw says the writer is done.  What the agent
is told is in [`package/SKILL.md`](package/SKILL.md); the parameters are in
[`package/manifest.json`](package/manifest.json).

## Layout

```
package/                     the release, zipped as it is
  manifest.json              source manifest (pack adds artifact, assets, locks)
  SKILL.md                   what MicroClaw's agent reads
  smappy_microclaw/
    runner.py                the microclaw.analysis.v1 protocol, stdlib only
    fitting.py               parameters onto SMAPpy's fitters, the camera check
locks/pylock.win_amd64.toml  the environment MicroClaw installs
requirements.in              what the lock is compiled from
tests/                       the worker driven as MicroClaw drives it
```

The fit itself is SMAPpy's: `smappy.gui.live_session` (the GUI on a fit
another program controls) and the Fit plugins' `Context.stop` and
`writer_finished`.  This package is only the protocol and the mapping.

## Developing

```sh
pip install -e ../SMAPpy            # or smappy-smlm from PyPI
PYTHONPATH=../microclaw python -m pytest tests -q -n 4
```

`microclaw` must be importable for the tests: every line the worker prints is
checked with MicroClaw's own `validate_worker_message`.

The lock, after a change to `requirements.in`:

```sh
uv pip compile --no-config --python-version 3.12 --python-platform x86_64-pc-windows-msvc requirements.in -o locks/pylock.win_amd64.toml
```

## Releasing

Bump `version` in `package/manifest.json`, push the tag `v<version>`; the
release workflow packs, signs (`MICROCLAW_PUBLISHER_KEY`) and publishes
`package.zip` and `release.json`, and `release.json` then goes to
[Micro-Claw/package-catalog](https://github.com/Micro-Claw/package-catalog) by
PR.  See MicroClaw's
[publishing guide](https://github.com/Micro-Claw/microclaw/blob/main/docs/publishing-skill-packages.md).
