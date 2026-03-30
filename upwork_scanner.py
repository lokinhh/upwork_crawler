"""
Entry point tương thích: python upwork_scanner.py (như trước).
Chuyển sang package upwork, gọi upwork.main.main().
"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if _root not in sys.path:
    sys.path.insert(0, str(_root))

from upwork.main import main

if __name__ == "__main__":
    main()
