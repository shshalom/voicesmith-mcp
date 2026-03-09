# Wake Word v3 — MCP Message Queue Architecture

## Context

v1 (server thread + tmux) and v2 (menu bar + tmux/socket) both relied on external text injection into the terminal — tmux send-keys, AppleScript keystrokes, etc. These approaches are fragile, conflict with terminal behavior, and don't work with GUI IDEs.

v3 takes a fundamentally different approach: **the wake word message flows through MCP itself**. No injection. Claude receives the transcribed text as a normal tool result from a `listen` call. This works with every IDE that supports MCP.

## Core Idea

When Claude is idle (finished a task, waiting for user input), it calls `listen(mode="wake")`. This tool doesn't open the mic — it polls a message queue on the MCP server. Meanwhile, the VoiceSmith menu bar app owns the mic, detects the wake word, transcribes speech, and posts the message to the target session's HTTP endpoint. The `listen(mode="wake")` call returns the message as its tool result. Claude processes it like any user response.

---

## Architecture

```
VoiceSmith Menu Bar (always running, owns mic)
  │
  ├── Wake detector running
  ├── User says: "Hey Jarvis, Eric add error handling"
  ├── Transcribes → "Eric add error handling"
  ├── Parses session name → "Eric"
  ├── POST http://127.0.0.1:7866/wake_message
  │   body: {"text": "add error handling", "from": "voice"}
  │
  ▼
Eric's MCP Server (port 7866)
  ├── Receives message → stores in _wake_queue
  │
  ▼
Eric's Claude (idle)
  ├── Called: listen(mode="wake", timeout=300)
  ├── listen() polls _wake_queue every 1s
  ├── Message found! Returns: {"text": "add error handling", "source": "wake"}
  │
  ▼
Eric's Claude
  ├── Processes "add error handling" as the next task
  ├── Works on it...
  ├── Finishes → calls listen(mode="wake") again
  └── Loop continues
```

---

## Components

### 1. `listen(mode="wake")` — New mode for the existing listen tool

**Parameters (extended):**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `mode` | string | No | `"mic"` (default, existing behavior) or `"wake"` (poll message queue) |
| `timeout` | number | No | Max seconds to wait. Default: 15 for mic mode, 300 for wake mode |

**Behavior in wake mode:**
1. Does NOT open the mic
2. Polls `_wake_queue` on the server every 1 second
3. If a message arrives, returns it immediately
4. If timeout reached, returns `{"success": false, "error": "timeout"}`
5. Respects cancellation (`stop` tool, MCP cancel notification)

**Returns (wake message received):**
```json
{
  "success": true,
  "text": "add error handling",
  "source": "wake",
  "confidence": 0.92
}
```

**Returns (timeout):**
```json
{
  "success": false,
  "error": "timeout",
  "message": "No wake message received within timeout"
}
```

### 2. `POST /wake_message` — New HTTP endpoint on each MCP server

**Request:**
```json
{
  "text": "add error handling",
  "from": "voice"
}
```

**Response:**
```json
{
  "success": true,
  "queued": true
}
```

**Behavior:**
- Stores the message in `_wake_queue` (a simple `queue.Queue` or list on the server)
- If `listen(mode="wake")` is active, the message is picked up within 1 second
- If no listen is active, the message waits in the queue until one starts (or expires after 60 seconds)
- Queue size: 1 (only the latest message is kept — new message replaces old)

### 3. Menu Bar Wake Detector — Updated to use HTTP instead of tmux

The wake detector (`wake_detector.py`) changes:
- After transcription, instead of `inject_text()` via tmux/AppleScript
- Now calls `POST /wake_message` on the target session's HTTP port
- Multi-session routing stays the same (parse first word as session name)

### 4. Voice Rules — Tell Claude to call listen(mode="wake") when idle

Add to `templates/voice-rules.md`:

```markdown
## Wake Word (Hands-Free Mode)
- When you finish a task and have nothing else to do, call `listen(mode="wake", timeout=300)` to wait for voice commands.
- This does NOT open the mic — it waits for the user to say the wake phrase via the VoiceSmith menu bar.
- When a message arrives, process it as your next task.
- If timeout, call listen(mode="wake") again to keep waiting.
- Do NOT call listen(mode="wake") while actively working on a task.
```

---

## Multi-Session Routing

The menu bar app knows all active sessions from `sessions.json`. When the wake word triggers:

1. **Single session** → route there directly
2. **Multiple sessions** → parse first word of transcription:
   - Matches a session name (case-insensitive) → route to that session, strip name from message
   - No match → route to most recently active session (lowest `last_tool_call_age_s`)
3. **No active sessions** → show notification "No active sessions"
4. **Target session not in wake mode** → message queues and waits (up to 60s)

---

## Mic Ownership

