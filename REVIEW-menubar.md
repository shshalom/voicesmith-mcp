# Menu Bar Spec Review

Reviewer: Senior Engineer
Date: 2026-03-05
Spec reviewed: `/Users/shwaits/Workspace/agent-voice-mcp/SPEC-menubar.md`
Context spec: `/Users/shwaits/Workspace/agent-voice-mcp/SPEC.md`
Codebase files consulted: `server.py`, `session_registry.py`, `shared.py`

---

## CRITICAL (3 issues)

### C1. `sessions.json` reads without flock will see partial writes

**Spec quote (Section 1, line 94):**
> "Read `~/.local/share/voicesmith-mcp/sessions.json` directly (faster than HTTP, no flock needed for reads)."

**Problem:** The server's `session_registry.py` uses `fcntl.flock(f, fcntl.LOCK_EX)` on every read-write cycle (lines 209, 274, 317, 344, 391). The write path at `_write_sessions` (lines 55-59) does:

```python
def _write_sessions(path: Path, sessions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"sessions": sessions}, f, indent=2)
```

The `open(path, "w")` truncates the file to zero bytes before writing content. If the menu bar app reads between the truncate and the completion of `json.dump`, it gets an empty string or partial JSON. `json.loads("")` raises `JSONDecodeError`. The session list will intermittently flash empty or cause an unhandled exception.

This is not theoretical. `register_session` acquires `LOCK_EX`, reads, cleans stale entries, writes, then releases the lock. The truncation window is small but real, especially when multiple sessions register concurrently at startup.

**Fix options (choose one):**
1. Use `LOCK_SH` (shared read lock) in the menu bar app. This cooperates with the server's `LOCK_EX` -- the reader blocks only while a write is in progress.
2. Switch `_write_sessions` to atomic writes (write to a temp file in the same directory, then `os.rename`). This makes all readers safe without any locking on the read side.
3. Read session data via HTTP `/status` endpoints only. This avoids the file entirely but has a bootstrapping problem -- you need to know the ports before you can call `/status`.

Option 2 is the best fix because it makes every reader safe (menu bar, future tooling, debugging scripts) without requiring them to know about locking. It also hardens the server itself.

---

### C2. `wake_enable` / `wake_disable` HTTP endpoints do NOT exist

**Spec quote (HTTP API table, lines 227-228):**
> "| `/wake_enable` | POST | -- | Already exists (if wake word installed) |"
> "| `/wake_disable` | POST | -- | Already exists (if wake word installed) |"

**Spec quote (Section 2, line 108):**
> "For wake word: also call `POST /wake_enable` or `/wake_disable` on the active session's HTTP endpoint to apply immediately"

**Problem:** These endpoints do NOT exist. The HTTP handler in `server.py` (class `_VoiceHTTPHandler`, lines 183-301) handles exactly four routes:

- `GET /status`
- `POST /listen`
- `POST /speak`
- `POST /session`

The `wake_enable` and `wake_disable` functions exist as MCP tool handlers (async functions at lines 718 and 737 of `server.py`), but they have zero HTTP bindings. The spec's claim that these "Already exist" is factually incorrect. An implementer following the spec will skip building these endpoints and discover at runtime that the wake word toggle does nothing.

**Fix:** In the HTTP API table (lines 227-228), change "Already exists" to "New" for both `/wake_enable` and `/wake_disable`. Add them to the "Files to Create/Modify" table entry for `server.py` (line 354).

---

### C3. "Active session" is never defined -- every action is ambiguous

**Spec uses the phrase "active session" or "the active session" in multiple places:**
- Line 108: "call `POST /wake_enable` or `/wake_disable` on the active session's HTTP endpoint"
- Line 120: "Calls `POST /set_voice` on the active session's HTTP endpoint"
- Line 169: "Polled from the active session's HTTP `/status` endpoint"
- Line 184: "Calls `POST /stop` on the active session"
- Line 185: "Calls `POST /speak` on the active session"

**Problem:** The spec shows two concurrent sessions in the menu mockup (lines 39-40):
```
|  * Fenrir (am_fenrir)        port 7865  |  <- current session
|  o Nova (af_nova)            port 7866  |  <- other active session
```

The filled vs open dot icons suggest one session is "current" and others are not, but the spec never defines:
- What makes a session "current" vs "other"
- How the user selects which session is "current"
- Whether clicking a session changes the active session (line 92 says clicking copies the port to clipboard, NOT selects it)
- Whether "Stop Playback" stops one session or all sessions
- Which session "Test Voice" targets
- Which session "Set Voice" targets

