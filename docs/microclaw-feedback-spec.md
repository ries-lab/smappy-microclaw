# Live analysis feedback: package metrics that steer an acquisition

A specification for MicroClaw (register row `R139`), written by the
smappy-microclaw side.  It is meant to be read by the people who decide and
by the agent that implements: sections 1–4 explain the problem and the
choices; section 5 is the normative part an implementation follows; sections
6–8 are the tests, the open questions and the package side.

Status: **proposal**, 2026-10-13.  Nothing here is implemented in MicroClaw.
MicroClaw file references are to `Micro-Claw/microclaw` at `70976ee`.

## 1. In one paragraph

A community package worker (here: SMAPpy's live localization fit) may send
**numbers** while it runs — "the localizations counted in each frame",
"total localizations", "how many frames I am behind".  MicroClaw records them
and shows them, and — only when the caller asked for it when planning the
acquisition, and the user confirmed it — acts on them through **two typed
actions MicroClaw already has the safety rules for**: change one illumination
power (for example the 405 nm activation laser) inside an authorized envelope,
and stop the acquisition early, keeping its data.  The package never gets
hardware access; MicroClaw does the writing, under its own guard.

## 2. Why

Single-molecule localization microscopy (SMLM) needs the density of active
emitters held roughly constant: too few and the acquisition takes hours, too
many and spots overlap and fit badly.  The operator does this by hand with the
405 nm activation laser, raising it as fluorophores bleach.  The fitted
localizations per frame are the right signal for it, and SMAPpy computes them
live.  The second wish is a stopping rule: "stop when 2 million localizations
are collected", or "stop when the density stays below 0.5 per frame at full
405 power" (the sample is exhausted).

Today this is impossible by design (`design/83` §"What a package can be"): a
worker is a read-only observer with no channel back.  `R139` already says what
the fix must look like — "a new typed MicroClaw capability that reads a
worker's declared telemetry and acts on it under the normal authorization,
envelope and write-budget rules, with a latency contract".  This document
proposes that capability.

## 3. What exists today, and why it is not enough

| piece | where | why it does not do this |
| --- | --- | --- |
| worker `status` messages | `skill_supervisor.py` `JobHandle._stdout` (~428–502) | free text, ≤512 chars, kept in a 256-line deque, surfaced only through the job record; no numbers, nothing reads them |
| `analysis_job_status` tool | `tools.py` ~6151 | reads the durable record only; the agent's turn is blocked inside the acquisition tool anyway |
| hook `SetIlluminationPower` + `illumination_envelope` | `hook_decisions.py` 75–79, `_dispatch` 972–1062; `tools.py` `_configure_hook_capabilities` 8713–8770 | the right *safety* machinery, but a hook runs on the image callback, cannot import package code, and **a per-frame hardware action is refused inside a hardware-sequenced batch** — which is exactly what an SMLM run at `interval_s=0` is |
| hook `StopAcquisition` | `hook_decisions.py` 1166, adaptive runners only | refused in fixed runners (1072–1080); the adaptive runner costs ~41 ms of dispatch per frame on M5 (`design/65`), too slow for 10–30 ms SMLM exposures |
| UI Stop | `webserve.py` 1318–1334 | cooperative, checked between tool calls; does not interrupt a running `run_timelapse` |

So the safety rules exist; what is missing is (a) a typed numeric channel
from the worker, and (b) something that acts on it **asynchronously to the
frame stream**, at a slow rate, while a sequenced acquisition runs.

## 4. Decisions, and the reason for each

- **D1 — Metrics are declared, numeric and named by the package.**  The
  manifest declares each metric an operation may send (name, unit, one-line
  description).  A message carrying an undeclared name, a non-finite number
  or anything but a number is a protocol failure, as for any other malformed
  line.  MicroClaw never interprets a name except the one a feedback rule
  names.  *Reason:* the trust boundary stays where `design/83` put it —
  publisher JSON is opaque data — and an agent can see what a package offers
  before using it.
- **D2 — A protocol bump to `1.2`, the wire name unchanged.**  As `1.1` added
  `opens_window` (`design/84` D1), `1.2` admits the manifest key `metrics`
  and the stdout message type `metrics`, both only for releases that declare
  `"protocol_version": "1.2"`.  A `1.0`/`1.1` worker is unaffected.
- **D3 — The package sends raw per-frame counts; the control law lives in
  MicroClaw, as a small built-in rule with three parameters.**  The worker
  does no smoothing: it sends N for every frame (zeros included), and the law
  is the one the lab used before with the 405 nm laser,

  `uv_i = clamp(uv_(i-1) + k · (1 − N_i / N_s), uv_min, ceiling)`,

  applied once per frame in frame order, where `N_s` is the setpoint and the
  gain `k` sets the smoothing.  *Reason:* smoothing in the worker (a window of
  frames) adds lag and hides what the law needs; the gain is the operator's
  knob.  The user says "keep 5 localizations per frame with the 405, at most
  30 %", not a program (CLAUDE.md "Microclaw is easy to use"); the rule is
  reviewable as data and the package stays an observer.  A custom law in a
  hook can come later by giving hooks read access to the same metrics (§5.7),
  but a hook cannot act per frame inside a hardware-sequenced burst today, so
  it does not replace this.
- **D4 — Every write goes through the existing illumination guard.**  The
  envelope is the same four keys as `illumination_envelope`
  (`device, property, max_power_percent, max_writes`), validated by the same
  code, checked per write by `guard.check_illumination` with the rig's step
  ratchet.  No new safety path.
- **D5 — The controller runs on its own thread, not on the image callback;
  the law is applied per frame, the laser is written at a bounded rate.**
  Counts arrive in messages of several consecutive frames; the controller
  applies the law to each frame in order and writes the resulting power once
  per message, at most every `write_interval_s` (default 0.2 s) and only when
  it moved by at least `min_change_percent`.  *Reason:* SMLM runs are
  hardware-sequenced bursts, where no per-frame software runs, and the laser
  should not see a write per frame at 100–1000 frames/s; applying the law per
  frame keeps its meaning (and `k`'s units) independent of how frames happen
  to be batched.  Whether a
  property write from another Python thread is safe while AcqEngJ runs a
  sequence is **not established** — §7, Q1, a rig probe before anything ships.
- **D6 — Stale data never raises power and never stops a run.**  A metric
  older than `max_age_s` (default 3 s), a worker that failed or exited, or a
  worker that reports itself more than `max_frames_behind` frames behind the
  camera, makes the controller *hold*: no increase, no stop.  A decrease is
  still allowed (as a non-increasing write is today, `design/32` 7b).
- **D7 — An early stop is a normal end, not a failure.**  The acquisition
  ends, the writer finishes, the data is kept, the analysis job gets its
  ordinary `writer` notification and completes, and the result says
  `stop_reason: "analysis_rule"` with the metric value that triggered it.
- **D8 — Consent once, at plan time, with the rest of the acquisition.**  The
  feedback rule is an argument of the acquisition tool, next to `analysis`;
  `execute_tool` validates it and asks one confirmation that discloses the
  laser, ceiling, write budget and stop rule — the shape of today's
  `kind="illumination"` confirmation.  No prompt during the run.
- **D9 — Export refuses with a reason.**  An exported script cannot run the
  package worker (it must import nothing from MicroClaw and is standalone), so
  a closed loop on package analysis cannot be reproduced; emitting the
  recorded power trace instead would be "the trace, not the program" that
  CLAUDE.md forbids.  The acquisition still emits; the feedback emits
  `CannotEmit("closed-loop feedback on package analysis …")` with the recorded
  writes in a comment.  (Decision for the MicroClaw maintainers to confirm.)

## 5. Specification

### 5.1 Manifest (`skill_packages.validate_manifest`)

- `SUPPORTED_PROTOCOLS` gains `"1.2"` (still `microclaw.analysis.v1` on the wire).
- An operation of a `1.2` release may carry `metrics`: a JSON object, at most
  32 entries, each key a metric name matching `[a-z][a-z0-9_]*` (≤64 chars),
  each value a closed mapping:
  - `description` (string, one line, ≤512) — required;
  - `unit` (string, ≤32, may be `""`) — required;
  - `kind` — required: `"gauge"` (a value now), `"counter"` (non-decreasing
    during a job), or `"per_frame"` (one value for every frame — the message
    carries a list, one number for each frame of its `frames` range).
- `metrics` on `self_check`, or in a `1.0`/`1.1` release, is refused.
- `opens_window` stays allowed under `1.2`.

Example (SMAPpy's `fit_ndtiff` / `fit_and_view`):

```json
"metrics": {
  "localizations_per_frame": {"description": "Localizations counted in each frame, those passing the count filter", "unit": "", "kind": "per_frame"},
  "localizations": {"description": "Localizations so far, all of them", "unit": "", "kind": "counter"},
  "frames_fitted": {"description": "Frames fitted so far", "unit": "frames", "kind": "counter"},
  "frames_behind": {"description": "Frames written but not yet read by the fit", "unit": "frames", "kind": "gauge"}
}
```

### 5.2 Wire: the `metrics` message (worker → MicroClaw)

```json
{"protocol":"microclaw.analysis.v1","type":"metrics","job_id":"<32 hex>",
 "frames":[120,124],
 "values":{"localizations_per_frame":[5,3,0,7,4],"localizations":601,"frames_behind":0}}
```

- Closed key set: `protocol, type, job_id, frames, values`.
- `frames`: two non-negative integers, `first <= last`: the **dataset frame
  ordinals** (0-based position in the order the dataset's images were
  written, i.e. NDTiff index order) the values describe.  Counters describe
  `[0, last]`.  This is how a controller knows how old the evidence is in
  acquisition terms, not only in wall-clock terms.  At most 2,000 frames per
  message.
- `values`: non-empty object; every key a metric declared for this operation.
  A `gauge` or `counter` is a finite JSON number (`bool` is not a number); a
  `per_frame` metric is a list of `last − first + 1` finite numbers, in frame
  order.  A message need not carry every metric.
- `last` never decreases within a job; a message whose `last` is lower than the
  previous one's is a protocol failure.  A `counter` never decreases.
- **Per-frame values are contiguous**: a message carrying a `per_frame` metric
  starts at the frame after the previous such message ended
  (`first == previous last + 1`), so every frame is counted exactly once and a
  frame with no localizations is sent as `0`, never skipped.
- **At high frame rates a message carries many frames.**  The worker sends
  at most 10 messages a second, each with every frame fitted since the last
  one: at 1,000 frames/s about 100 frames a message; when the fit catches up
  after falling behind, up to 2,000, split into further messages beyond that.
  A per-frame count is a few bytes, so 2,000 frames stay far inside the line
  bound.  Nothing is lost or averaged: the controller applies the law to each
  frame and writes once (§5.5), so the frame rate changes how many frames a
  write answers for, not what the law computes.
- Allowed only for operations that declare `metrics`, and only before the
  `result`.  The 65,536-byte line bound applies.
- Rate: the worker sends at most **10 per second**.  The supervisor does not
  fail a faster worker; it keeps the newest and counts the rest as coalesced.

`validate_worker_message` gains the type behind a `metrics=` argument (the
operation's declaration), so `1.0`/`1.1` validation is byte-for-byte as today.
A reference validator is in §9; the package's tests use it.

### 5.3 Supervisor

- On each valid `metrics` line, `JobHandle` stamps `received_monotonic` and
  `received_at` (UTC), and updates, under its lock, a `latest` map
  `name -> {value, frames, received_at}` for gauges and counters.
  `per_frame` values go to a bounded queue (≤ 100,000 frames) that the
  controller drains in order; a full queue drops the oldest and marks the
  feed as having lost frames (stale, D6).  These are the only state the
  controller reads.
- It keeps a bounded history for the record: one sample per second at most,
  1024 samples (ring; per-frame metrics as their mean over the second), plus
  `metrics_received` and `metrics_coalesced` counts.
- The job record gains `metrics: {declared, latest, history, received,
  coalesced}`; it is persisted on the existing lifecycle/terminal writes and
  additionally at most every 5 s while metrics arrive (so a crash keeps
  recent values).
- A worker failure stops the feed: `latest` keeps its last values but the
  handle reports `feed_alive = False`, which the controller treats as stale.

### 5.4 Seeing the numbers

- `analysis_job_status` returns the `metrics` block (from the durable record,
  as today).
- The package panel's job row shows the latest values with units, refreshed
  with the panel's existing polling.  Nice to have; not needed for control.
- The acquisition tool's result carries, per analysis job, the last metrics
  and, if a feedback rule ran, its summary (§5.5, "record").

### 5.5 The feedback capability

**Argument**, on the tools in `ANALYSIS_TOOLS` (only together with
`analysis`, whose job is the metric source):

```json
"analysis_feedback": {
  "metric": "localizations_per_frame",
  "max_age_s": 3.0,
  "max_frames_behind": 500,
  "illumination": {
    "envelope": {"device": "405", "property": "Power %", "max_power_percent": 30, "max_writes": 20000},
    "setpoint": 5.0,
    "gain_percent": 0.02,
    "min_percent": 0.5,
    "start_percent": null,
    "write_interval_s": 0.2,
    "min_change_percent": 0.05
  },
  "stop": {"metric": "localizations", "at_least": 2000000}
}
```

- `metric` must be a `per_frame` metric the operation declares.
- `setpoint` is `N_s`; `gain_percent` is `k`, in percent of laser power per
  frame (at `N = 0` the power rises by `k` each frame; at `N = 2 N_s` it falls
  by `k`); `min_percent` is `uv_min`, the floor the power never falls below.
  `k` is per frame, so the same `k` reacts faster, in seconds, at a higher
  frame rate; the confirmation states it also per second at the planned
  frame rate (`k × frames/s`), the number an operator can judge.
- `max_writes` counts increasing writes, as for hooks: at the default 0.2 s
  it allows up to 5 a second, so a 30-minute run needs a budget in the
  thousands; the confirmation says so in those terms.  `illumination` and
  `stop` are each optional, at least one present.  `stop.metric` may name any
  declared metric; exactly one of `at_least` / `at_most` is given.
  (Exhaustion — "below X while at full power for N s" — is a later extension;
  keep the shape open for it.)
- Validation, before any hardware moves, in `execute_tool` next to the
  `analysis` preparation: the envelope through the same code path as
  `illumination_envelope` (`_configure_hook_capabilities`'s checks:
  `guard.is_illumination_power`, finite ceiling ≤ the configured
  `illumination.max_power_percent`, positive integer `max_writes`); the
  metric names against the release's manifest; `setpoint > 0`,
  `gain_percent > 0`, `0 <= min_percent <= max_power_percent`,
  `write_interval_s >= 0.05`, `min_change_percent >= 0`.
  Refused together with an `illumination_envelope` for a hook on the same
  device (two writers on one property).
- Add `analysis_feedback` to `HOOK_CAPABILITY_ARGS`, or wherever the
  parameterized refusal matrix will generate its cases (CLAUDE.md: "add a new
  capability there first").
- **Confirmation**: one, at plan time, folded into the analysis confirmation
  or right after it: device/property, ceiling, floor, write budget, start
  value, setpoint and gain, stop rule.  A decline is "nothing was acquired", as for a
  declined analysis.

**Controller** — a thread started after the dataset exists and the analysis
job was submitted, stopped after the acquisition's `with` block exits:

```
written = read initial power once (the parent's baseline, as hooks do)
uv = start_percent if given else written;  uv = clamp(uv, min_percent, ceiling)
if uv != written: write uv (guard-checked); written = uv
until the acquisition ends:
    take the per-frame counts that arrived, in frame order (wait ≤ 50 ms)
    stale = (not handle.feed_alive) or lost frames
            or no new frame for max_age_s
            or latest["frames_behind"] > max_frames_behind (if declared)
    for each frame's N (in order):
        if illumination configured:
            step = gain_percent * (1 - N / setpoint)
            if stale and step > 0: step = 0          # D6: hold increases
            uv = clamp(uv + step, min_percent, ceiling)
    if stop configured and not stale and the stop condition holds:
        request a clean stop (below); record; stop deciding
    if illumination configured and |uv - written| >= min_change_percent
            and now - last_write >= write_interval_s:
        write uv through the illumination guard (envelope ceiling, budget for
        increases, guard.check_illumination with the rig's step ratchet)
        on success: written = uv
        on refusal: record it; uv = written (the law continues from what the
                    laser really has)
```

`min_percent` keeps the power off zero, as the old implementation did: the
law then never stalls at an off laser, and the rig's step ratchet (a ratio
between consecutive writes) has something to be a ratio of.  The rule is
deliberately simple; its constants are arguments, its source is MicroClaw's,
and every step is recorded.

**Clean stop** — the acquisition ends at the next safe point and keeps its
data: for an event-generator runner, stop putting events (as `_done_early`
does); for a hardware-sequenced burst, the mechanism is **open** (§7, Q2:
`Acquisition.abort()` and what it does to the NDTiff writer and teardown must
be measured).  Either way: writer finishes normally, the analysis job gets
`acquisition` and `writer` notifications as for any run, `input_complete` is
true, and the result carries `stop_reason: "analysis_rule"`.

**End of run**: the controller stops deciding when the acquisition's `with`
block exits.  No automatic final write (the `design/32` 7b ruling for hooks),
unless the caller adds `illumination.end_percent`, written once after the
run through the same guard — proposed, for the maintainers to accept or drop.

**Record**: every decision (time, frames, metric value and age, old → new
power or hold/refusal reason, read-back if any) appended to a log in the
reserved analysis output directory, and summarized in the tool result:
`analysis_feedback: {writes, increases, holds_stale, refusals, final_percent,
stop_reason, stop_value}`.  The write timings join the run's
`duration_breakdown`.

**Export**: §4 D9.

### 5.6 Timing contract

- Worker side (SMAPpy, measured 2026-10-13 on a 4-core container, NDTiff at
  100 frames/s): frame written → its localizations available, median 0.04–0.08 s,
  max 0.17 s.  The worker sends a `metrics` message after each fitted block,
  coalesced to ≤10/s.  So the metric lags the camera by about 0.1–0.2 s when
  the fitter keeps up; `frames_behind` says when it does not.
- Supervisor: a `metrics` line becomes `latest` within one pipe read (no
  added queue).
- Controller: the law per frame as counts arrive; a write at most every
  `write_interval_s` (default 0.2 s); a write is the setter's 41–97 µs plus
  the bridge round trip (`design/78`).  So the power answers a frame about
  0.1–0.3 s after it was written, whatever the frame rate.
- **Gate measurement**: on the rig, during a sequenced burst, report per
  decision: frame ordinal written → metric received → write applied, and
  whether frame cadence changed with the controller on vs off
  (`duration_breakdown`, gap summary, n sized to the spread — CLAUDE.md
  "Engineering principles").

### 5.7 Later, not now

- Hooks may read the same `latest` metrics (a read-only accessor in the hook
  context) for custom laws in non-sequenced runs.  Same feed, no new channel.
- More actions (another property, a stage) only as further typed capabilities.

## 6. Acceptance tests (MicroClaw)

1. A `1.2` manifest with `metrics` validates; the same key in `1.0`/`1.1`, on
   `self_check`, or with a malformed entry is refused (parametrized, like
   `tests/test_skill_packages.py` 1164–1173 for `opens_window`).
2. `validate_worker_message`: a valid `metrics` line passes; undeclared name,
   non-finite or boolean value, `first > last`, decreasing `last`, decreasing
   counter, a per-frame list of the wrong length, a gap or overlap between
   per-frame messages, more than 2,000 frames, extra key, message after
   `result`, `metrics` in a `1.1` job — each is a protocol failure.  `1.0`/`1.1` behaviour unchanged.
3. Supervisor: `latest` updated per line; history ≤1/s and ≤1024; coalesced
   count; record persisted with `metrics`; a worker exit sets `feed_alive`
   false.
4. Feedback argument: every refusal in §5.5 raised before hardware moves
   (counted: no `Acquisition` constructed); declined confirmation acquires
   nothing.
5. Controller against a fake handle and a fake core, driven by a fake clock:
   `N = N_s` leaves the power alone; `N = 0` raises it by `k` per frame up to
   the ceiling and stops there; `N = 2 N_s` lowers it by `k` per frame down to
   `min_percent` and never below; a message of several frames gives the same
   power as the frames applied one by one, in one write; writes respect
   `write_interval_s` and `min_change_percent`; budget exhausted refuses
   increases but allows decreases, and the law continues from the written
   value; stale data, a dead feed, lost frames or `frames_behind` over the
   limit hold increases; the stop rule fires once, only on fresh data;
   nothing is written after the acquisition ends.
6. A run with an early stop: data kept, writer finished, analysis job
   completes with `input_complete: true`, result `stop_reason:
   "analysis_rule"`.
7. Export: a session with `analysis_feedback` exports a script that compiles,
   runs the acquisition, and carries the refusal comment and the recorded
   writes; the session's other steps are unaffected.
8. **Watched fail**: each test fails on the tree before the change, for its
   stated reason (CLAUDE.md block workflow, step 3).
9. **Bridge-shaped fakes**: the controller's core fake returns Java-shaped
   values (`size()/get(i)`, locale-formatted property text) — the property
   read is text and must not be `float()`-coerced blindly (CLAUDE.md,
   "A value read over the bridge is locale-formatted text").

## 7. Open questions to settle on a rig before building §5.5

- **Q1 — Is a property write from a second Python thread safe while AcqEngJ
  runs a hardware-sequenced burst?**  pyjavaz serializes bridge calls; the
  question is whether a write interleaves with the engine without stalling
  frames or failing.  Probe: a 10 ms burst of 5,000 frames on the demo machine
  and on a rig, a thread writing the activation laser's power at 1 Hz, versus
  no writes; compare frame gaps and count failures.
- **Q2 — How does a sequenced burst stop cleanly?**  Does
  `Acquisition.abort()` finish the NDTiff writer and leave a readable dataset
  with every frame written so far, and how long does teardown take (`design/60`)?
  Probe on the demo machine first.
- **Q3 — Is the laser's power property safe to change while it emits?**  Rig
  specific; belongs in the rig profile, not in code.

## 8. The package side (smappy-microclaw), for reference

- Manifest `protocol_version: "1.2"`, the four metrics of §5.1 on `fit_ndtiff`
  and `fit_and_view`.
- After each fitted block (SMAPpy fits each read at once since 2026-10-13,
  SMAPpy `NOTES.md`), the worker sends the per-frame counts of every frame
  fitted since its last message — zeros included — with the counters, at
  most 10 messages a second and 2,000 frames a message.
- Only **secure** localizations are counted, so the controller is not fed
  noise: by default those with at least 500 photons, a precision of 15 nm or
  better (SMAPpy's `xy_err_nm`) and a relative log-likelihood of at least −1
  (`logl_rel`, SMAP's LLrel).  The job can change each bound:
  `count_min_photons`, `count_max_precision_nm`, `count_min_logl_rel`.
  `localizations` (the counter) is always all of them.
- `frames_behind` comes from SMAPpy's watch (`on_behind`): frames in the
  NDTiff index not yet read.  The reader runs at most two blocks ahead of the
  fitter, so frames read but not yet fitted are not counted; that is at most
  two *frames per block* (default 200 each).
- Needs SMAPpy after 0.4.0 (`LiveSession(on_stream=...)` and the `fitted`
  event, SMAPpy `474bc99`); under smappy 0.4.0 the worker sends no metrics.
- SMAPpy's own GUI keeps showing everything; the metrics are a side channel.

A prototype of this side is in this repository and tested against §9's
validator, executed from this document (`tests/test_metrics.py`): under a 1.2
manifest both fit operations send metrics that pass it and agree with the
job's result, and under the released 1.1 manifest the worker sends none.  So
the interface is known to be producible before MicroClaw builds the consumer.

## 9. Reference validator for the `metrics` message

```python
import math

MAX_FRAMES = 2000


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_metrics_message(message, *, job_id, declared, previous_last=None,
                             previous_counters=None, previous_per_frame_last=None):
    """Raise ValueError if `message` is not a valid metrics line for this job.

    declared: the operation's manifest `metrics` mapping.  The previous_*
    arguments carry the state of this job's earlier messages (None for the
    first).  Returns `last`.
    """
    if set(message) != {"protocol", "type", "job_id", "frames", "values"}:
        raise ValueError("metrics: wrong keys")
    if message["protocol"] != "microclaw.analysis.v1" or message["type"] != "metrics":
        raise ValueError("metrics: wrong protocol or type")
    if message["job_id"] != job_id:
        raise ValueError("metrics: wrong job")
    frames = message["frames"]
    if (not isinstance(frames, list) or len(frames) != 2
            or not all(type(f) is int and f >= 0 for f in frames)
            or frames[0] > frames[1]):
        raise ValueError("metrics: frames must be [first, last], 0 <= first <= last")
    first, last = frames
    if last - first + 1 > MAX_FRAMES:
        raise ValueError(f"metrics: more than {MAX_FRAMES} frames")
    if previous_last is not None and last < previous_last:
        raise ValueError("metrics: last frame went backwards")
    values = message["values"]
    if not isinstance(values, dict) or not values:
        raise ValueError("metrics: values must be a non-empty object")
    per_frame = False
    for name, value in values.items():
        if name not in declared:
            raise ValueError(f"metrics: {name} is not declared")
        kind = declared[name]["kind"]
        if kind == "per_frame":
            per_frame = True
            if (not isinstance(value, list) or len(value) != last - first + 1
                    or not all(_number(v) for v in value)):
                raise ValueError(f"metrics: {name} must be one finite number per frame")
        elif not _number(value):
            raise ValueError(f"metrics: {name} is not a finite number")
        elif (kind == "counter" and previous_counters
                and value < previous_counters.get(name, value)):
            raise ValueError(f"metrics: counter {name} decreased")
    if (per_frame and previous_per_frame_last is not None
            and first != previous_per_frame_last + 1):
        raise ValueError("metrics: per-frame values must continue at the next frame")
    return last
```
