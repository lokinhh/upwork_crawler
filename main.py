"""
Entry point khi chạy: python main.py (từ thư mục gốc repo).
"""
import sys
from pathlib import Path

# Đảm bảo gốc repo trên sys.path khi chạy python main.py
_root = Path(__file__).resolve().parent
if _root not in sys.path:
    sys.path.insert(0, str(_root))

from upwork.main import main

if __name__ == "__main__":
    main()
