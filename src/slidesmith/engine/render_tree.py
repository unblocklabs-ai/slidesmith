"""Render tree construction.

Converts Google's flat, back-to-front element list into a visual containment
hierarchy without changing its paint order. Elements that are visually
contained within others may become children in the tree when the containing
subtree is contiguous in document order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from slidesmith.engine.bounds import BoundingBox, Transform, get_bounds, get_group_bounds


@dataclass
class RenderNode:
    """A node in the render tree.

    Represents an element with its visual containment children.
    """

    # Element data from API
    element: dict[str, Any]

    # Computed bounding box
    bounds: BoundingBox

    # Clean ID (assigned by IDManager)
    clean_id: str = ""

    # Original sibling position in Google's pageElements array. Google exposes
    # that array in back-to-front order; retain it through SML regeneration.
    source_order: int = 0

    # Children in the render tree (visually contained elements)
    children: list[RenderNode] = field(default_factory=list)

    # Parent node (None for top-level elements)
    parent: RenderNode | None = None

    @property
    def element_type(self) -> str:
        """Get the element type (shape type, IMAGE, LINE, GROUP, etc.)."""
        if "shape" in self.element:
            shape_type: str = self.element["shape"].get("shapeType", "SHAPE")
            return shape_type
        if "image" in self.element:
            return "IMAGE"
        if "line" in self.element:
            return "LINE"
        if "elementGroup" in self.element:
            return "GROUP"
        if "table" in self.element:
            return "TABLE"
        if "video" in self.element:
            return "VIDEO"
        if "sheetsChart" in self.element:
            return "SHEETS_CHART"
        return "UNKNOWN"

    @property
    def has_text(self) -> bool:
        """Check if this element contains text."""
        if "shape" in self.element:
            return "text" in self.element["shape"]
        return False

    def relative_bounds(self) -> BoundingBox:
        """Get bounds relative to parent.

        If no parent, returns absolute bounds.
        """
        if self.parent is None:
            return self.bounds
        return self.bounds.relative_to(self.parent.bounds)


def build_render_tree(
    elements: list[dict[str, Any]],
    id_manager: Any | None = None,
) -> list[RenderNode]:
    """Build render tree from flat element list.

    Creates a visual containment hierarchy where elements that are visually
    contained within larger elements become children.

    Args:
        elements: List of pageElements from Google Slides API
        id_manager: Optional IDManager to look up clean IDs
    Returns:
        List of root nodes (top-level elements)
    """
    if not elements:
        return []

    # First, flatten any API groups and create nodes
    nodes = _create_nodes(elements, id_manager)

    # Walk in Google's back-to-front order. The stack is the path to the most
    # recently painted node. A parent can accept the next node only when it
    # contains that node and the previous paint slot was the end of the
    # parent's current subtree. Popping a candidate closes its subtree
    # permanently, so a later overlapping element cannot reopen it and move
    # across an unrelated sibling in the generated document.
    roots: list[RenderNode] = []
    container_stack: list[RenderNode] = []
    subtree_end: dict[int, int] = {
        id(node): node.source_order for node in nodes
    }

    for node in nodes:
        while container_stack:
            candidate = container_stack[-1]
            is_contiguous = subtree_end[id(candidate)] == node.source_order - 1
            is_containing = (
                candidate.bounds.area > node.bounds.area
                and candidate.bounds.contains(node.bounds, 0.7)
            )
            # A loose element must never be inferred as a child of a real API
            # group. The group's native children are already attached below
            # it, and the group itself remains one paint-order slot here.
            is_native_group = "elementGroup" in candidate.element
            if is_contiguous and is_containing and not is_native_group:
                break
            container_stack.pop()

        if container_stack:
            parent = container_stack[-1]
            parent.children.append(node)
            node.parent = parent

            # Every ancestor's subtree now ends at this paint slot. This is
            # what lets a larger candidate remain available around a nested
            # sequence while rejecting it after an interleaved sibling.
            for ancestor in container_stack:
                subtree_end[id(ancestor)] = node.source_order
        else:
            roots.append(node)

        container_stack.append(node)

    _sort_children(roots)

    return roots


def _create_nodes(
    elements: list[dict[str, Any]],
    id_manager: Any | None,
    parent_transform: Transform | None = None,
) -> list[RenderNode]:
    """Create RenderNodes from elements, handling groups.

    Args:
        elements: List of pageElements from Google Slides API
        id_manager: Optional IDManager to look up clean IDs
        parent_transform: Transform from parent element (for nested groups)
    """
    nodes: list[RenderNode] = []

    for source_order, elem in enumerate(elements):
        google_id = elem.get("objectId", "")
        clean_id = ""
        if id_manager:
            clean_id = id_manager.get_clean_id(google_id) or ""

        # Handle groups specially - they contain children
        if "elementGroup" in elem:
            # Get this group's transform and compose with parent
            group_transform = Transform.from_element(elem)
            if parent_transform:
                composed_transform = parent_transform.compose(group_transform)
            else:
                composed_transform = group_transform

            # Create nodes for children, passing composed transform
            children_elements = elem["elementGroup"].get("children", [])
            child_nodes = _create_nodes(
                children_elements, id_manager, composed_transform
            )

            # Compute group bounds from children
            if child_nodes:
                child_bounds = [n.bounds for n in child_nodes]
                group_bounds = get_group_bounds(child_bounds)
            else:
                group_bounds = get_bounds(elem, parent_transform)

            # Create group node
            group_node = RenderNode(
                element=elem,
                bounds=group_bounds,
                clean_id=clean_id,
                source_order=source_order,
            )

            # Attach children to group (these are API group children, not render tree children)
            # For groups, we keep the API structure
            for child_node in child_nodes:
                child_node.parent = group_node
                group_node.children.append(child_node)

            nodes.append(group_node)
        else:
            # Regular element - compute bounds with parent transform
            bounds = get_bounds(elem, parent_transform)
            node = RenderNode(
                element=elem,
                bounds=bounds,
                clean_id=clean_id,
                source_order=source_order,
            )
            nodes.append(node)

    return nodes


def _sort_children(nodes: list[RenderNode]) -> None:
    """Recursively retain Google's back-to-front sibling order."""
    for node in nodes:
        if node.children:
            node.children.sort(key=lambda n: n.source_order)
            _sort_children(node.children)
