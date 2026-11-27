"""Rebuild a selector that no longer matches.

Commercial self-healing compares the element you used to find with what is on
screen now: same role, a name that is only a typo away, or the element you
still have selected. :func:`heal` does that with the live tree and then asks
the generator for a fresh selector, so the replacement can use an anchor
instead of ``:nth()``.

It returns None when several elements are equally plausible. Guessing wrong is
worse than asking the user to pick again.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional, Sequence

from .node import Node
from .selector import Selector, SelectorSyntaxError, Step, parse

_STRONG = {
    "name", "aria-label", "automationid", "placeholder", "text", "own-text",
    "data-testid", "data-test-id", "data-test", "data-qa", "data-cy", "data-automation-id",
}


@dataclass(frozen=True)
class HealHit:
    """A live element and a selector that finds it again."""

    node: Node
    selector: str
    confidence: float  # 0 to 1
    why: str


def heal(engine, selector, hint: Optional[Node] = None, timeout: float = 6.0) -> Optional[HealHit]:
    """Return a replacement for ``selector``, or None.

    ``hint`` is the element the user already selected, when there is one. It
    breaks a tie when nothing on screen is close to the selector text. A close
    name on a different element wins over the selection.
    """
    try:
        sel = parse(selector) if isinstance(selector, str) else selector
    except SelectorSyntaxError:
        return None
    res = engine.find_all(sel, timeout=min(3.0, timeout))
    if res.error:
        return None
    if len(res.matches) == 1:
        return HealHit(res.matches[0], sel.to_text(), 1.0, "The selector already matches one element.")
    if len(res.matches) > 1:
        return None

    scopes = _scopes(engine, sel, timeout)
    if not scopes:
        return None
    last = sel.steps[-1]
    pool = _pool(engine, scopes, timeout)
    # Score from the selector first. The selected element is only a tie-break
    # when nothing on screen is close to the text the user typed.
    plain = []
    for node in pool:
        points, why, sim = _score(node, last)
        if points > 0:
            plain.append((points, why, sim, node))
    if not plain:
        return None
    use_hint = hint is not None and max(item[2] for item in plain) < 0.72
    scored = []
    for points, why, _sim, node in plain:
        if use_hint and hint == node:
            points += 50
            if "still matches" not in why and "close" not in why:
                why = "This is the element that was already selected."
        scored.append((points / 100.0, node, why))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best, node, why = scored[0]
    second = scored[1][0] if len(scored) > 1 else None
    if best < 0.55 or (second is not None and best - second < 0.08):
        return None

    fresh, found = engine.generate(node, timeout=max(2.0, timeout))
    if found.error or found.matches != [node]:
        return None
    return HealHit(node, fresh.to_text(), min(1.0, best), why)


def _copy(step: Step) -> Step:
    return Step(step.role, list(step.filters), step.nth, step.visible, step.deep, step.tech, step.relation)


def _scopes(engine, sel: Selector, timeout: float) -> List[Node]:
    """The widest prefix of ``sel`` that still matches, else the technology root."""
    for count in range(len(sel.steps) - 1, 0, -1):
        prefix = Selector([_copy(s) for s in sel.steps[:count]])
        res = engine.find_all(prefix, timeout=min(2.0, timeout))
        if res.matches:
            return list(res.matches)
    relaxed = Selector([_copy(s) for s in sel.steps])
    for step in reversed(relaxed.steps):
        while step.filters:
            step.filters.pop()
            res = engine.find_all(relaxed, timeout=min(2.0, timeout))
            if res.matches:
                return list(res.matches)
    try:
        return [engine.backend(sel.tech).root()]
    except KeyError:
        return []


def _pool(engine, scopes: Sequence[Node], timeout: float) -> List[Node]:
    from .engine import Budget, SearchAborted
    from .node import walk

    budget = Budget(min(timeout, 3.0), max_nodes=12000)
    out: List[Node] = []
    seen = set()
    try:
        for scope in scopes:
            for node in walk(scope, budget):
                key = (node.tech, node.key())
                if key in seen:
                    continue
                seen.add(key)
                out.append(node)
    except SearchAborted:
        pass
    return out


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _score(node: Node, step: Step):
    """Points out of 100, an explanation, and how close the selector text is (0 to 1)."""
    role_ok = step.role in ("*", "") or step.role_matches(node.role())
    if not role_ok:
        return 0, "", 0.0
    points = 40 if role_ok and step.role not in ("*", "") else 0
    why = f"The role {node.role()} matches." if points else ""
    best_sim = 0.0
    sim_why = ""
    for f in step.filters:
        try:
            actual = node.get(f.name) or ""
        except Exception:  # noqa: BLE001
            actual = ""
        if f.matches(actual):
            gain = 60 if f.name in _STRONG else 16
            points += gain
            if f.name in _STRONG:
                best_sim = 1.0
            if gain >= 60:
                why = f"The {f.name} still matches."
            continue
        if actual and f.value:
            ratio = _ratio(f.value, actual)
            if ratio >= 0.6:
                points += int(24 * ratio)
                if ratio > best_sim:
                    best_sim = ratio
                    sim_why = f"The {f.name} is close: selector {f.value!r}, element {actual!r}."
    if sim_why and "still matches" not in why:
        why = sim_why
    if not why:
        why = f"The role {node.role()} is the closest remaining candidate."
    return points, why, best_sim
