/**
 * A deadline for a Chromium-driven harness, and a report when it is missed.
 *
 * WHY
 * ---
 * These harnesses had no internal deadline of any kind. When one wedged, it
 * simply sat there: a measured run stayed alive for 467 seconds having spent
 * 0.9 seconds of CPU, produced no stdout at all, and was still going when it
 * was killed by hand. On the Python side that is indistinguishable from slow
 * work, so `subprocess.run(timeout=600)` waits out the entire ten minutes and
 * then raises TimeoutExpired with an empty stdout -- which the wrapper reports
 * as "produced no report", naming neither the harness's last known phase nor
 * the fact that it hung rather than crashed.
 *
 * A harness that cannot finish must still SAY SO, in the same JSON shape it
 * would have produced anyway, so the caller gets a named failing step instead
 * of an empty pipe. That is all this is.
 *
 * The deadline is a backstop, not a tuning knob: it is set well above the
 * measured duration of a healthy run so that crossing it means something is
 * wrong, never that the machine was busy.
 */
'use strict';

const { app } = require('electron');

/**
 * Arm a deadline. Returns a `{ phase, disarm }` pair:
 *
 *   phase('loading the bundle')   record what is being awaited right now
 *   disarm()                      call once the report has been written
 */
function watchdog({ label, ms }) {
  let current = 'starting';
  const started = Date.now();

  /* NANO_WATCHDOG_MS overrides the deadline. Two uses, both real: a CI runner
     with software rendering may legitimately need longer than a workstation,
     and a deadline nobody can trigger on demand is a guard nobody has ever
     seen work. Setting it to a second is how this file was proved to fire and
     to report in the right shape. */
  const override = Number(process.env.NANO_WATCHDOG_MS);
  if (Number.isFinite(override) && override > 0) ms = override;

  const timer = setTimeout(() => {
    const elapsed = ((Date.now() - started) / 1000).toFixed(1);
    const detail = `${label} was still in "${current}" after ${elapsed}s `
      + `(deadline ${(ms / 1000).toFixed(0)}s)`;
    console.error('WATCHDOG: ' + detail);
    // The SAME shape a completed run emits, so the caller parses it normally
    // and reports a named failure rather than "no report".
    process.stdout.write(JSON.stringify({
      ok: false,
      timedOut: true,
      timeoutMs: ms,
      lastPhase: current,
      elapsedMs: Date.now() - started,
      steps: [{
        label: `${label} finished within its time budget`,
        pass: false,
        detail,
      }],
      views: [],
      calledFunctions: [],
    }, null, 2));
    // app.exit, not process.exit: it takes the renderer, GPU and utility
    // children down with it instead of orphaning them.
    app.exit(1);
  }, ms);

  return {
    phase: (name) => { current = name; },
    disarm: () => clearTimeout(timer),
  };
}

module.exports = { watchdog };
