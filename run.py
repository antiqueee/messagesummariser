#!/usr/bin/env python3
"""
Telegram Chat Summarizer - запуск приложения
"""
import os
import sys
from pathlib import Path

# Auto-activate venv if it exists and we're not already inside it
_venv = Path(__file__).parent / "venv"
if _venv.exists() and sys.prefix == sys.base_prefix:
    _py = _venv / "bin" / "python3"
    if _py.exists():
        os.execv(str(_py), [str(_py)] + sys.argv)

# Fix encoding for non-ASCII (Russian) text
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

import uvicorn

if __name__ == "__main__":
    reload_enabled = os.getenv("UVICORN_RELOAD", "").lower() in {"1", "true", "yes", "on"}
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=8000,
        reload=reload_enabled
    )
