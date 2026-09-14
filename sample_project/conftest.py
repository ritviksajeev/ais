"""Put the project root on ``sys.path`` so tests can import the modules flatly.

The mediator treats this file as part of every sandbox closure: without it the
test suite cannot import the module under edit, so a sandbox that omitted it
would report import errors instead of real behaviour.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
