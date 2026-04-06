"""
Compatible entry point: python upwork_scanner.py (as before).
Switch to the upwork package, call upwork.main.main().
"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if _root not in sys.path:
    sys.path.insert(0, str(_root))

from upwork.main import main

if __name__ == "__main__":
    main()