Without this definition, every interactive feature has undefined behavior when multiple sessions exist.

**Fix:** Add a "Session Selection" subsection that defines one of these models:
1. **Single-target model:** Clicking a session selects it as the target for all actions. Visual indicator (checkmark or filled dot) shows which is selected. Default to the most recently registered session.
2. **All-target model:** Actions like Stop, Mute, and Test apply to ALL sessions. Voice switching applies to the most recently active session. Document this explicitly per action.
3. **Action-specific routing:** Some actions target all (Stop, Mute), some target one (Set Voice, Test). Each action's description must state its routing behavior.

---

## IMPORTANT (6 issues)

### I1. 2-second HTTP polling will hang when `/listen` is blocking

**Spec quote (line 304):**
> "Session status: GET `/status` on each active session every 2 seconds"

**Problem:** The server's HTTP listener uses Python's synchronous, single-threaded `HTTPServer` (line 306 of `server.py`):

```python
server = HTTPServer(("127.0.0.1", port), _VoiceHTTPHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
```

The `/listen` handler (lines 274-289) blocks for up to 30 seconds:

```python
future = asyncio.run_coroutine_threadsafe(
    listen(timeout=15, prompt="push-to-talk", silence_threshold=1.5),
    _event_loop,
)
result = future.result(timeout=30)
```

While `/listen` is in progress, any `GET /status` poll from the menu bar app queues behind it in the single-threaded handler. With a 2-second poll interval, the menu bar's HTTP client will time out or accumulate a backlog of stalled requests. The UI will show stale data or appear frozen for up to 30 seconds.

With 3 concurrent sessions, that is 6 HTTP requests every 2 seconds -- any one of which can stall if that session is currently handling a listen call.

**Fix (three changes needed):**
1. Switch the server from `HTTPServer` to `ThreadingHTTPServer` (one-line change: `from http.server import ThreadingHTTPServer`). This allows concurrent request handling so `/status` polls are not blocked by an active `/listen`.
2. Set a 1-2 second timeout on the menu bar's HTTP poll requests so a stalled session does not freeze the entire UI.
3. Poll at 2s only while the menu is open. When the menu is closed, poll at 10-30s (just enough for icon state changes). This reduces unnecessary load.

---

### I2. `config.json` dual-writer race between menu bar and server

**Spec quote (line 107):**
> "Read `config.json`, flip the boolean, write back (atomic write via temp file + rename)"

**Problem:** The server also writes to `config.json`. Per the main SPEC.md (line 79):
> "auto-saved to config.json on graceful shutdown and periodically (every 60s)"

The race condition:
1. Menu bar reads `config.json` at time T (contains `duck_media: true`, `voice_registry: {A: x}`)
2. Server writes its periodic save at time T+1 (updates `voice_registry: {A: x, B: y}`, `duck_media` still `true`)
3. Menu bar writes its toggled version at time T+2 (`duck_media: false`, but with the OLD `voice_registry: {A: x}`)
4. The server's registry addition of `B: y` from step 2 is silently lost

Neither the server nor the spec defines any locking protocol for `config.json`. The `sessions.json` file uses `flock`, but `config.json` does not.

**Fix (choose one):**
1. Add `flock` to all `config.json` reads and writes in both the server and the menu bar app. This is the minimal fix.
2. Route all config mutations through HTTP. Add a `POST /config` endpoint with body `{"key": "tts.duck_media", "value": false}`. The server becomes the single writer for `config.json`, eliminating the race entirely. This is the cleaner approach and aligns with the existing HTTP control model.

---

### I3. `rumps` cannot animate or pulse icons

**Spec quote (Icon table, line 24):**
> "| Recording (speech capture) | mic pulsing | User is speaking, mic is recording |"

**Problem:** `rumps` is a thin Python wrapper around `NSStatusItem`. It supports setting a static icon image via `app.icon = "path/to/icon.png"`, but it has no built-in animation API. There is no `animate()`, no frame-sequence support, no Core Animation bridge.

To simulate pulsing, you would need a background timer that swaps between two static images at roughly 10fps. This approach:
- Looks janky compared to native Core Animation pulsing
- Wastes CPU with frequent image swaps
- Requires careful thread synchronization because rumps runs its own NSApplication run loop and icon updates must happen on the main thread

The "blue" and "red" color states also require pre-rendered icon variants since rumps does not draw programmatically -- it loads image files.

