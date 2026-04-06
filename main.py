"""
Entry point when running: python main.py (from the repo root directory).
"""
import sys
from pathlib import Path

# Make sure the repo root is on sys.path when running python main.py
_root = Path(__file__).resolve().parent
if _root not in sys.path:
    sys.path.insert(0, str(_root))

from upwork.main import main

if __name__ == "__main__":
    main()
