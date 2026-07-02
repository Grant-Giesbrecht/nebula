"""
Structured references between artifacts.

On disk, refs are compact strings:

    "diode.graf"                    -> same-session file
    "S-0152"                         -> whole-session reference, same archive
    "S-0152/diode.graf"              -> session + file, same archive
    "postdoc|S-0152/diode.graf"      -> cross-archive reference
    "postdoc|S-0152"                 -> whole-session, cross-archive

In memory, refs are a small immutable dataclass so every consumer reads
fields instead of re-parsing strings. There is exactly one parser and one
formatter; nothing else in this codebase should split ref strings by hand.

A ref with session=None means "this same session" -- the common case of
one artifact in a session derived from another artifact in the same
session. archive=None likewise means "this same archive".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REF_ARCHIVE_SEP = "|"
REF_PATH_SEP = "/"

# Kept in sync with session.SESSION_ID_PREFIX. Used to disambiguate a bare
# token ("S-0152" vs "diode.graf") when there's no '/' to split on.
SESSION_PREFIX = "S-"


@dataclass(frozen=True)
class Ref:
    """A reference to a session, or a specific artifact within a session.

    file:    the artifact filename, or None if this ref points at a whole
             session.
    session: the session id (e.g. "S-0152"), or None to mean "this same
             session" (only meaningful for same-archive refs).
    archive:   the archive name (e.g. "postdoc"), or None to mean "this same
             archive".
    """

    file: Optional[str] = None
    session: Optional[str] = None
    archive: Optional[str] = None

    def is_cross_archive(self) -> bool:
        return self.archive is not None

    def is_same_session(self) -> bool:
        return self.session is None

    def resolved(self, *, archive: str, session: str) -> "Ref":
        """Return a copy with archive/session filled in from context
        wherever this ref left them implicit (None)."""
        return Ref(
            file=self.file,
            session=self.session or session,
            archive=self.archive or archive,
        )


def parse_ref(text: str) -> Ref:
    """Parse a ref string into a Ref.

    Handles all forms:
        "diode.graf"                  -> file only, same session/archive
        "S-0152"                      -> whole-session, same archive
        "S-0152/diode.graf"           -> session + file, same archive
        "postdoc|S-0152/diode.graf"   -> fully qualified

    Raises ValueError on malformed input rather than guessing, since a
    silently-wrong provenance link is worse than a loud failure.
    """
    if not text or not text.strip():
        raise ValueError("empty ref string")

    text = text.strip()
    archive: Optional[str] = None

    if REF_ARCHIVE_SEP in text:
        parts = text.split(REF_ARCHIVE_SEP)
        if len(parts) != 2:
            raise ValueError(f"malformed ref (multiple '|'): {text!r}")
        archive, text = parts
        archive = archive.strip()
        text = text.strip()
        if not archive:
            raise ValueError(f"malformed ref (empty archive before '|'): {text!r}")
        if not text:
            raise ValueError(f"malformed ref (nothing after '|'): {text!r}")

    if REF_PATH_SEP in text:
        session_id, _, filename = text.partition(REF_PATH_SEP)
        session_id = session_id.strip()
        filename = filename.strip()
        if not session_id or not filename:
            raise ValueError(f"malformed ref (empty session/file): {text!r}")
        return Ref(session=session_id, file=filename, archive=archive)

    # No '/': either a bare session id or a bare filename. Disambiguate by
    # convention -- session ids always start with "S-".
    if text.startswith(SESSION_PREFIX):
        return Ref(session=text, file=None, archive=archive)
    return Ref(session=None, file=text, archive=archive)


def format_ref(ref: Ref) -> str:
    """Format a Ref back into its compact string form."""
    if ref.file and ref.session:
        body = f"{ref.session}{REF_PATH_SEP}{ref.file}"
    elif ref.file:
        body = ref.file
    elif ref.session:
        body = ref.session
    else:
        raise ValueError("Ref must have at least a session or a file")

    if ref.archive:
        return f"{ref.archive}{REF_ARCHIVE_SEP}{body}"
    return body
