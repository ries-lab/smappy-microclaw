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
**numbers** while it runs — "localizations per frame over the last 100 frames",
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
- **D3 — The control law lives in MicroClaw, as a small built-in rule with
  parameters, not in package code and not as a free-form hook.**  The user
  says "keep 5 localizations per frame with the 405, at most 30 %", not a
  program.  *Reason:* CLAUDE.md "Microclaw is easy to use" and "the user owns
  the session"; the rule is reviewable as data, the package stays an observer.
  A hook-based custom law can come later by giving hooks read access to the
  same metrics (§5.7) — it does not replace this.
- **D4 — Every write goes through the existing illumination guard.**  The
  envelope is the same four keys as `illumination_envelope`
  (`device, property, max_power_percent, max_writes`), validated by the same
  code, checked per write by `guard.check_illumination` with the rig's step
  ratchet.  No new safety path.
- **D5 — The controller runs on its own thread, not on the image callback,
  at a bounded rate (default one decision per second).**  *Reason:* SMLM runs
  are hardware-sequenced bursts, where no per-frame software runs; activation
  density changes over seconds to minutes, so 1 Hz is ample.  Whether a
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
  - `kind`: `"gauge"` (a value now) or `"counter"` (non-decreasing during a
    job) — required.
- `metrics` on `self_check`, or in a `1.0`/`1.1` release, is refused.
- `opens_window` stays allowed under `1.2`.

