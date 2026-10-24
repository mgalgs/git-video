import os

# --voice picks an agent narrator when its command is on PATH (claude, with a
# credential variable set): a test must never start a real agent session.
# Tests that want one put a fake first on PATH (tests/test_fork_sandbox.py,
# tests/test_claude.py).
os.environ['PATH'] = os.pathsep.join(d for d in os.environ.get('PATH', '').split(os.pathsep)
                                     if not any(os.path.exists(os.path.join(d, c)) for c in ('fork-sandbox', 'claude')))
for _var in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_var, None)
