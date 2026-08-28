# Project review findings

Reviewed: 2026-08-28

This document records issues found during a review of the current project,
along with proposed fixes and regression-test coverage. The findings are
ordered by likely user impact.

## 1. Long-range history omits recent samples

**Severity:** High  
**Location:** `server/hub/db.py`, `Store.history_with_bands()`

### Problem

History requests choose one storage source based only on the start time:

```python
if from_ts < full_res_cutoff:
    return self._history_rollups(device, metrics, from_ts, to_ts)
return self._history_samples(device, metrics, from_ts, to_ts), {}
```

When a request begins before the full-resolution cutoff but ends after it,
only rollups are returned. Rollups are intentionally created only for buckets
older than the cutoff. Consequently, ranges such as `7d` and `30d` can omit
the newest full-resolution period (24 hours with the default configuration).
The resulting chart can appear to stop a day before the present.

### Proposed fix

Split requests that cross the cutoff into two queries:

1. Read rollups from `from_ts` through the last completed rollup bucket at or
   before the cutoff.
2. Read raw samples from the cutoff through `to_ts`.
3. Merge each metric's points in timestamp order without duplicating the
   boundary.
4. Preserve rollup min/max bands for rolled-up points. Either omit bands for
   raw points or represent absent raw bands explicitly in the API contract.
5. Apply the point budget to the combined result, not independently to both
   halves, so a series still returns at most `MAX_POINTS` points.

It may be helpful to return aligned band data only for the rollup portion. The
chart already treats bands as optional, but its polygon logic should be
verified with a band shorter than the corresponding series.

### Regression tests

- Insert old samples, run rollup/prune, then insert recent raw samples.
- Query a range spanning both sides of the cutoff.
- Assert that both the rolled-up and recent timestamps are returned in order.
- Assert the newest timestamp is recent rather than the last rollup bucket.
- Assert the combined result remains within `MAX_POINTS`.

## 2. Configured device-port defaults are not applied

**Severity:** Medium  
**Locations:** `server/hub/config.py`, `hub.example.toml`

### Problem

`Device` declares default ports, but `load()` always supplies values obtained
with `dict.get()`:

```python
omlx_port=d.get("omlx_port"),
macmon_port=d.get("macmon_port"),
```

For an omitted key, this passes `None` and overrides the dataclass default.
Omitting either port therefore disables that source. This contradicts the
example configuration, which says omitted ports use defaults. There is also a
second inconsistency: `Device.omlx_port` defaults to 8000 while
`hub.example.toml` documents 8080.

### Proposed fix

First choose and document a single omlx default (likely 8000 or 8080 based on
the supported omlx deployment). Then apply defaults explicitly during config
loading, for example:

```python
omlx_port=d.get("omlx_port", DEFAULT_OMLX_PORT),
macmon_port=d.get("macmon_port", DEFAULT_MACMON_PORT),
```

Define the constants once and use them both in `Device` and the loader to
avoid future drift. Retain an explicit disabling mechanism, such as port `0`,
and document it. TOML has no `null`, so saying that omission disables a source
is not a practical alternative unless separate enable flags are added.

### Regression tests

- Load a device with both port keys omitted and assert both documented
  defaults are used.
- Load a device with explicit custom ports and assert they are preserved.
- Load a device with port `0` and assert the corresponding source is disabled.

## 3. Explicit history bounds are insufficiently validated

**Severity:** Medium  
**Locations:** `server/hub/api.py`, `server/hub/db.py`

### Problem

The history endpoint accepts explicit `from_ts` and `to_ts` values without
checking that they are finite, correctly ordered, or reasonably bounded.
Potential outcomes include:

- `from_ts > to_ts` silently returning an empty result;
- `NaN` or infinity reaching SQLite or causing JSON serialization failures;
- a very broad query loading a large number of rows and decoding every JSON
  payload before downsampling to `MAX_POINTS`.

Although the service is expected to run within a tailnet, malformed input can
still cause avoidable errors or excessive work.

### Proposed fix

At the API boundary:

1. Require `from_ts` and `to_ts` together. Do not silently replace both when
   only one explicit bound was supplied.
2. Reject non-finite bounds using `math.isfinite()`.
3. Reject `from_ts >= to_ts` with HTTP 400.
4. Clamp or reject bounds outside the configured retention window.
5. Consider imposing a documented maximum explicit span.

For stronger protection, avoid reading every matching raw row before
decimation. Options include selecting timestamp buckets in SQL, querying in
bounded chunks, or estimating the row count and sampling at the database
layer. Correctness is more important than perfectly even sampling, but first
and last points should be retained.

### Regression tests

- Verify reversed, equal, non-finite, and half-specified bounds return 400.
- Verify a valid explicit range still works.
- Verify requests beyond retention are consistently clamped or rejected,
  according to the chosen API behavior.

## 4. Background tasks are not awaited during shutdown

**Severity:** Low  
**Location:** `server/hub/api.py`, application lifespan cleanup

### Problem

The lifespan handler cancels poll, commit, and rollup tasks and immediately
closes the SQLite connection:

```python
for task in tasks:
    task.cancel()
store.close()
```

Cancellation is cooperative. Without awaiting the tasks, their cleanup is not
guaranteed to finish before the store closes. This can produce pending-task
warnings or cleanup races, particularly while a poller is exiting its
`httpx.AsyncClient` context.

### Proposed fix

Cancel all tasks, then await them before closing the store:

```python
for task in tasks:
    task.cancel()
await asyncio.gather(*tasks, return_exceptions=True)
store.close()
```

Put this cleanup in a `finally` block around the lifespan `yield` so it also
runs when application shutdown follows an exception.

### Regression tests

- Start the application with a controlled background task, exit the
  `TestClient` context, and assert the task reaches its cancellation cleanup.
- Run asyncio tests with warnings treated as errors to catch leaked tasks.

## 5. macmon setup documentation is out of sync with the current tree

**Severity:** Low  
**Locations:** `README.md`, `macos/install-macmon-agent.sh`

### Problem

The README refers to three machine-specific plist files that are absent from
the current working tree. It also describes copying a plist and using the
legacy `launchctl load` flow, while the new installer generates the plist and
uses `launchctl bootout`/`bootstrap`.

Following the README as written can therefore lead to missing-file errors or
an installation different from the maintained script.

### Proposed fix

- Make `macos/install-macmon-agent.sh` the primary documented installation
  path.
- Remove references to deleted, machine-specific plist files unless they will
  be restored.
- Explain that the installer discovers the Tailscale address at startup and
  waits for Tailscale after reboot.
- Document the Apple Silicon `/opt/homebrew/bin/macmon` assumption and either
  add Intel-path detection to the installer or clearly state how to edit it.
- Keep a short manual plist section only as troubleshooting or advanced usage.

### Verification

Test the documented process on a clean macOS user account or VM where no old
LaunchAgent is loaded, then verify:

```sh
launchctl print "gui/$(id -u)/com.macmon"
curl "http://$(tailscale ip -4 | head -1):9090/json"
```

## Review verification status

- Python source compilation completed successfully with `compileall`.
- `git diff --check` reported no whitespace errors.
- The Python test suite was not run during the review because the documented
  `.venv` was absent and `pytest` was not installed in the system Python.
- Existing uncommitted project changes were treated as user work and were not
  modified as part of this review.