**Fix:**
- Remove "pulsing" from the icon table. Replace with a distinct static "recording" icon (e.g., a filled red dot overlay on the mic, or a mic with radiating lines).
- Document that 6 static icon PNG assets are needed (one per state: dim, normal, recording, blue/listening, crossed/muted, red/error).
- If pulsing animation is a hard requirement, call it out explicitly as a reason to use the SwiftUI path from the start rather than rumps.

---

### I4. Version check via `npm view` requires npm and can hang

**Spec quote (line 199):**
> "run `npm view voicesmith-mcp version` (single network call)"

**Problem:**
1. `npm view` requires npm to be installed and on PATH. Users who installed via `install.sh` (git clone path) may not have npm at all.
2. `npm view` can take 3-10 seconds even on a fast connection and writes warnings to stderr (deprecation notices, audit warnings).
3. Behind corporate proxies or VPNs, `npm view` may hang indefinitely because it goes through npm's own proxy/auth resolution.
4. Subprocess error handling is tricky -- the menu bar needs to distinguish "npm not found," "network timeout," "registry error," and "success."

**Fix:** Replace `npm view` with a direct HTTPS request to the npm registry REST API:
```
GET https://registry.npmjs.org/voicesmith-mcp/latest
```
This returns a JSON object with a `version` field. Benefits:
- Works without npm installed
- Easy to set a 5-second socket timeout
- No stderr noise
- Simpler error handling (HTTP status codes)

Also add a `"check_updates": true` key to `config.json` so users behind air-gapped networks can disable the check entirely.

---

### I5. "Update Now" may install a different version than displayed

**Spec quote (line 207):**
> "Run `npx voicesmith-mcp install --update` in a subprocess"

**Problem:** Between the cached version check (which runs every 6 hours per line 199) and the user clicking "Update Now," a newer version could be published. The UI says "Update to v1.0.19" but `npx` fetches and runs the latest available package, which might now be v1.0.20. The user sees a version mismatch between what was promised and what was installed.

Additionally, `npx` has its own internal caching behavior and may run a stale cached version of the installer binary rather than the version it claims to download.

**Fix:** Pin the version in the npx invocation:
```
npx voicesmith-mcp@1.0.19 install --update
```
This ensures the installed version matches exactly what was shown to the user in the "Update Available" menu item.

---

### I6. Mute/Unmute toggle is missing from the menu

**Problem:** The spec defines `POST /mute` and `POST /unmute` in the HTTP API table (lines 225-226). The icon state table includes a "Muted" state with a crossed mic icon (line 26). But there is NO Mute toggle anywhere in the menu structure or the Quick Toggles section (Section 2, lines 96-110).

The Quick Toggles section lists only three toggles:
- Media Ducking
- Nudge on Timeout
- Wake Word

Mute is arguably the single most important quick action for a menu bar audio application. "Silence everything right now" is the primary reason someone clicks a menu bar icon during a meeting or phone call. The infrastructure is defined (HTTP endpoints, icon state) but the user-facing control is absent.

**Fix:** Add a "Mute" toggle to Section 2. Unlike the other toggles (which write to `config.json`), Mute should call `POST /mute` or `POST /unmute` on the active session's HTTP endpoint because mute is a runtime state, not a persisted config value. The toggle should be visually prominent -- ideally at the top of the menu, not buried in the toggles section.

---

## MINOR (6 issues)

### M1. "View Rules" and "Edit Rules" are the same action

**Spec quote (lines 148-149):**
> "| **View Rules** | Opens the installed voice rules file in the default text editor (read-only intent). |"
> "| **Edit Rules** | Same as View -- opens in editor. The file is user-editable. |"

The spec explicitly acknowledges these are identical ("Same as View"). On macOS, the `open` command opens a file in the default application for that file type. There is no mechanism to force read-only mode through the default app handler. Both actions result in the same file opening in the same editor in the same mode.

Having two identical menu items creates user confusion ("what's the difference?") and wastes menu real estate.

**Fix:** Either:
- Remove "View Rules" entirely. Keep only "Edit Rules..."
- Replace "View Rules" with a Quick Look preview: `subprocess.run(["qlmanage", "-p", path])`. Quick Look shows a non-editable floating panel, which fulfills the "view" intent distinctly from "edit."

---

### M2. Session click copies port with no visual feedback or discoverability

**Spec quote (line 92):**
> "Clicking a session copies its port to clipboard (for debugging)"

**Problem:** There is no visual affordance (tooltip, cursor change, or label) indicating that clicking a session will copy its port. There is no confirmation that the copy happened. Users will click a session expecting something visible to happen (select it, show details, navigate somewhere) and have no idea their clipboard just changed.

