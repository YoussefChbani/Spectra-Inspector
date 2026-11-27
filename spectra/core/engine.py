"""Selector search and generation, independent of any technology."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .node import Backend, Node, walk
from .selector import Filter, Selector, Step, escape_wildcards, parse, step_matches


class SearchAborted(Exception):
    pass


class Budget:
    def __init__(self, timeout: float = 10.0, max_nodes: int = 200_000):
        self.deadline = time.monotonic() + timeout
        self.max_nodes = max_nodes
        self.visited = 0

    def tick(self, n: int = 1):
        self.visited += n
        if self.visited > self.max_nodes:
            raise SearchAborted(f"visited more than {self.max_nodes} elements")
        if (self.visited & 63) == 0 and time.monotonic() > self.deadline:
            raise SearchAborted("search timed out")


@dataclass
class SearchResult:
    matches: List[Node] = field(default_factory=list)
    elapsed_ms: float = 0.0
    visited: int = 0
    error: Optional[str] = None
    step_counts: List[int] = field(default_factory=list)  # matches after each step

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.matches) == 1


class Engine:
    def __init__(self):
        self.backends: Dict[str, Backend] = {}

    def register(self, backend: Backend):
        self.backends[backend.tech] = backend

    def backend(self, tech: str) -> Backend:
        if tech not in self.backends:
            raise KeyError(f"technology '{tech}' is not available")
        return self.backends[tech]

    # ------------------------------------------------------------------ search
    def _step_candidates(self, ctx: Node, step: Step, budget: Budget) -> List[Node]:
        fast = ctx.find(step, step.deep, budget)
        if fast is not None:
            budget.tick(len(fast))
            pool = fast
        elif step.deep:
            pool = walk(ctx, budget)
        else:
            pool = ctx.children()
            budget.tick(len(pool))
        out = [n for n in pool if step_matches(step, n)]
        if step.nth is not None:
            i = step.nth - 1 if step.nth > 0 else len(out) + step.nth
            out = [out[i]] if 0 <= i < len(out) else []
        return out

    def _by_relation(self, landmark: Node, step: Step, budget: Budget) -> List[Node]:
        """Nearest elements in ``step.relation`` to ``landmark``, inside its window."""
        from .anchor import nearest_matches
        chain = landmark.ancestry()
        root = chain[0] if chain else landmark
        pool = [n for n in walk(root, budget) if n != landmark and step_matches(step, n)]
        return nearest_matches(landmark, pool, step.relation, step.nth)

    def run(self, selector, timeout: float = 10.0, root: Optional[Node] = None,
            stop_after_step: Optional[int] = None) -> Tuple[SearchResult, List[List[Node]]]:
        """Run a selector. Returns the result and, per step, the list of
        contexts that step was evaluated from (useful for :nth computation)."""
        t0 = time.perf_counter()
        res = SearchResult()
        contexts_per_step: List[List[Node]] = []
        budget = Budget(timeout)
        try:
            sel = parse(selector) if isinstance(selector, str) else selector
            backend = self.backend(sel.tech)
            current = [root or backend.root()]
            steps = sel.steps if stop_after_step is None else sel.steps[:stop_after_step]
            for step in steps:
                contexts_per_step.append(current)
                nxt: List[Node] = []
                seen = set()
                if step.relation:
                    sources = ((landmark, self._by_relation(landmark, step, budget)) for landmark in current)
                else:
                    sources = ((ctx, self._step_candidates(ctx, step, budget)) for ctx in current)
                for _, found in sources:
                    for n in found:
                        k = (n.tech, n.key())
                        if k not in seen:
                            seen.add(k)
                            nxt.append(n)
                res.step_counts.append(len(nxt))
                current = nxt
                if not current:
                    break
            res.matches = current
        except SearchAborted as e:
            res.error = str(e)
        except Exception as e:  # noqa: BLE001 - shown to the user
            res.error = f"{type(e).__name__}: {e}"
        res.elapsed_ms = (time.perf_counter() - t0) * 1000
        res.visited = budget.visited
        return res, contexts_per_step

    def find_all(self, selector, timeout: float = 10.0) -> SearchResult:
        return self.run(selector, timeout)[0]

    def find_first(self, selector, timeout: float = 10.0) -> Optional[Node]:
        r = self.find_all(selector, timeout)
        return r.matches[0] if r.matches else None

    # ------------------------------------------------------------------ generation
    def make_step(self, node: Node, attrs: List[str], deep: bool) -> Step:
        filters = []
        for a in attrs:
            v = node.get(a)
            if v:
                filters.append(Filter(a, "=", escape_wildcards(v)))  # a real * in a title stays exact
        return Step(role=node.role() or "*", filters=filters, deep=deep)

    def generate(self, target: Node, timeout: float = 8.0) -> Tuple[Selector, SearchResult]:
        """Build a short selector that finds exactly ``target``.

        Strategy: the top-level ancestor plus the target; while that is
        ambiguous, add the nearest ancestor that carries a strong identifier;
        then fall back to :nth() on the target step; and if attributes do not
        even reach the target, emit a full child-by-child path. When :nth()
        was required and the elements have rectangles, a relative selector
        (``/below/`` and the others) replaces the index if it is at least as stable.
        """
        chain = target.ancestry()
        tech = target.tech
        deadline = time.monotonic() + timeout
        if not chain:
            raise ValueError("element has no ancestry")

        def build(included: List[int]) -> Selector:
            steps = []
            prev = None
            for idx in included:
                node = chain[idx]
                deep = prev is not None and idx != prev + 1
                if prev is None:
                    deep = idx != 0
                steps.append(self.make_step(node, node.suggested_attrs(is_top=(idx == 0)), deep))
                prev = idx
            steps[0].tech = tech
            return Selector(steps)

        last = len(chain) - 1
        included = sorted({0, last})
        tried_anchor = set()
        sel = build(included)
        res = self.find_all(sel, max(1.0, deadline - time.monotonic()))
        while res.error is None and target in res.matches and len(res.matches) > 1 \
                and time.monotonic() < deadline:
            anchor = self._pick_anchor(chain, included, tried_anchor)
            if anchor is None:
                break
            tried_anchor.add(anchor)
            cand = sorted(set(included) | {anchor})
            cand_sel = build(cand)
            cand_res = self.find_all(cand_sel, max(1.0, deadline - time.monotonic()))
            if cand_res.error is None and target in cand_res.matches and \
                    len(cand_res.matches) < len(res.matches):
                included, sel, res = cand, cand_sel, cand_res

        if res.error is None and target in res.matches and len(res.matches) > 1:
            sel = self._add_nth(sel, target)
            res = self.find_all(sel, max(1.0, deadline - time.monotonic()))
        if res.error is None and target in res.matches and len(res.matches) > 1 and len(sel.steps) > 1:
            # identical top-level windows (two "Untitled - Notepad"): index the window too
            sel = self._add_top_nth(sel, chain[0])
            res = self.find_all(sel, max(1.0, deadline - time.monotonic()))

        sel.included = list(included)
        if res.error is not None or target not in res.matches or len(res.matches) > 1:
            sel = self._full_path(chain)
            sel.included = list(range(len(chain)))
            res = self.find_all(sel, max(2.0, deadline - time.monotonic()))
        anchored = self._prefer_anchor(target, sel, res, deadline)
        if anchored is not None:
            return anchored
        return sel, res

    def _prefer_anchor(self, target: Node, sel: Selector, res: SearchResult, deadline: float):
        """Use a relative selector when :nth() was the only way to be unique."""
        if time.monotonic() >= deadline:
            return None
        if res.error or res.matches != [target]:
            return None
        if not sel.steps or sel.steps[-1].nth is None or sel.steps[0].nth is not None:
            return None
        from .anchor import propose_anchor
        from .resilience import grade
        alt = propose_anchor(self, target)
        if alt is None:
            return None
        alt_res = self.find_all(alt, max(1.0, min(3.0, deadline - time.monotonic())))
        if alt_res.error or alt_res.matches != [target]:
            return None
        new, old = grade(alt), grade(sel)
        if new.score > old.score or (new.score == old.score and sel.steps[-1].nth is not None):
            alt.included = None
            return alt, alt_res
        return None

    def heal(self, selector, hint: Optional[Node] = None, timeout: float = 6.0):
        """A replacement for a selector that matches nothing. See :mod:`spectra.core.heal`."""
        from .heal import heal
        return heal(self, selector, hint=hint, timeout=timeout)

    def _pick_anchor(self, chain, included, tried) -> Optional[int]:
        best, best_score = None, 0
        for idx in range(len(chain) - 2, 0, -1):
            if idx in included or idx in tried:
                continue
            node = chain[idx]
            score = 0
            for a in node.suggested_attrs():
                if node.get(a):
                    score += 3 if a in ("automationid", "id", "data-testid", "name") else 1
            # prefer ancestors close to the target
            score = score * 10 - (len(chain) - idx)
            if score > best_score:
                best, best_score = idx, score
        return best

    def _add_nth(self, sel: Selector, target: Node) -> Selector:
        res, ctxs = self.run(sel, stop_after_step=len(sel.steps))
        last = sel.steps[-1]
        if not ctxs:
            return sel
        budget = Budget(10)
        probe = Step(last.role, list(last.filters), None, last.visible, last.deep, last.tech)
        for ctx in ctxs[-1]:
            found = self._step_candidates(ctx, probe, budget)
            if target in found:
                last.nth = found.index(target) + 1
                break
        return sel

    def _add_top_nth(self, sel: Selector, top: Node) -> Selector:
        res, _ = self.run(sel, stop_after_step=1)
        if top in res.matches and len(res.matches) > 1:
            sel.steps[0].nth = res.matches.index(top) + 1
        return sel

    def _full_path(self, chain: List[Node]) -> Selector:
        steps = []
        parent = chain[0].parent()
        for i, node in enumerate(chain):
            st = self.make_step(node, node.suggested_attrs(is_top=(i == 0)), deep=False)
            siblings = parent.children() if parent is not None else []
            same = [s for s in siblings if step_matches(st, s)]
            if len(same) > 1 and node in same:
                st.nth = same.index(node) + 1
            steps.append(st)
            parent = node
        steps[0].tech = chain[0].tech
        return Selector(steps)
