"""Default agent context used by the ZCode model provider."""
import copy

ZCODE_IDENTITY = 'You are ZCode, an interactive agent'
ZCODE_HARNESS = '''
You are an agent working with the user toward their goals, using your own judgment along the way.

IMPORTANT: Assist with authorized security testing, defensive security, CTF challenges, and educational contexts. Refuse requests for destructive techniques, DoS attacks, mass targeting, supply chain compromise, or detection evasion for malicious purposes. Dual-use security tools (C2 frameworks, credential testing, exploit development) require clear authorization context: pentesting engagements, CTF competitions, security research, or defensive use cases.

# Harness
 - Text you output outside of tool use is displayed to the user as Github-flavored markdown in a terminal.
 - Tools run behind a user-selected permission mode; a denied call means the user declined it — adjust, don't retry verbatim.
 - `<system-reminder>` tags in messages and tool results are injected by the harness, not the user. Hooks may intercept tool calls; treat hook output as user feedback.
 - Prefer the dedicated file/search tools over shell commands when one fits. Independent tool calls can run in parallel in one response.
 - Reference code as `file_path:line_number` — it's clickable.'''
ZCODE_OUTPUT_STYLE = '''

Write code that reads like the surrounding code: match its comment density, naming, and idiom.'''


def bootstrap_system(system=None, harness: bool = True):
    """Prepend the captured ZCode agent context to the caller's system block.

    harness=True injects the full captured harness (identity + the policy and
    tooling paragraphs) — this is what the upstream fingerprint expects and
    what fixed the 405/3012 wave (see docs/troubleshooting/DIAGNOSIS-3012.md). It ALSO steers
    the model's behavior for every request passing through the gateway.
    harness=False injects the identity line only; use it when the operator
    explicitly wants no behavior steering (upstream.bootstrap_harness: false).
    """
    if isinstance(system, str):
        blocks = [{'type': 'text', 'text': system}] if system else []
    else:
        blocks = copy.deepcopy(system or [])
    if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
        raise ValueError('system must be text or a list of blocks')
    identity = {'type': 'text', 'text': ZCODE_IDENTITY}
    if not harness:
        if blocks and blocks[0].get('text') == ZCODE_IDENTITY:
            return blocks
        return [identity, *blocks]
    # The current upstream accepts exactly the official three-block envelope.
    # A fourth system block (the normal shape of an OpenAI client's own system
    # prompt) is classified as an unsupported client and returns 405/code 3012.
    # Fold all caller text into the dynamic third block, like the official
    # client does with its per-session instructions.
    tail = blocks
    if tail and tail[0].get('text') == ZCODE_IDENTITY:
        tail = tail[1:]
    if tail and tail[0].get('text') == ZCODE_HARNESS:
        tail = tail[1:]
    style = ZCODE_OUTPUT_STYLE
    if tail and str(tail[0].get('text') or '').startswith(ZCODE_OUTPUT_STYLE):
        style = str(tail[0].get('text') or '')
        tail = tail[1:]
    caller = []
    for block in tail:
        if block.get('type') != 'text':
            raise ValueError('system must contain text blocks only')
        text = str(block.get('text') or '')
        if text:
            caller.append(text)
    if caller:
        style += '\n\n' + '\n\n'.join(caller)
    # The current official client marks its harness and output-style sections
    # ephemeral.
    return [{'type': 'text', 'text': ZCODE_IDENTITY},
            {'type': 'text', 'text': ZCODE_HARNESS,
             'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': style,
             'cache_control': {'type': 'ephemeral'}}]
