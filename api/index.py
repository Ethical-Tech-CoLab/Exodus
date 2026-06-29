"""
Vercel serverless entry point. Vercel's Python builder treats anything under
api/ as a function — we re-export the FastAPI app defined in ../main.py so
Vercel's ASGI runtime can serve every route through one function.
"""
import os, sys

# Make the project root importable so `from main import app` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app  # noqa: E402,F401  (re-exported for Vercel)
