# Resolved: missing ZCode agent initialization - 2026-10-09

The gateway initially omitted ZCode's default system context. A minimal chat
request received HTTP 405/code 3012, while the same account and model accepted
that request with the native identity and harness blocks. Adding those defaults
resolved the rejection for normal chat requests.

## Controlled comparison

The fresh session at `D:/JB/zcode-mitm/sessions/20261009-062246/flows` contains
successful official-client `hi` requests for both GLM models. The captured
credential matches the currently signed-in account and pool entry 3.

Using that credential and GLM-5.3-Flash:

| Request body | Observed result |
| --- | --- |
| Minimal OpenAI `hi`, before correction | 405 / 3012 |
| Full native body | 200, completed response |
| Native body without its 77 tools | 200, completed response |
| Simple `hi` with the three native system blocks, no tools | 200, completed response |
| Simple `hi` with only identity and harness blocks, no tools | 200, completed response |
| Identity block alone | 405 / 3012 |
| Identity and harness without cache markers | 200, completed response |
| Normal OpenAI `hi`, after correction | 200, completed response |

These observations establish a working minimal initialization. They do not
reveal the server's internal rule or prove that every sentence of the harness
is individually required.

## Implementation

`zcode_bootstrap.py` contains the stable identity and harness text observed in
the successful captures. `build_body` prepends those blocks for OpenAI and
Anthropic requests, preserving caller instructions, messages, tools, images,
cache markers, and native thinking signatures. Existing native bootstrap
blocks are preserved without duplication. The injected defaults have no cache
markers, so they do not consume additional cache breakpoints.

No workspace paths, captured conversation history, environment block, or native
tool library are inserted into client requests.

Earlier corrections also restored HTTP-Referer and the saved device identity.
Those corrections alone did not resolve 3012. Model requests use device metadata;
billing/OAuth requests retain X-Device-Mid.

## Reasoning and validation

Turning Flash thinking off with a valid native context returned HTTP 400/code 1210:
the model always reasons and requires low, high, or max. Unsupported disabled
thinking now fails locally with a clear explanation, and client configuration
no longer advertises an off variant.
The equivalent GLM-5.3 probe encountered model overload, so its backend behavior
when thinking is disabled was not established. Both models expose only the
captured low/high/max modes through this adapter.

All 127 offline tests passed. Real HTTP API checks succeeded for normal Flash
OpenAI chat, GLM-5.3 streaming with content and DONE, Anthropic messages with a
caller system instruction, and a caller-supplied weather function. The requested
function was returned without any injected tool definitions or local execution.
Temporary GLM-5.3 overloads (529/code 1305) occurred before its successful reply;
these are separate from the resolved 3012 rejection.
The rebuilt executable on port 8788 was then verified with normal Flash chat
and GLM-5.3 streaming; both completed successfully with HTTP 200. Capture has
been stopped and the official client's original proxy settings restored.

The installed client has API-key signing support, but explicitly skips signing
for Start Plan account access. Missing signing was not the cause of this error.

## Evidence scope

- The user's historical and fresh retained captures.
- Direct model requests and local HTTP API observations.
- Installed `C:/Program Files/ZCode/resources/glm/zcode.cjs` static inspection.
- Static REA evidence:
  `ev_7852ff5dab7b60c9b215e3146bcac8ad039c55a192a73e1e72cd8836aa584fa7`,
  covering a read-only copy of that bundle, not the entire Electron application.

The server source and its internal abuse-rule diagnostics are unavailable.
No credentials or private message contents are included here.
