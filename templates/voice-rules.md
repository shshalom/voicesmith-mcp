# Voice Rules for AI Agents

These rules define how AI agents use the Agent Voice MCP Server for spoken interaction. Include this file from your CLAUDE.md or agent configuration.

---

## Voice State Awareness

- If the `speak` tool is available in your toolbox, use voice for all interactions following the rules below.
- If the `speak` tool is not available, respond in text only. Do not mention or reference voice capabilities.
- Use the `status` tool to check server health if voice seems unresponsive.

## Bookend Speaking Pattern

The main agent speaks **twice** per response -- an opening voice and a closing voice. Written text in between serves as the detailed/reference content.

### Opening Voice (Background, Non-Blocking)

- Fire immediately when you start working on a request.
- Keep it brief: 1 sentence acknowledging the request.
- Must run in the background (use `block: false`) so work starts in parallel.
- Examples: "Got it, looking into that now." / "Sure, updating the tests." / "On it."

### Closing Voice (Mandatory, After Work Completes)

- Speak after all work is done, summarizing the outcome.
- If asking a question: speak the question aloud.
- If a decision is pending: call out what you need from the user.
- If work is done: summarize the result.
- If an error occurred: explain what went wrong.
- Never skip the closing voice. The user relies on it to know you are finished.

## When to Speak

Speak in these situations:

1. **Acknowledge requests** -- Brief opening when the user asks for something.
2. **Summarize outcomes** -- Closing voice after completing work.
3. **Ask questions** -- Speak the question so the user can respond by voice.
4. **Report errors** -- Explain what failed and what to do next.
5. **Announce decisions** -- When you have made a choice the user should know about.

Do not narrate every small action. Speak at transitions: start, finish, error, question.

## Sub-Agent Voice Rules

Each sub-agent gets a **distinct voice** via the voice registry. When a sub-agent calls `speak` with its name, the server auto-assigns a unique voice that persists for the session.

### When Sub-Agents Must Speak

1. **Starting work** -- Announce intent before beginning a task.
2. **Finishing work** -- Summarize the result.
3. **Blocked** -- Explain what is needed to continue.
4. **Errors** -- Report what went wrong and whether recovery is possible.
5. **Handoffs** -- Announce when passing work to another agent.

### Handoff Protocol

When Agent A hands work to Agent B:

1. **Agent A speaks** (in A's voice): Announce the handoff and what B should do.
   - Example: "Nova, I found the route patterns. Can you scaffold the endpoint?"
2. **Agent B speaks** (in B's voice): Acknowledge before starting work.
   - Example: "Got it, scaffolding the endpoint now."

This creates an audible conversation between agents with distinct voices.

## Listen Behavior

When asking the user a question that expects a short answer:

1. Use `speak_then_listen` for an atomic speak-then-record flow, or call `speak` followed by `listen` separately.
2. If `listen` returns successfully, use the transcribed text as the user's response.
3. If `listen` returns cancelled or timeout, fall back to requesting text input. Do not retry `listen`.
4. Keep listen timeouts reasonable (10-15 seconds for simple questions).

## Mute Awareness

- If voice output seems unresponsive, call `status` to check if the server is muted.
- When muted, `speak` returns success but plays no audio. Respect this state -- do not repeatedly call speak trying to force audio.
- The user controls mute/unmute. Do not call `unmute` unless the user asks you to.

## General Guidelines

- Keep spoken messages concise: 1-2 sentences per voice call.
- Use natural, conversational language when speaking. Avoid technical jargon in spoken output.
- Written responses can be detailed; spoken responses should be summaries.
- Do not speak code, file paths, or long lists. Speak the summary; write the details.
