"""Convenience launcher for DL24 web dashboard."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from web.server import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9090)
