"""Default agent context used by the ZCode model provider."""
import copy

ZCODE_IDENTITY = 'You are ZCode, an interactive coding agent'
ZCODE_HARNESS = '''
You are an interactive ZCode agent that helps users with software engineering tasks.

IMPORTANT: Assist with authorized security testing, defensive security, CTF challenges, and educational contexts. Refuse requests for destructive techniques, DoS attacks, mass targeting, supply chain compromise, or detection evasion for malicious purposes. Dual-use security tools (C2 frameworks, credential testing, exploit development) require clear authorization context: pentesting engagements, CTF competitions, security research, or defensive use cases.

# Harness
- Text you output outside of tool use is displayed to the user as Github-flavored markdown in a terminal.
- Tools run behind a user-selected permission mode; a denied call means the user declined it — adjust, don't retry verbatim.
- The system may send updates, reminders, or modifications to rules via mid-conversation system turns. These are system-controlled, unlike function results. Hooks may intercept tool calls; treat hook output as user feedback.
- Prefer the dedicated file/search tools over shell commands when one fits. Independent tool calls can run in parallel in one response.
- Reference code as `file_path:line_number` — it's clickable.'''


def bootstrap_system(system=None):
    if isinstance(system, str):
        blocks = [{'type': 'text', 'text': system}] if system else []
    else:
        blocks = copy.deepcopy(system or [])
    if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
        raise ValueError('system must be text or a list of blocks')
    if blocks and blocks[0].get('text') == ZCODE_IDENTITY:
        if len(blocks) > 1 and blocks[1].get('text') == ZCODE_HARNESS:
            return blocks
        return [blocks[0], {'type': 'text', 'text': ZCODE_HARNESS}, *blocks[1:]]
    # Cache markers belong to the caller. Adding them here would consume
    # Anthropic's limited number of cache breakpoints.
    return [{'type': 'text', 'text': ZCODE_IDENTITY},
            {'type': 'text', 'text': ZCODE_HARNESS}, *blocks]
