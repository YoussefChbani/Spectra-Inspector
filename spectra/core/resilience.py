"""How stable a selector is, and how targetable an element is.

Playwright and Testing Library prefer a role plus an accessible name or a
test id, and they tell you when a locator will rot. These grades do the same
for Spectra selectors. They use only the selector text and the element's
attributes, so they run without Windows and without a browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from .selector import Selector, parse

# A generated id, a CSS-in-JS class, or a framework prefix. Shared with the web back end.
_DYNAMIC_ID = re.compile(
    r"\d{4,}|^:|:$|^[0-9a-f]{8,}$|^(ember|react|mui-|radix-|headlessui-|rc_|__next)", re.I)
# ok1, item-2, btn12: a letter, more letters, then a counter. "save" and "h1" are not this.
_POSITIONAL_ID = re.compile(r"[A-Za-z].*[A-Za-z].*\d$")

# Points added for one exact, stable attribute on a selector step. The best
# handles (test id, automation id) are worth the most.
_ATTR_BONUS = {
    "data-testid": 24, "data-test-id": 24, "data-test": 22, "data-qa": 22, "data-cy": 22,
    "data-automation-id": 22, "automationid": 22, "aria-label": 16, "name": 14,
    "placeholder": 12, "text": 14, "own-text": 8, "id": 12, "controlid": 10,
    "alt": 10, "title": 8, "host": 8, "process": 6, "role": 6, "type": 4,
}
_STRONG = {
    "data-testid", "data-test-id", "data-test", "data-qa", "data-cy", "data-automation-id",
    "automationid", "aria-label", "name", "placeholder", "text",
}
_DYNAMIC_ATTRS = {"id", "class", "automationid", "name", "classname"}
_RUNTIME_ATTRS = {"hwnd", "pid", "frame-id"}
_GENERIC_ROLES = {"*", "div", "span", "pane", "custom", "group", "unknown", "document"}

# Best attribute first. grade_target keeps only the best one, so a test id is
# not watered down by also having a class.
_TARGET_WEIGHTS = (
    ("data-testid", 34), ("data-test-id", 34), ("data-test", 32), ("data-qa", 32),
    ("data-cy", 32), ("data-automation-id", 32), ("automationid", 30),
    ("aria-label", 18), ("name", 16), ("placeholder", 14), ("id", 14),
    ("controlid", 12), ("text", 12), ("title", 8), ("alt", 8),
)


def looks_dynamic(value: str) -> bool:
    """True when an id or class looks generated and will change next run."""
    return bool(_DYNAMIC_ID.search(value or ""))


def looks_positional(value: str) -> bool:
    """True for an id like ``ok1`` or ``item-2``, which follows the element's position."""
    return bool(_POSITIONAL_ID.search(value or ""))


@dataclass(frozen=True)
class Grade:
    """A score from 0 to 100 and a short list of reasons."""

    score: int
    band: str  # resilient, usable, fragile
    reasons: Tuple[str, ...]

    def summary(self) -> str:
        return f"{self.band} {self.score}/100"


def _band(score: int) -> str:
    if score >= 75:
        return "resilient"
    if score >= 50:
        return "usable"
    return "fragile"


def _finish(score: int, reasons: List[str]) -> Grade:
    score = max(0, min(100, score))
    if not reasons:
        reasons.append("The role and the attributes should still match after a normal change to the screen.")
    return Grade(score, _band(score), tuple(reasons[:6]))


def _clip(value: str, n: int = 32) -> str:
    value = " ".join(str(value).split())
    return value if len(value) <= n else value[: n - 1] + "…"


def grade(selector: Union[str, Selector]) -> Grade:
    """Grade a selector. A high score survives a reorder and a restyle.

    ``:nth()`` without an anchor, a generated id, ``hwnd``, and a long path
    pull the score down. A test id, an automation id, an accessible name, or
    a ``/below/`` style anchor pulls it up.
    """
    sel = selector if isinstance(selector, Selector) else parse(selector)
    score = 48
    reasons: List[str] = []
    anchored = any(st.relation for st in sel.steps)
    if anchored:
        score += 14
        reasons.append("Anchored to a nearby element, so reordering siblings does not move the match.")
    saw_strong = False
    for i, st in enumerate(sel.steps):
        if st.nth is not None and not st.relation:
            score -= 22
            reasons.append(f"Step {i + 1} uses :nth({st.nth}). That index changes when another element is inserted.")
        if i == len(sel.steps) - 1 and (st.role or "*").casefold() in _GENERIC_ROLES:
            score -= 6
        only_class = bool(st.filters)
        step_strong = False
        for f in st.filters:
            if f.name not in ("class", "style"):
                only_class = False
            if f.name in _RUNTIME_ATTRS:
                score -= 16
                reasons.append(f"{f.name} is assigned at runtime and will not match next time.")
                continue
            bonus = _ATTR_BONUS.get(f.name, 3)
            if f.op not in (None, "=", "?="):
                bonus = max(2, bonus // 3)
            val = f.value or ""
            if f.name == "id" and looks_positional(val):
                score -= 16
                reasons.append(f"id {_clip(val)} ends with a counter, so it changes when elements are reordered.")
                continue
            if f.name in _DYNAMIC_ATTRS and looks_dynamic(val):
                score -= 16
                reasons.append(f"{f.name} looks generated ({_clip(val)}) and will not be stable next run.")
                continue
            if bonus >= 14 and f.op in (None, "=", "?="):
                step_strong = True
            score += min(bonus, 24)
        if step_strong:
            saw_strong = True
        if only_class and st.filters:
            score -= 8
            reasons.append(f"Step {i + 1} only uses class, which changes when the styling changes.")
        if not st.filters and i > 0 and st.nth is None and not st.relation:
            score -= 4
    extra = len(sel.steps) - 4
    if extra > 0:
        score -= 4 * extra
        reasons.append(f"{len(sel.steps)} steps is a long path. It breaks if the tree gains a wrapper.")
    if not saw_strong and not anchored:
        score -= 6
        reasons.append("There is no automation id, test id, or accessible name.")
    return _finish(score, reasons)


def grade_target(node) -> Grade:
    """Grade the element itself: does it have a handle worth putting in a selector?"""
    score = 40
    reasons: List[str] = []
    role = (node.role() or "").strip()
    if not role or role.casefold() in _GENERIC_ROLES:
        score -= 8
        reasons.append("The role is generic, so the selector needs a stable attribute.")
    else:
        score += 6
        reasons.append(f"The role {role} is specific.")
    best_name: Optional[str] = None
    best_weight = 0
    dynamic_notes: List[str] = []
    for name, weight in _TARGET_WEIGHTS:
        try:
            val = node.get(name)
        except Exception:  # noqa: BLE001 - a back end that fails one property still gets a grade
            val = None
        if not val:
            continue
        if name in _DYNAMIC_ATTRS and looks_dynamic(str(val)):
            dynamic_notes.append(f"{name} looks generated ({_clip(val)}) and will change between runs.")
            continue
        if weight > best_weight:
            best_name, best_weight = name, weight
    if best_name:
        score += best_weight
        reasons.append(f"{best_name} is a stable handle.")
        reasons.extend(dynamic_notes[:1])
    elif dynamic_notes:
        score -= 10
        reasons.extend(dynamic_notes[:2])
    else:
        score -= 12
        reasons.append("There is no stable name, automation id, or test id.")
    return _finish(score, reasons)
