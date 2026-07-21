"""ID management for clean IDs.

Assigns short, readable IDs to elements and maintains mapping to Google object IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


GOOGLE_OBJECT_ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{4,49}$")
_AUTHORED_ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
_GOOGLE_GENERATED_ID_RES = (
    re.compile(r"^[segml]\d+$"),
    re.compile(r"^g[0-9a-f]+_\d+_\d+$"),
    re.compile(r"^p\d+"),
    re.compile(r"^SLIDES_API"),
)


def authored_clean_id(google_id: str) -> str | None:
    """Return the reusable clean ID for a human-authored Google object ID.

    ``new_`` is the prefix used by older Slidesmith create requests. Removing
    it lets those objects join the same stable authored-ID round trip as new
    direct-ID creates.
    """
    candidate = google_id[4:] if google_id.startswith("new_") else google_id
    if not candidate or not _AUTHORED_ID_RE.fullmatch(candidate):
        return None
    if any(pattern.match(candidate) for pattern in _GOOGLE_GENERATED_ID_RES):
        return None
    return candidate


def is_valid_google_object_id(value: str) -> bool:
    """Return whether a value satisfies the Google Slides create-ID grammar."""
    return GOOGLE_OBJECT_ID_RE.fullmatch(value) is not None


@dataclass
class IDManager:
    """Manages assignment of clean IDs to elements.

    Human-authored page element IDs are preserved when safe. Generated
    fallbacks use prefixes:
    - s1, s2, ... for slides
    - e1, e2, ... for page elements (shapes, images, lines)
    - g1, g2, ... for groups
    - m1, m2, ... for masters
    - l1, l2, ... for layouts

    The mapping from clean ID to Google object ID is stored for use during push.
    """

    # Mapping: clean_id -> google_object_id
    id_mapping: dict[str, str] = field(default_factory=dict)

    # Reverse mapping: google_object_id -> clean_id
    _reverse_mapping: dict[str, str] = field(default_factory=dict)

    # Counters for each prefix
    _counters: dict[str, int] = field(default_factory=dict)

    def _next_id(self, prefix: str) -> str:
        """Generate next ID for a given prefix."""
        while True:
            count = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = count
            clean_id = f"{prefix}{count}"
            if clean_id not in self.id_mapping:
                return clean_id

    def _assign_id(
        self, google_id: str, prefix: str, *, preserve_authored: bool = False
    ) -> str:
        """Assign one mapping, optionally preserving a safe authored ID."""
        existing = self._reverse_mapping.get(google_id)
        if existing is not None:
            return existing
        clean_id = authored_clean_id(google_id) if preserve_authored else None
        if clean_id is None or clean_id in self.id_mapping:
            clean_id = self._next_id(prefix)
        self.id_mapping[clean_id] = google_id
        self._reverse_mapping[google_id] = clean_id
        return clean_id

    def assign_slide_id(self, google_id: str) -> str:
        """Assign a clean slide ID."""
        return self._assign_id(google_id, "s")

    def assign_element_id(
        self, google_id: str, *, preserve_authored: bool = False
    ) -> str:
        """Assign a clean element ID, optionally reusing an authored name."""
        return self._assign_id(
            google_id, "e", preserve_authored=preserve_authored
        )

    def assign_group_id(
        self, google_id: str, *, preserve_authored: bool = False
    ) -> str:
        """Assign a clean group ID, optionally reusing an authored name."""
        return self._assign_id(
            google_id, "g", preserve_authored=preserve_authored
        )

    def assign_master_id(self, google_id: str) -> str:
        """Assign a clean master ID."""
        return self._assign_id(google_id, "m")

    def assign_layout_id(self, google_id: str) -> str:
        """Assign a clean layout ID."""
        return self._assign_id(google_id, "l")

    def get_clean_id(self, google_id: str) -> str | None:
        """Get clean ID for a Google object ID."""
        return self._reverse_mapping.get(google_id)

    def get_google_id(self, clean_id: str) -> str | None:
        """Get Google object ID for a clean ID."""
        return self.id_mapping.get(clean_id)

    def to_dict(self) -> dict[str, str]:
        """Export mapping as dictionary for JSON serialization."""
        return dict(self.id_mapping)

    @classmethod
    def from_dict(cls, mapping: dict[str, str]) -> IDManager:
        """Create IDManager from a saved mapping dictionary."""
        manager = cls()
        manager.id_mapping = dict(mapping)
        manager._reverse_mapping = {v: k for k, v in mapping.items()}

        # Reconstruct counters from existing IDs
        for clean_id in mapping:
            generated = re.fullmatch(r"([segml])(\d+)", clean_id)
            if generated is None:
                continue
            prefix, number = generated.groups()
            manager._counters[prefix] = max(
                manager._counters.get(prefix, 0), int(number)
            )

        return manager


def assign_ids(
    presentation: dict[str, Any],
    existing_mapping: dict[str, str] | None = None,
) -> IDManager:
    """Assign clean IDs to all elements in a presentation.

    Processes in order:
    1. Masters
    2. Layouts
    3. Slides (in order)
       - Page elements within each slide (in document order)

    Args:
        presentation: Full presentation data from Google Slides API

    Returns:
        IDManager with all IDs assigned
    """
    manager = IDManager.from_dict(existing_mapping or {})

    # Assign master IDs
    for master in presentation.get("masters", []):
        google_id = master.get("objectId", "")
        if google_id:
            manager.assign_master_id(google_id)
            _assign_page_element_ids(master.get("pageElements", []), manager)

    # Assign layout IDs
    for layout in presentation.get("layouts", []):
        google_id = layout.get("objectId", "")
        if google_id:
            manager.assign_layout_id(google_id)
            _assign_page_element_ids(layout.get("pageElements", []), manager)

    # Assign slide IDs and their elements
    for slide in presentation.get("slides", []):
        google_id = slide.get("objectId", "")
        if google_id:
            manager.assign_slide_id(google_id)
            _assign_page_element_ids(slide.get("pageElements", []), manager)

    current_ids = set(_iter_presentation_ids(presentation))
    manager.id_mapping = {
        clean_id: google_id
        for clean_id, google_id in manager.id_mapping.items()
        if google_id in current_ids
    }
    manager._reverse_mapping = {
        google_id: clean_id
        for clean_id, google_id in manager.id_mapping.items()
    }

    return manager


def _iter_presentation_ids(presentation: dict[str, Any]) -> Any:
    """Yield all page and page-element IDs currently in a presentation."""
    for page_kind in ("masters", "layouts", "slides"):
        for page in presentation.get(page_kind, []) or []:
            page_id = page.get("objectId")
            if isinstance(page_id, str) and page_id:
                yield page_id
            yield from _iter_page_element_ids(page.get("pageElements", []))


def _iter_page_element_ids(elements: list[dict[str, Any]]) -> Any:
    for element in elements:
        object_id = element.get("objectId")
        if isinstance(object_id, str) and object_id:
            yield object_id
        yield from _iter_page_element_ids(
            element.get("elementGroup", {}).get("children", []) or []
        )


def _assign_page_element_ids(
    elements: list[dict[str, Any]], manager: IDManager
) -> None:
    """Assign IDs to page elements, including group children."""
    for elem in elements:
        google_id = elem.get("objectId", "")
        if not google_id:
            continue

        # Check if this is a group
        if "elementGroup" in elem:
            manager.assign_group_id(google_id, preserve_authored=True)
            # Recursively assign IDs to children
            children = elem["elementGroup"].get("children", [])
            _assign_page_element_ids(children, manager)
        else:
            manager.assign_element_id(google_id, preserve_authored=True)
