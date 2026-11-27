"""Find an element by where it sits relative to a landmark.

``:nth(2)`` breaks when a sibling is inserted. A relative selector does not::

    uia:Window[process="notepad.exe"] >> Text[name="Email"] /below/ Edit
    web:tab[host="shop.example"] >> label[text="Email"] /right/ input

``/below/``, ``/above/``, ``/left/``, ``/right/`` and ``/near/`` take the
previous match as a landmark and choose the nearest element in that direction
inside the same top-level window (or browser tab). They are not parent/child
combinators: the target is usually a sibling of the landmark.

Without ``:nth()``, only the nearest element is returned. Two elements within
two pixels of the same distance both match, so validation can still say the
selector is ambiguous. ``:nth(2)`` is the second nearest, ``:nth(-1)`` the
farthest.

The generator calls :func:`propose_anchor` when it would otherwise emit
``:nth()``. Elements with no rectangle, or two elements on the same rectangle,
keep the index.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .node import Node
from .resilience import looks_dynamic, looks_positional
from .selector import Selector

RELATIONS = ("below", "above", "left", "right", "near")
NEAR_PX = 96
_TIE_PX = 2.0

# Strongest landmark attribute first. At most two are written into the step.
_LANDMARK_ORDER = (
    "data-testid", "data-test-id", "data-test", "data-qa", "data-cy", "data-automation-id",
    "automationid", "aria-label", "name", "placeholder", "id", "controlid", "text", "title", "alt",
)
_LANDMARK_WEIGHT = {
    "data-testid": 5, "data-test-id": 5, "data-test": 5, "data-qa": 5, "data-cy": 5,
    "data-automation-id": 5, "automationid": 5, "aria-label": 3, "name": 3, "placeholder": 2,
    "id": 2, "controlid": 2, "text": 1, "title": 1, "alt": 1,
}


def _center(rect) -> Tuple[float, float]:
    return (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0


def _overlap(a0: float, a1: float, b0: float, b1: float, pad: float) -> bool:
    return a0 - pad < b1 and b0 - pad < a1


def relation_gap(relation: str, landmark: Node, node: Node) -> Optional[float]:
    """Distance along ``relation`` from ``landmark`` to ``node``, or None.

    None means the node is not in that direction. A smaller gap is closer.
    """
    if relation not in RELATIONS:
        return None
    lr, nr = landmark.rect(), node.rect()
    if not lr or not nr:
        return None
    if nr[2] <= nr[0] or nr[3] <= nr[1] or lr[2] <= lr[0] or lr[3] <= lr[1]:
        return None
    lx, ly = _center(lr)
    nx, ny = _center(nr)
    if abs(nx - lx) < 0.5 and abs(ny - ly) < 0.5:
        return None  # same rectangle: direction carries no information
    # A few pixels of slack for borders. More than that calls a control "beside"
    # a field when it is actually underneath a wide one.
    pad = 4.0
    if relation == "near":
        dist = ((nx - lx) ** 2 + (ny - ly) ** 2) ** 0.5
        if dist < 1 or dist > NEAR_PX:
            return None
        return dist
    if relation == "below":
        if ny <= ly or not _overlap(lr[0], lr[2], nr[0], nr[2], pad):
            return None
        return max(0.0, nr[1] - lr[3]) + 0.15 * abs(nx - lx)
    if relation == "above":
        if ny >= ly or not _overlap(lr[0], lr[2], nr[0], nr[2], pad):
            return None
        return max(0.0, lr[1] - nr[3]) + 0.15 * abs(nx - lx)
    if relation == "right":
        if nx <= lx or not _overlap(lr[1], lr[3], nr[1], nr[3], pad):
            return None
        return max(0.0, nr[0] - lr[2]) + 0.15 * abs(ny - ly)
    if relation == "left":
        if nx >= lx or not _overlap(lr[1], lr[3], nr[1], nr[3], pad):
            return None
        return max(0.0, lr[0] - nr[2]) + 0.15 * abs(ny - ly)
    return None


def holding(landmark: Node, node: Node) -> List[Tuple[float, str]]:
    """Relations that hold, tightest axis first, ``near`` last if it also holds."""
    found = []
    for rel in ("below", "above", "left", "right"):
        gap = relation_gap(rel, landmark, node)
        if gap is not None:
            found.append((gap, rel))
    found.sort()
    gap = relation_gap("near", landmark, node)
    if gap is not None:
        found.append((gap, "near"))
    return found


def nearest_matches(landmark: Node, nodes: Sequence[Node], relation: str,
                    nth: Optional[int]) -> List[Node]:
    """Nodes in ``relation`` to ``landmark``, nearest first.

    ``nth`` is 1-based into that order (negative counts from the far end).
    With no ``nth``, return the nearest, plus any other within two pixels.
    """
    ranked: List[Tuple[float, Node]] = []
    for node in nodes:
        gap = relation_gap(relation, landmark, node)
        if gap is not None:
            ranked.append((gap, node))
    ranked.sort(key=lambda item: item[0])
    if not ranked:
        return []
    if nth is not None:
        index = nth - 1 if nth > 0 else len(ranked) + nth
        return [ranked[index][1]] if 0 <= index < len(ranked) else []
    best = ranked[0][0]
    return [node for gap, node in ranked if gap - best <= _TIE_PX]


def stable_attrs(node: Node) -> List[str]:
    """Up to two attributes that can name ``node`` in a selector."""
    out = []
    for name in _LANDMARK_ORDER:
        try:
            val = node.get(name)
        except Exception:  # noqa: BLE001
            val = None
        if not val:
            continue
        text = str(val)
        if len(text) > 80 or looks_dynamic(text) or (name == "id" and looks_positional(text)):
            continue
        out.append(name)
        if len(out) == 2:
            break
    return out


def propose_anchor(engine, target: Node) -> Optional[Selector]:
    """A relative selector that finds exactly ``target``, or None.

    Used when the ordinary generator had to add ``:nth()``. The landmark is a
    nearby element with a stable name or id, not an ancestor of the target.
    """
    chain = target.ancestry()
    if len(chain) < 2 or not target.rect():
        return None
    window = chain[0]
    ancestors = set(chain)
    from .engine import Budget, SearchAborted
    from .node import walk

    budget = Budget(timeout=2.0, max_nodes=8000)
    ranked = []
    try:
        for node in walk(window, budget):
            if node in ancestors:
                continue
            rels = holding(node, target)
            attrs = stable_attrs(node)
            if not rels or not attrs:
                continue
            weight = sum(_LANDMARK_WEIGHT.get(a, 1) for a in attrs)
            ranked.append((rels[0][0], -weight, node, rels))
    except SearchAborted:
        pass
    ranked.sort(key=lambda item: (item[0], item[1]))

    tried = 0
    for _, _, landmark, rels in ranked[:12]:
        for _, rel in rels:
            if tried >= 10:
                return None
            tried += 1
            sel = _build(engine, window, landmark, target, rel)
            res = engine.find_all(sel, timeout=2.0)
            if res.error is None and res.matches == [target]:
                return sel
    return None


def _build(engine, window: Node, landmark: Node, target: Node, relation: str) -> Selector:
    top = engine.make_step(window, window.suggested_attrs(is_top=True), deep=False)
    top.tech = target.tech or window.tech
    mid = engine.make_step(landmark, stable_attrs(landmark), deep=True)
    attrs = stable_attrs(target) or target.suggested_attrs(is_top=False)
    tgt = engine.make_step(target, attrs[:2], deep=False)
    tgt.relation = relation
    return Selector([top, mid, tgt])
