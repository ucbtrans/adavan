"""
Shared pytest setup.

Two jobs, both of which must happen *before* any test module imports app.py
(pytest imports conftest.py first, so this file is the only safe place):

  1. Put the project root on sys.path so `import app`, `import events`, … work.
  2. Cut every outbound network path — AWS and Anthropic — at the seam where
     the client object is created.  The real modules are then imported as-is,
     so tests exercise the real import graph instead of a pile of fake
     `sys.modules` entries whose visibility depended on file collection order.

Nothing here stubs application modules.  A test that needs `put_event` or
`answer_question` to behave a certain way patches that name on the module
under test (see test_api_routes.py) — which works regardless of which test
file ran first.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Deterministic environment ────────────────────────────────────────────────
# app.py and assistant.py call load_dotenv(), which does not override values
# already present — so setting these first keeps a developer's real .env keys
# and table names out of the test run.
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"
os.environ.pop("ANTHROPIC_SSM_PARAM", None)   # else assistant._api_key() calls SSM
os.environ.pop("SESSIONS_TABLE", None)        # sessions.py falls back to its file backend
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

# ── No network, ever ────────────────────────────────────────────────────────
# boto3.client / boto3.resource are the only ways the app reaches AWS, and every
# call site is lazy (inside a function), so patching the factories is enough:
# `boto3.dynamodb.conditions.Key` and friends stay real.
_patchers = [
    patch("boto3.client", MagicMock(name="boto3.client")),
    patch("boto3.resource", MagicMock(name="boto3.resource")),
    # assistant.py does `from anthropic import Anthropic` at import time, so this
    # has to be started before assistant is first imported — i.e. here.
    patch("anthropic.Anthropic", MagicMock(name="Anthropic")),
]
for _p in _patchers:
    _p.start()


def pytest_unconfigure(config):
    for p in _patchers:
        p.stop()
