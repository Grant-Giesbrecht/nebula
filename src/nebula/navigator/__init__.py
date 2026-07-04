"""
Nebula Navigator -- a Finder-like GUI browser over a nebula archive.

Kept as a self-contained subpackage that uses `nebula` purely as a library,
so it can be lifted into its own repo (and packaged as a macOS/Windows app)
without dragging the core along.

`model` has no GUI dependency and is always importable. The Flet view lives
in `app`; importing it (or calling launch()) requires Flet, so it's deferred
rather than imported at package load.
"""

from nebula.navigator import model


def launch(archive, archive_label=None):
    """Open the Navigator window on an archive (registered name or path).
    Imports Flet lazily so `import nebula.navigator` stays lightweight."""
    from nebula.navigator.app import launch as _launch

    return _launch(archive, archive_label=archive_label)


__all__ = ["model", "launch"]
