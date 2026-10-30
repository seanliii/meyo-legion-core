#!/usr/bin/env python3
"""Thin local command; the existing Legion server remains the runtime."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "release/legion-service"))
from personal_runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