**Clean separation:**
- **Menu bar app** → owns the mic for wake word detection (via audio-service socket)
- **MCP servers** → use the mic only for `listen(mode="mic")` / `speak_then_listen` (existing behavior)
- **No conflict** → wake detection and MCP listen use different audio paths

When the MCP server needs the mic for `speak_then_listen`:
1. The multi-client audio-service streams to both the menu bar detector and the MCP server simultaneously
2. OR the menu bar yields (disconnects from socket) — auto-resumes when the MCP listen ends

---

## Server Changes

### `server.py`

```python
# New global
_wake_queue = queue.Queue(maxsize=1)

# Extended listen tool
@mcp.tool()
async def listen(mode: str = "mic", timeout: float = 15, ...):
    if mode == "wake":
        return await _listen_wake(timeout)
    else:
        return await _listen_mic(timeout, ...)  # existing behavior

async def _listen_wake(timeout: float) -> dict:
    """Poll wake message queue until a message arrives or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            msg = _wake_queue.get_nowait()
            return {
                "success": True,
                "text": msg["text"],
                "source": "wake",
                "confidence": msg.get("confidence", 1.0),
            }
        except queue.Empty:
            await asyncio.sleep(1)
    return {"success": False, "error": "timeout", "message": "No wake message received within timeout"}

# New HTTP endpoint
def _handle_wake_message(self):
    params = self._read_json_body()
    if params is None:
        return
    text = params.get("text", "")
    if not text:
        self._json_response(400, {"error": "missing_text"})
        return
    # Clear old message, put new one
    while not _wake_queue.empty():
        try: _wake_queue.get_nowait()
        except: pass
    _wake_queue.put({"text": text, "confidence": params.get("confidence", 1.0)})
    self._json_response(200, {"success": True, "queued": True})
```

### `wake_detector.py`

Replace `inject_text()` with:

```python
def deliver_message(text: str, port: int):
    """Deliver transcribed wake message to a session via HTTP."""
    result = http_post(port, "/wake_message", {"text": text})
    if result and result.get("success"):
        emit("DELIVERED", str(port))
    else:
        emit("ERROR", f"delivery failed to port {port}")
```

---

## Voice Rules Changes

Add to `templates/voice-rules.md`:

```markdown
## Wake Word (Hands-Free Mode)
- When you finish a task and have nothing else to do, call `listen(mode="wake", timeout=300)`.
- This waits for voice commands from the VoiceSmith menu bar — it does NOT open the mic.
- When a message arrives, process it as your next task.
- On timeout, call `listen(mode="wake")` again to keep waiting.
- Do NOT call this while actively working — only when idle.
```

---

## What This Replaces

| Old approach | New approach |
|---|---|
| tmux send-keys | HTTP POST /wake_message |
| AppleScript keystroke | HTTP POST /wake_message |
| claude-commander socket | Not needed |
| Terminal wrapping | Not needed |
| Focus switching | Not needed |

**Everything flows through MCP and HTTP. No terminal hacks.**

---

## IDE Compatibility

| IDE | Works? | How |
|-----|--------|-----|
| Claude Code | Yes | listen(mode="wake") + HTTP |
| Cursor | Yes | Same MCP tools |
| VS Code | Yes | Same MCP tools |
| Codex | Yes | Same MCP tools |
| Any MCP client | Yes | Just needs listen tool + HTTP |

---

## Files to Modify

| File | Change |
|------|--------|
| `server.py` | Add `_wake_queue`, extend `listen` with `mode="wake"`, add `POST /wake_message` handler |
| `wake_detector.py` | Replace `inject_text()` with `deliver_message()` via HTTP |
| `templates/voice-rules.md` | Add wake word idle loop instructions |
| `menubar/VoiceSmithMenu.swift` | No changes needed (already manages detector) |

---

## Edge Cases

| Case | Behavior |
|------|----------|
| Claude busy when message arrives | Message queues, delivered when Claude calls listen(mode="wake") |
| Message queued but Claude never calls listen | Message expires after 60 seconds |
| Multiple messages before Claude picks up | Latest message replaces old (queue size 1) |
| User says wake word but no sessions | Menu bar shows notification |
| listen(mode="wake") cancelled by user (typing) | Normal MCP cancellation, Claude reads user's typed input instead |
| Session dies while message queued | Message lost — user can retry |

---

## Verification

1. Claude finishes a task → calls `listen(mode="wake")`
2. Menu bar detects "Hey Jarvis" → transcribes "add tests"
3. Menu bar posts to session's `/wake_message`
4. `listen(mode="wake")` returns `{"text": "add tests"}`
5. Claude processes "add tests" as the next task
6. Claude finishes → calls `listen(mode="wake")` again
7. Multi-session: "Hey Jarvis, Nova run the linter" → routes to Nova's port
8. No sessions active → notification shown
9. Claude is working (no listen active) → message queues, delivered when idle
10. User types while listen(mode="wake") is active → cancellation, typed input takes priority