Example (SMAPpy's `fit_ndtiff` / `fit_and_view`):

```json
"metrics": {
  "localizations_per_frame": {"description": "Localizations per frame over the last window of frames fitted", "unit": "", "kind": "gauge"},
  "localizations": {"description": "Localizations so far", "unit": "", "kind": "counter"},
  "frames_fitted": {"description": "Frames fitted so far", "unit": "frames", "kind": "counter"},
  "frames_behind": {"description": "Frames written but not yet fitted", "unit": "frames", "kind": "gauge"},
  "median_photons": {"description": "Median photons per localization over the window", "unit": "photons", "kind": "gauge"}
}
```

### 5.2 Wire: the `metrics` message (worker → MicroClaw)

```json
{"protocol":"microclaw.analysis.v1","type":"metrics","job_id":"<32 hex>",
 "frames":[first,last],"values":{"localizations_per_frame":4.8,"frames_behind":0}}
```

- Closed key set: `protocol, type, job_id, frames, values`.
- `frames`: two non-negative integers, `first <= last`: the **dataset frame
  ordinals** (0-based position in the order the dataset's images were
  written, i.e. NDTiff index order) the values describe.  Counters describe
  `[0, last]`.  This is how a controller knows how old the evidence is in
  acquisition terms, not only in wall-clock terms.
- `values`: non-empty object; every key a metric declared for this operation;
  every value a finite JSON number (`bool` is not a number).  A message need
  not carry every metric.
- `last` never decreases within a job; a message whose `last` is lower than the
  previous one's is a protocol failure.  A `counter` never decreases.
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
  `name -> {value, frames, received_at}`.  This is the only state the
  controller reads.
- It keeps a bounded history for the record: one sample per second at most,
  1024 samples (ring), plus `metrics_received` and `metrics_coalesced`
  counts.
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
    "envelope": {"device": "405", "property": "Power %", "max_power_percent": 30, "max_writes": 200},
    "target": 5.0,
    "deadband": 0.2,
    "step_factor": 1.25,
    "start_percent": null,
    "interval_s": 1.0
  },
  "stop": {"metric": "localizations", "at_least": 2000000}
}
```

- `metric` must be a `gauge` the operation declares.  `illumination` and
  `stop` are each optional, at least one present.  `stop.metric` may name any
  declared metric; exactly one of `at_least` / `at_most` is given.
  (Exhaustion — "below X while at full power for N s" — is a later extension;
  keep the shape open for it.)
- Validation, before any hardware moves, in `execute_tool` next to the
  `analysis` preparation: the envelope through the same code path as
  `illumination_envelope` (`_configure_hook_capabilities`'s checks:
  `guard.is_illumination_power`, finite ceiling ≤ the configured
  `illumination.max_power_percent`, positive integer `max_writes`); the
  metric names against the release's manifest; `target > 0`,
  `0 <= deadband < 1`, `step_factor > 1`, `interval_s >= 0.2`.
  Refused together with an `illumination_envelope` for a hook on the same
  device (two writers on one property).
- Add `analysis_feedback` to `HOOK_CAPABILITY_ARGS`, or wherever the
  parameterized refusal matrix will generate its cases (CLAUDE.md: "add a new
  capability there first").
- **Confirmation**: one, at plan time, folded into the analysis confirmation
  or right after it: device/property, ceiling, write budget, start value,
  target and rule, stop rule.  A decline is "nothing was acquired", as for a
  declined analysis.

**Controller** — a thread started after the dataset exists and the analysis
job was submitted, stopped after the acquisition's `with` block exits:

```
read initial power once (the parent's baseline, as hooks do)
if start_percent is not None: write it (counts as a write; guard-checked)
every interval_s until the acquisition ends:
    m = handle.latest[metric]
    stale = (not handle.feed_alive) or age(m) > max_age_s
            or latest["frames_behind"] > max_frames_behind (if declared)
    if stop configured and not stale and stop condition holds on its metric:
        request a clean stop (below); record; stop deciding
    if illumination configured:
        if stale: hold (record "held: stale") and continue
        v = m.value
        if v < target * (1 - deadband): new = min(ceiling, max(power, floor) * step_factor)
        elif v > target * (1 + deadband): new = power / step_factor
        else: continue
        write new through the illumination guard (envelope ceiling, budget for
        increases, guard.check_illumination with the rig's step ratchet);
        on refusal: record the refusal and hold
```

`floor` is a small positive start for a laser at 0 % (e.g. 0.5 % or the rig's
minimum) — a multiplicative rule cannot leave zero.  The rule is deliberately
simple; its constants are arguments, its source is MicroClaw's, and it is
recorded.

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
- Controller: one decision per `interval_s` (default 1 s); a write is the
  setter's 41–97 µs plus the bridge round trip (`design/78`).
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
   counter, extra key, message after `result`, `metrics` in a `1.1` job — each
   is a protocol failure.  `1.0`/`1.1` behaviour unchanged.
3. Supervisor: `latest` updated per line; history ≤1/s and ≤1024; coalesced
   count; record persisted with `metrics`; a worker exit sets `feed_alive`
   false.
4. Feedback argument: every refusal in §5.5 raised before hardware moves
   (counted: no `Acquisition` constructed); declined confirmation acquires
   nothing.
5. Controller against a fake handle and a fake core, driven by a fake clock:
   below target raises ×`step_factor` to the ceiling and stops there; above
   lowers; inside the deadband does nothing; budget exhausted refuses
   increases but allows decreases; stale metric, dead feed or
   `frames_behind` over the limit holds; the stop rule fires once, only on
   fresh data; nothing is written after the acquisition ends.
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

- Manifest `protocol_version: "1.2"`, the five metrics of §5.1 on `fit_ndtiff`
  and `fit_and_view`.
- After each fitted block (SMAPpy fits each read at once since 2026-10-13,
  SMAPpy `NOTES.md`), the worker updates a window of the last
  `metrics_window_frames` frames (parameter, default 100) and sends one
  `metrics` message, at most 10 per second, with `frames = [first, last]` of
  that window.
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

def validate_metrics_message(message, *, job_id, declared, previous_last=None,
                             previous_counters=None):
    """Raise ValueError if `message` is not a valid metrics line for this job.

    declared: the operation's manifest `metrics` mapping.  Returns `last`.
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
    if previous_last is not None and frames[1] < previous_last:
        raise ValueError("metrics: last frame went backwards")
    values = message["values"]
    if not isinstance(values, dict) or not values:
        raise ValueError("metrics: values must be a non-empty object")
    for name, value in values.items():
        if name not in declared:
            raise ValueError(f"metrics: {name} is not declared")
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"metrics: {name} is not a finite number")
        if (declared[name]["kind"] == "counter" and previous_counters
                and value < previous_counters.get(name, value)):
            raise ValueError(f"metrics: counter {name} decreased")
    return frames[1]
```
