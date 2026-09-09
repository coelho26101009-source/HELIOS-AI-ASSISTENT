# Public Release Checklist

What is done, what is not, and what genuinely blocks a public beta.

Marks are used strictly: `[x]` only when the thing is actually finished and
verified, `[ ]` when it is not, `[~]` when it is partly there with the remainder
named. An item nobody has tested says so.

Last reviewed: **2026-09-09**, during the documentation truth pass. That pass
audited every public document against the code and corrected the provider,
conversation, memory, CI and security claims; it changed documentation only.

---

## Repository

- [x] Clear README describing what Nano is
- [x] `.gitignore` covers secrets, logs, runtime data, voice recordings, build output
- [x] No secrets, keys or personal data committed (audited; `.env` is ignored)
- [x] Single canonical product version (`version.json`)
- [x] Architecture documentation (`docs/architecture/`)
- [x] Security policy documentation (`docs/SECURITY_POLICY.md`)
- [~] Version alignment — `version.json` is canonical, but `electron/package.json`
      and `frontend/package.json` still read `8.1.0`. `electron-builder` stamps
      the installer from the Electron one, so this is a packaging-pass item.
- [ ] Repository description and topics set on GitHub (values recommended in the
      audit report; must be set by a human in repository settings)

## Community standards

- [x] `README.md`
- [x] `CONTRIBUTING.md`
- [x] `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)
- [x] `SECURITY.md`
- [x] `SUPPORT.md`
- [x] `CHANGELOG.md`
- [x] Issue templates (bug, feature) with a security escape hatch
- [x] `.github/ISSUE_TEMPLATE/config.yml` — blank issues disabled so a
      vulnerability cannot arrive as an untemplated public issue
- [x] Pull request template with security and capability impact sections
- [x] **`LICENSE`** — Apache License 2.0, canonical text, chosen by the project owner
- [x] GitHub private vulnerability reporting **enabled** in repository settings
      (`SECURITY.md` instructs people to use it, so it must actually be on)
- [x] Discussions enabled, or `SUPPORT.md` updated to stop linking to it

## License

- [x] **Choose a licence.** Apache License 2.0.
- [x] Add `LICENSE` at the repository root
- [ ] Add the licence to the About panel (README.md links it; the in-app About
      panel does not yet show it)
- [~] Confirm compatibility with the LGPL dependencies below — Apache-2.0 is
      generally understood to be compatible at the application level; a formal
      legal review has not been done. See
      [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

Nano is now licensed under **Apache License 2.0**: permissive, permits
commercial reuse, modification and redistribution, and does not generally
require downstream modified applications to stay open-source. This does not
remove the obligations from third-party dependencies — see
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

## Privacy

- [x] `PRIVACY.md` documenting real data flows per provider mode
- [x] **Documentation truth pass** — every public document audited against the
      code (2026-09-09). Corrected: Groq-only provider descriptions, AUTO/CLOUD
      mode semantics, conversations described as read-only, Memory/RAG listed as
      roadmap, the absence of Mistral, Gemini and the Second Brain from all
      user-facing docs, `edge-tts` described as local TTS, a claimed cloud-audio
      fallback that does not exist, and a stale count of eel-exposed functions.
- [x] Data flows traced in code rather than assumed
- [x] Storage locations, retention and deletion documented
- [x] **Corrected a false claim**: the UI said "no modo Local, nada sai do
      computador" while `edge-tts` sends spoken text to Microsoft in every mode
- [x] Spoken-reply disclosure surfaced in Definições → Privacidade
- [x] Screenshots auto-expire (1 hour / 10 most recent)
- [x] Voice recordings deleted immediately after transcription
- [x] API key stored OS-encrypted (DPAPI), never reaches the renderer
- [x] **Conversation deletion**: per thread, several at once, and all — each
      removing the thread's messages, summary, facts and index entries
- [x] Memory deletion: per memory, "Esquecer tudo", and per Second Brain node
- [ ] One-click "delete all my data" (individual controls exist; a single wipe
      across conversations, memories, the graph, settings and credentials does
      not)
- [ ] Privacy review by someone qualified, before any commercial deployment

## Security

- [x] Central authority chain: MODEL → REQUEST → POLICY → PERMISSION → EXECUTOR → NARROW TOOL
- [x] No arbitrary shell, PowerShell, CMD or script execution anywhere reachable
- [x] Unsupported capabilities declared machine-readably (`core/capabilities.py`)
- [x] Protected paths; deletion means the Recycle Bin
- [x] Grants bound to capability + target + scope; no permanent allow
- [x] Secrets never in logs, tool results, clipboard or audit entries
- [x] Prompt-injection trust boundary (external content is data, never instruction)
- [x] Central schema validation ahead of the policy decision, so the arguments
      the policy judged are the arguments the handler receives
- [x] Semantic Execution Ledger: per-turn deduplication of tool calls keyed on
      the effective (schema-normalised) arguments, covering both a completed
      call replayed after a provider failover and two identical calls in flight
      in one response
- [x] Window close verified against window **identity** (handle plus owning
      process id), with an unreadable owner PID falling back to `IsWindow` for
      that poll rather than being read as a mismatch
- [x] **Audit fix**: withdrew `context_switcher` (PowerShell, `shell=True`,
      `taskkill /F`, registry write, path traversal on a model-supplied name)
- [x] **Audit fix**: removed `launch_process`/`kill_process` (`shell=True`, dead code)
- [x] **Audit fix**: Origin enforcement on the local control plane
- [x] Regression tests for all three, proven non-vacuous
- [x] CI gate rejecting `shell=True`, `os.system`, `os.popen`, `eval`, `exec`
- [x] **Audit fix**: removed `core/project_agent.py` — unreferenced, unreachable
      helpers that ran `git`/`pytest` in a caller-supplied directory. Confirmed
      dead via a repository-wide reference audit before deletion.
- [ ] Independent security review before public beta
- [ ] Dependency vulnerability scanning (Dependabot or equivalent) enabled

### Electron

- [x] `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`
- [x] `webviewTag: false`, `webSecurity: true`
- [x] Narrow preload; no generic invoke channel
- [x] Navigation restricted to Nano's own origin
- [x] External links validated and handed to the OS browser
- [x] All device permission requests denied
- [x] Single-instance lock
- [x] **Audit fix**: Content Security Policy on the main window, verified
      against the real bundle with a negative control proving enforcement

### Local control plane

- [x] Binds to loopback only; never `0.0.0.0`
- [x] Ephemeral port
- [x] **Audit fix**: WebSocket upgrades rejected unless the Origin is Nano's own
      page — closes cross-site WebSocket hijacking of every exposed backend
      function, including the entire approval surface. That surface was around
      seventy functions when the fix landed and is now well over a hundred,
      which is the point: the guard has to hold as the surface grows.
- [~] **A native local process is still not authenticated.** It can send any
      Origin. Accepted for now: a process at that privilege already owns the
      user session. A per-session token would close it if the threat model
      changes.

## Continuous integration

- [x] CI on push and pull request
- [x] Python tests on Ubuntu **and** Windows
- [x] Frontend typecheck and production build
- [x] Electron shell tests
- [x] Static security gate
- [x] No secrets required; a fork's PR runs the full suite
- [x] Least privilege (`contents: read`)
- [x] Pinned action majors, official actions only
- [x] Dependency caching
- [x] Packaging workflow made manual-only with an explicit publish opt-in
- [x] **Render/behaviour harnesses in CI.** The `chromium-ui` job loads the
      production bundle into Electron's own Chromium under `xvfb` and runs the
      57 `chromium`-marked tests (`render-check`, `chat-drive`, `settings-drive`,
      `memory-render` and their UI contracts). They used to skip on CI because
      every module looked for `electron.exe`, and the skip read as a pass.
- [~] Harness coverage is not total: `csp-check`, `overlay-live` and
      `focus-trap-render` still run only in the manual release gate, and the
      Windows rendering path is not covered by any CI job.
- [ ] Branch protection requiring CI to pass before merge

## Packaging

- [ ] **Professional Windows installer** — the largest remaining piece of work
- [ ] Packaged Python runtime that does not depend on a system Python
- [ ] **Zero terminal windows** — no console flashes on launch
- [ ] Installed-build validation (everything so far was validated from a checkout)
- [ ] Uninstaller that removes the application and offers to remove user data
- [ ] Install size and startup time measured and acceptable
- [~] `build-windows.yml` exists and is a reasonable starting point, but has
      never produced a validated installer

## Code signing

- [ ] Code-signing certificate obtained (OV or EV)
- [ ] Certificate stored in Actions secrets, never in the repository
- [ ] Signing wired into the release workflow only — never a PR-triggered one
- [ ] Signature verified before publishing
- [ ] SmartScreen reputation understood and communicated to early users

## Updates

- [ ] Update mechanism (none exists; a user cannot learn a new version shipped)
- [ ] Update check is opt-in or clearly disclosed — it is a network call
- [ ] Data migration strategy across versions
- [ ] Rollback guidance for users

## Onboarding

- [ ] First-run experience (there is none — Nano starts in its full interface)
- [ ] Provider setup UX: explain AUTO/CLOUD/LOCAL, and that CLOUD needs a key
- [ ] Explain what Nano can do to the computer, and that it asks first
- [ ] Surface the privacy summary during setup, not buried in Settings
- [ ] Microphone and voice setup, including that wake-phrase is off by default
- [ ] Graceful path when no cloud provider and no Ollama is available
- [ ] Explain that the preferred cloud provider and the fallback order are two
      separate settings

## Website

- [ ] Landing page
- [ ] Download with checksums
- [ ] Screenshots and a demo
- [ ] Public privacy and security pages
- [ ] Documentation hosting

*(Cookie/analytics policy deliberately out of scope until a website exists.)*

## Public beta

- [ ] All security items above resolved
- [x] Licence chosen and applied
- [ ] Installer built, signed and validated on a clean Windows machine
- [ ] Tested on a machine that is not the developer's
- [ ] Tested without Ollama installed
- [ ] Tested without any cloud API key
- [ ] Tested with each cloud provider as the preferred one
- [ ] Known-issues list published
- [ ] Feedback channel staffed
- [ ] Beta expectations set explicitly in the release notes

---

## The honest summary

**Ready:** the security architecture, the permission model, the interface,
conversations and memory, the multi-provider routing layer with its committed
benchmark evidence, the automated test coverage including real Chromium UI tests
in CI, and the community and privacy documentation.

**Not ready, and genuinely blocking:**

1. **No installer.** There is nothing for a beta tester to download.
2. **No code signing**, so the first thing a user would see is a SmartScreen
   warning on software that asks to control their computer.
3. **No onboarding.** Nano opens into its full interface and assumes the user
   knows what AUTO, CLOUD and LOCAL mean.
4. **Never validated as an installed application** — only from a development
   checkout.

The security and privacy work is in good shape. The distribution work has not
started.
