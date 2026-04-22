"""
conftest.py — Pytest configuration.

Puts the project root on sys.path so `import config`, `import strategy`, etc.
work when running tests from any directory.
"""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
