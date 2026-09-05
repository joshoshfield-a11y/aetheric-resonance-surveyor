#!/usr/bin/env python3
"""Thin launcher for the ARS command-line interface."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ars_core.cli import main

if __name__ == "__main__":
    sys.exit(main())