Additionally, copying a port number to the clipboard is a developer debugging action, not something end users need. It is an odd primary click action for a session list item.

**Fix:** Either:
- Show a brief macOS notification via `rumps.notification()`: "Port 7865 copied to clipboard."
- Repurpose the click action to select the session as the active target (which also resolves C3). Move "Copy Port" to a right-click context menu or a submenu action.

---

### M3. Whisper model switch tells user to restart but provides no mechanism

**Spec quote (line 139):**
> "Shows a notification: 'Whisper model changed to small. Restart your session to apply.'"

**Problem:** The user has no way to restart a session from the menu bar app. They must return to their IDE, find the right session, and restart the MCP server manually. The spec does not tell the user how to do this. The notification creates an expectation ("restart your session") without providing the means to fulfill it.

**Fix:** Either:
- Add a "Restart Session" action to the Actions section. Implementation: send SIGTERM to the server PID (`os.kill(session["pid"], signal.SIGTERM)`). The IDE's MCP client will detect the server exit and respawn it automatically.
- Include actionable guidance in the notification text: "Whisper model changed to small. Close and reopen your IDE chat session to apply."

---

### M4. 54-voice flat submenu will be unusable in rumps

**Spec quote (line 114):**
> "Submenu listing all 54 Kokoro voices, grouped by language"

**Problem:** `rumps` submenus are flat scrolling lists. A 54-item submenu will extend well past the screen height on most displays and require scrolling. The spec says "grouped by language" but `rumps` does not support section headers, visual separators, or grouping labels within a single submenu level. All 54 items appear as an undifferentiated list.

**Fix:** Use nested submenus to create visual grouping:
```
Voice >
  American English >
    Eric (am_eric)
    Adam (am_adam)
    Nova (af_nova)
    ...
  British English >
    Daniel (bm_daniel)
    Alice (bf_alice)
    ...
  Other Languages >
    Spanish >
      ...
    French >
      ...
    ...
```
This is 2-3 nesting levels deep, but each level has a manageable number of items (3-8). `rumps` does support nested `MenuItem` submenus.

---

### M5. `/speak` and `/listen` endpoints used but not listed in HTTP API table

**Problem:** The HTTP API table (lines 220-228) lists endpoints that need to be created or extended. It omits `/speak` and `/listen`, which already exist in the server and are used by the menu bar app:

- "Test Voice" (Section 7, line 185): "Calls `POST /speak` on the active session"
- Push-to-talk uses `POST /listen` (existing functionality)

The table is meant to document all HTTP API surface area the menu bar depends on. Omitting existing endpoints that the menu bar calls creates an incomplete dependency picture.

**Fix:** Add rows to the HTTP API table:
- `| /speak | POST | {"name": "...", "text": "..."} | Existing -- used by Test Voice |`
- `| /listen | POST | -- | Existing -- used by push-to-talk (no menu bar changes needed) |`

---

### M6. "Files to Create/Modify" table is incomplete

**Spec quote (lines 348-359):** The files table lists 8 entries but is missing several:

| Missing file | Why it is needed |
|-------------|-----------------|
| `session_registry.py` | Needs atomic writes to fix C1 |
| `shared.py` | Contains voice list constants (`ALL_VOICE_IDS`, `VOICE_NAME_MAP`) that the menu bar needs to import or bundle |
| `menubar/__init__.py` | Required for Python package imports if rumps implementation |
| Icon assets source | The 6 icon PNGs need to come from somewhere -- no mention of a designer, asset generator, or SF Symbols extraction |
| `wake_listener.py` | If `/wake_enable` and `/wake_disable` HTTP routes are added, they call into this module -- it may need modification for thread-safety |

**Fix:** Audit all code paths the menu bar touches and ensure every file that needs creation or modification appears in the table.

---

## SUGGESTIONS (5 items)

### S1. Consider server-sent events (SSE) instead of polling

The entire architecture is poll-based. A lightweight SSE endpoint (`GET /events`) on the server would push state changes to the menu bar in real time:
- Voice switched
- Mute toggled
- Listen started/stopped (for icon animation)
- Session registered/unregistered
- Error occurred

SSE is straightforward to implement with `BaseHTTPRequestHandler`: keep the connection open and write `data: {...}\n\n` lines. The menu bar reads the stream in a background thread. This eliminates the 2-5 second staleness window between polls and reduces HTTP traffic to near zero during idle periods.

Not required for v1, but worth noting as the natural evolution of the polling architecture.

---

