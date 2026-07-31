"""
Nebula Navigator -- a Finder-like browser over a nebula archive.

Kept as a self-contained subpackage that uses `nebula` purely as a library,
so it can be lifted into its own repo without dragging the core along.

There is no GUI toolkit in here. The user interface is the Tauri app in
`navigator-tauri/`, which talks to `api` over line-delimited JSON on
stdin/stdout; `model` is the toolkit-independent data layer both it and the
CLI build on. That keeps every module here importable and unit-testable
without a display.
"""

from nebula.navigator import model

__all__ = ["model"]
