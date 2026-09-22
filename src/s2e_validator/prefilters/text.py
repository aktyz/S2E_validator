"""Text preparation shared by every filter that reads a description.

Two forms, for two purposes. `normalize` keeps the text as the contributor wrote
it, only tidying whitespace, and is what length rules measure. `canonical`
throws away everything that does not change what a reader sees, and is what
fingerprints and uniqueness keys are built from, so invisible edits cannot pass
a copy off as new.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Trim and collapse whitespace; nothing else changes."""
    return _WHITESPACE_RUN.sub(" ", text).strip()


def canonical(text: str) -> str:
    """The form two visually identical captions share.

    - NFKC folds compatibility forms and recomposes decomposed letters, so
      "ł" typed as one code point and "l" plus a combining stroke match.
    - Unicode category Cf is dropped: zero-width spaces, joiners and the BOM
      render as nothing, but inserted between letters they change almost every
      shingle. `text_format` deliberately lets them through, since emoji depend
      on them, so this is the place to neutralise them.
    - Whitespace is collapsed and the result case-folded.
    """
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(char for char in folded if unicodedata.category(char) != "Cf")
    return normalize(visible).casefold()
