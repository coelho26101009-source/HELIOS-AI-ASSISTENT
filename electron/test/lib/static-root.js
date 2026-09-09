/**
 * Map one HTTP request URL onto a file inside a static root -- or refuse it.
 *
 * WHY THIS EXISTS
 * ---------------
 * Six harnesses in this directory serve the built frontend over a throwaway
 * http server so the real bundle can be driven in Electron's own Chromium.
 * Each one grew its own copy of "decode the URL, join it onto frontend/out,
 * read the file", and the copies drifted: three had no containment check at
 * all, one checked containment AFTER it had already called fs.statSync on the
 * attacker-controlled path, and two checked with a bare prefix match that a
 * sibling directory (frontend/out-old) satisfies. CodeQL flagged fourteen
 * js/path-injection paths across four of them.
 *
 * These are test harnesses bound to 127.0.0.1, so the practical blast radius
 * was small -- but "it is only a test" is how a traversal helper gets copied
 * into something that is not a test, and a static server that serves whatever
 * the URL names is worth exactly one shared, correct implementation.
 *
 * WHAT "CONTAINED" MEANS HERE
 * ---------------------------
 * The resolved path must be the root itself or sit strictly beneath it. Three
 * independent barriers enforce that, and NONE of them touch the filesystem --
 * the caller only ever gets a path that has already been proven contained, so
 * no stat/read/stream ever runs against an unvalidated string:
 *
 *   1. any `..` anywhere in the decoded path is refused outright,
 *   2. the path is resolved and must fall under the root, and
 *   3. the prefix test is separator-aware, so `<root>-old` is not `<root>`.
 *
 * Percent-decoding happens exactly ONCE. Decoding twice would resolve `%252e`
 * into `.` and reintroduce the traversal this is meant to stop; decoding zero
 * times would let `%2e%2e/` through. Malformed encoding (`%ZZ`, a trailing
 * `%`) makes decodeURIComponent throw, which is a refusal, not a crash -- the
 * old call sites let that exception escape the request handler.
 *
 * Backslashes are folded to forward slashes before any of this, so `..\secret`
 * is refused identically on Windows and on Linux. Without that fold, POSIX
 * would treat `..\secret` as one ordinary filename and the two platforms would
 * disagree about what the same request means.
 */
'use strict';

const path = require('path');

/** The file served when the request names the root itself. */
const INDEX = 'index.html';

/**
 * Resolve `requestUrl` against `root`.
 *
 * @param {string} root        Directory the server is allowed to serve from.
 * @param {string} requestUrl  Raw `req.url`, query string and fragment included.
 * @returns {string|null}      An absolute path inside `root`, or null if the
 *                             request must be refused. A non-null return says
 *                             nothing about whether the file EXISTS -- that is
 *                             still the caller's question to ask.
 */
function resolveStaticPath(root, requestUrl) {
  const base = path.resolve(root);

  // req.url is technically optional on an http.IncomingMessage, and one of the
  // old call sites would have thrown on a request that omitted it.
  const raw = typeof requestUrl === 'string' && requestUrl !== '' ? requestUrl : '/';

  // A query string or fragment names a resource variant, never a file on disk.
  const pathname = raw.split('?')[0].split('#')[0];

  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch (err) {
    return null;                       // malformed percent-encoding
  }

  // A NUL truncates the path inside libuv, so `a.html\0.png` would read a.html
  // while every extension check above it saw a .png.
  if (decoded.indexOf('\0') !== -1) return null;

  // Windows accepts both separators; treating them the same everywhere keeps
  // this decision platform-independent.
  const unified = decoded.replace(/\\/g, '/');

  // Barrier 1. No traversal segment survives, in any position. The built
  // frontend contains no filename with two consecutive dots, so refusing the
  // substring outright costs nothing and cannot be reasoned around.
  if (unified.includes('..')) return null;

  // Strip leading separators so the remainder is unambiguously relative:
  // path.resolve honours an absolute second argument and would otherwise
  // abandon the root entirely for a request like `//etc/passwd`.
  const relative = unified.replace(/^\/+/, '');

  const full = path.resolve(base, relative === '' ? INDEX : relative);

  // Barrier 2. The resolved path must live under the root.
  if (!full.startsWith(base)) return null;

  // Barrier 3. ...and `startsWith` alone would also accept a SIBLING whose
  // name merely begins with the root's, so require the separator that makes it
  // a real descendant. `<root>/x` passes; `<root>-old/x` does not.
  if (full !== base && !full.startsWith(base + path.sep)) return null;

  // A request can name the root by more spellings than the empty one: `/.` and
  // `/././` collapse onto it too, and they mean what `/` means. Returning the
  // bare directory instead would hand the caller a path to stat that every
  // other spelling of the same request never produces.
  if (full === base) return path.join(base, INDEX);

  return full;
}

module.exports = { resolveStaticPath };