### S2. Add "Open Config..." to the menu

The menu has "Voice Rules" for editing IDE instruction files but no way to open `config.json` itself. Power users who want to tweak `vad_threshold`, `silence_threshold`, `log_level`, or other settings must remember the path `~/.local/share/voicesmith-mcp/config.json`. An "Open Config..." menu item that runs `subprocess.run(["open", config_path])` costs nothing to implement and is immediately useful.

---

### S3. Add crash recovery via `KeepAlive` in the LaunchAgent plist

**Spec quote (lines 310-313):**
> "Starts: Automatically via LaunchAgent"
> "Runs: As a standalone process, independent of MCP server sessions"

The spec mentions a LaunchAgent for auto-start on boot but does not mention crash recovery. If the menu bar app crashes at runtime (Python exception, segfault, OOM), nothing restarts it until the next login.

**Fix:** Add `<key>KeepAlive</key><true/>` to the `com.voicesmith-mcp.menubar.plist` LaunchAgent. This causes launchd to restart the process automatically after a crash. Include `<key>ThrottleInterval</key><integer>10</integer>` to prevent rapid restart loops if the crash is persistent.

---

### S4. Add "Mute All" for multi-session scenarios

With multiple active sessions, `POST /mute` only mutes one session. If the user is in a meeting and needs silence from all AI agents, they must mute each session individually. A "Mute All" action that iterates over all active sessions and calls `POST /mute` on each would be more practical.

This also partially resolves the ambiguity in C3: "Mute All" always does the right thing regardless of which session is considered "active."

Consider making the top-level Mute toggle always target all sessions, with per-session mute available via a session's context menu or submenu.

---

### S5. Handle missing fields in extended `/status` gracefully

**Problem:** The extended `/status` response (lines 234-268) adds many new fields that do not exist in the current `/status` handler. Current handler (server.py lines 188-200) returns only:

```json
{
  "ready": true,
  "name": "...",
  "port": 7865,
  "session_id": "...",
  "mcp_connected": true,
  "uptime_s": 4980,
  "last_tool_call_age_s": 12
}
```

The extended response adds: `muted`, `tts.loaded`, `tts.model`, `tts.voices`, `tts.duck_media`, `stt.loaded`, `stt.model`, `stt.language`, `stt.nudge_on_timeout`, `vad.loaded`, `wake_word.enabled`, `wake_word.listening`, `wake_word.state`, `wake_word.model`, `queue_depth`, `registry_size`.

If the menu bar app polls a server that has not been updated to the extended schema (e.g., during a partial upgrade), it will receive the old minimal response. Accessing missing nested keys like `response["tts"]["duck_media"]` will raise `KeyError` or `TypeError`.

**Fix:** The spec should require the menu bar app to treat ALL fields as optional. Use `.get()` with sensible defaults throughout. Display "unknown" or a neutral state for any missing field. Add a note to the spec: "The menu bar MUST gracefully handle servers running older versions that return a subset of these fields."

---

## SUMMARY

| Severity | Count | Key themes |
|----------|-------|------------|
| Critical | 3 | File read safety without locking (C1), endpoints falsely claimed as existing (C2), undefined active session concept (C3) |
| Important | 6 | Single-threaded HTTP blocking on listen (I1), config.json dual-writer race (I2), rumps animation limits (I3), npm dependency for version checks (I4), version mismatch on update (I5), missing mute toggle (I6) |
| Minor | 6 | Duplicate menu items (M1), no click feedback (M2), no restart mechanism (M3), flat 54-item submenu (M4), missing API table entries (M5), incomplete files table (M6) |
| Suggestion | 5 | SSE instead of polling (S1), Open Config menu item (S2), LaunchAgent KeepAlive (S3), Mute All (S4), graceful field handling (S5) |

### Recommended resolution order

1. **C3** -- Define "active session" semantics. Every interactive feature depends on this. Without it, the implementer must make arbitrary decisions that may conflict with the designer's intent.
2. **C1** -- Fix `_write_sessions` to use atomic writes. This benefits the whole system (not just the menu bar) and is a one-function change in `session_registry.py`.
3. **C2** -- Correct the HTTP API table. Change `/wake_enable` and `/wake_disable` from "Already exists" to "New."
4. **I1** -- Switch to `ThreadingHTTPServer`. One-line change that prevents the menu bar from freezing during listen calls.
5. **I6** -- Add the Mute toggle to the menu. This is the most obvious missing feature.
6. **I2** -- Decide on config.json write strategy (flock vs HTTP-only mutations) before implementation starts.
