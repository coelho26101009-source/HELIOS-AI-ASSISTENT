/**
 * Readiness predicates for the Chromium-driven harnesses.
 *
 * THIS FILE IS NOT A NODE MODULE. It is read as text and prepended to the
 * source that `webContents.executeJavaScript` evaluates, so it runs INSIDE the
 * page. Never `require()` it.
 *
 * WHY IT EXISTS
 * -------------
 * The harnesses used to spell every wait as `await sleep(350)`. A fixed sleep
 * is a bet that the work finishes inside a number somebody guessed once, and
 * this repository has now lost that bet in four distinct ways, all of them
 * measured rather than theorised:
 *
 *   * a conversation switch that had not landed after 600ms, so the transcript
 *     still held the OLD thread's bubbles when the count was sampled -- the
 *     assertion then read "bubbles 3 -> 1" and blamed the product for a drop
 *     that was really the switch finally completing;
 *   * a bulk delete whose rail still showed four rows after 700ms;
 *   * a rail drawer that had not opened after 500ms at the 940x620 viewport,
 *     so the toolbar measured as "not rendered" and reported a clipping
 *     failure against a page that was fine;
 *   * a CSS `transition: background` read on the very tick the transition
 *     started, which reports the FROM value -- transparent -- and so claims a
 *     focused control is invisible while it is mid-fade to visible.
 *
 * The cure is not a bigger number. It is to name the condition being waited
 * for. Every helper here is bounded: it gives up after a deadline and returns
 * a falsy result rather than hanging, and the caller then asserts on the state
 * it actually observed. Waiting for the condition does NOT weaken the
 * assertion -- a product that never reaches the state still fails, it just
 * fails after the deadline instead of before the work.
 */
window.__ready = window.__ready || (() => {
  'use strict';

  const DEFAULT_TIMEOUT = 5000;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* requestAnimationFrame DOES NOT FIRE in an occluded or backgrounded window,
     and these harnesses run windows that the compositor may never touch.
     Waiting on rAF alone hung a whole run. Race every frame against a timer so
     the loop always makes progress and degrades to a timed poll when there is
     no compositor. */
  const nextFrame = () => new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    requestAnimationFrame(finish);
    setTimeout(finish, 25);
  });

  /**
   * Wait until `read()` returns something truthy, and return it.
   * Returns null if the deadline passes, so the caller can assert on the miss.
   */
  const waitFor = async (read, timeout = DEFAULT_TIMEOUT) => {
    const deadline = Date.now() + timeout;
    for (;;) {
      let value;
      try { value = read(); } catch (err) { value = null; }
      if (value) return value;
      if (Date.now() >= deadline) return null;
      await nextFrame();
    }
  };

  /** Wait until `read()` returns falsy. True if it did, false on deadline. */
  const waitGone = async (read, timeout = DEFAULT_TIMEOUT) => {
    const deadline = Date.now() + timeout;
    for (;;) {
      let value;
      try { value = read(); } catch (err) { value = null; }
      if (!value) return true;
      if (Date.now() >= deadline) return false;
      await nextFrame();
    }
  };

  /**
   * Wait until `read()` returns the SAME value on `frames` consecutive frames.
   *
   * This is the honest way to sample geometry and computed style. Unlike
   * waitFor it does not know what the right answer is, so it cannot bias the
   * measurement towards passing -- it only waits for the page to stop moving.
   * Values are compared by JSON, so objects and arrays work.
   *
   * REQUIRES A GENUINELY COMPOSITED PAGE. "Stable across frames" and "frozen
   * because there are no real frames" produce the identical signal from
   * inside the page: `nextFrame` degrades to its setTimeout branch, `read()`
   * returns the same unchanged value every poll, and this resolves `stable:
   * true` having proven nothing. A BrowserWindow created with show:false and
   * never shown has no surface for the compositor to draw into and gets no
   * real rendering opportunities -- measured directly (temporary diagnostics,
   * since removed) at ONE requestAnimationFrame tick in a 500ms window,
   * versus 141 once the window was shown with showInactive(). A CSS
   * Transition is scheduled to start on exactly such an opportunity, so on a
   * window like that the transitioned property reads at its BEFORE value
   * forever, and this function calls that "settled". It happened:
   * memory-render.js's dimOpacity read a frozen 1 through this exact path on
   * Linux CI while appearing to behave locally, because a SEPARATE,
   * unrelated setting (prefers-reduced-motion active on the dev machine)
   * meant there was no transition to freeze in the first place there. Call
   * `win.showInactive()` (see chat-drive.js and memory-render.js) before
   * driving any harness that calls this, waitFor, waitGone, settleLayout or
   * settledStyle -- a page that is never shown cannot be waited into
   * correctness.
   */
  const waitStable = async (read, { frames = 3, timeout = DEFAULT_TIMEOUT } = {}) => {
    const deadline = Date.now() + timeout;
    let last = Symbol('none');
    let agreed = 0;
    let value = null;
    for (;;) {
      await nextFrame();
      try { value = read(); } catch (err) { value = null; }
      const now = JSON.stringify(value === undefined ? null : value);
      if (now === last) {
        agreed += 1;
        if (agreed >= frames) return { value, stable: true };
      } else {
        agreed = 1;
        last = now;
      }
      if (Date.now() >= deadline) return { value, stable: false };
    }
  };

  /**
   * Settle the layout: two consecutive frames that agree about the viewport
   * AND about the app's own width. `win.setSize` returns immediately and the
   * renderer relayouts on its own schedule, so a fixed sleep can sample a
   * frame in which documentElement.clientWidth ALREADY reports the new,
   * narrower width while the elements still carry their old, wider boxes.
   * Every one of them then measures as past the right edge and a clean layout
   * reports six offenders -- two readings from the same instant that disagree,
   * which is the signature of a half-applied relayout, not of a clipped page.
   */
  const settleLayout = async (timeout = DEFAULT_TIMEOUT) => {
    const doc = document.documentElement;
    const { stable } = await waitStable(() => {
      const app = document.querySelector('.app');
      return doc.clientWidth + 'x' + doc.clientHeight + ':'
        + (app ? Math.round(app.getBoundingClientRect().width) : -1);
    }, { frames: 2, timeout });
    return stable;
  };

  /**
   * Wait for a CSS transition on `prop` to finish, then return the settled
   * computed value.
   *
   * Reading getComputedStyle on the tick that a transition starts returns the
   * value being transitioned AWAY FROM. `.msg__meta-toggle:focus` fades a
   * background in, so a same-tick read reports `rgba(0, 0, 0, 0)` and the
   * assertion "the focused control is visually distinguished" fails against a
   * stylesheet that is doing exactly what it should.
   */
  const settledStyle = async (el, prop, timeout = DEFAULT_TIMEOUT) => {
    const { value } = await waitStable(
      () => getComputedStyle(el)[prop], { frames: 3, timeout });
    return value;
  };

  return { sleep, nextFrame, waitFor, waitGone, waitStable, settleLayout, settledStyle };
})();
