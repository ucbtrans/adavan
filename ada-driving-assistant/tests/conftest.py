# Ensure the project root is on sys.path so tests can import app, events, etc.
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
