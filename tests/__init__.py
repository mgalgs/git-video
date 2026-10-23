import os

# --voice picks the fork-sandbox narrator when the command is on PATH: a test
# must never start a real agent session. Tests that want one put a fake first
# on PATH (tests/test_fork_sandbox.py).
os.environ['PATH'] = os.pathsep.join(d for d in os.environ.get('PATH', '').split(os.pathsep)
                                     if not os.path.exists(os.path.join(d, 'fork-sandbox')))
