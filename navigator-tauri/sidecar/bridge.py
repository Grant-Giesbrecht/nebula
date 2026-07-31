"""PyInstaller entry point for the Nebula bridge.

Frozen into a standalone executable so the shipped .app carries its own
Python and needs nothing installed on the target machine. Behaviour is
identical to `python -m nebula.navigator.api`: no args -> stdio server,
args -> one-shot op (handy for testing the frozen binary directly).
"""

from nebula.navigator.api import main

if __name__ == "__main__":
    raise SystemExit(main())
