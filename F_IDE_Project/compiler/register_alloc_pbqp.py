"""Solve PBQP allocation by isolated-node reduction, exact search, or iterative heuristics."""
from typing import Dict, List, Tuple, Set
import math


class PBQPVector:

    def __init__(self, costs: List[float]):
        self.costs = list(costs)

    def __len__(self) -> int:
        return len(self.costs)

    def __getitem__(self, i: int) -> float:
        return self.costs[i]

    def min_cost(self) -> Tuple[int, float]:
        min_idx = 0
        min_val = self.costs[0]
        for i, c in enumerate(self.costs):
            if c < min_val:
                min_val = c
                min_idx = i
        return min_idx, min_val


class PBQPMatrix:
    """Rows index options for node u; columns index options for node v."""
    def __init__(self, costs: List[List[float]]):
        self.costs = [list(row) for row in costs]
        self.rows = len(costs)
        self.cols = len(costs[0]) if costs else 0

    def __getitem__(self, idx: Tuple[int, int]) -> float:
        return self.costs[idx[0]][idx[1]]


class PBQPGraph:

    def __init__(self):
        self.vectors: Dict[int, PBQPVector] = {}
        self.matrices: Dict[Tuple[int, int], PBQPMatrix] = {}
        self.neighbors: Dict[int, Set[int]] = {}

    def add_node(self, nid: int, vector: PBQPVector):
        self.vectors[nid] = vector
        self.neighbors.setdefault(nid, set())

    def add_edge(self, u: int, v: int, matrix: PBQPMatrix):
        if u == v:
            return
        if u > v:
            u, v = v, u
        self.matrices[(u, v)] = matrix
        self.neighbors[u].add(v)
        self.neighbors[v].add(u)

    def remove_node(self, nid: int):
        for neighbor in list(self.neighbors.get(nid, set())):
            self.neighbors[neighbor].discard(nid)
            key = (min(nid, neighbor), max(nid, neighbor))
            if key in self.matrices:
                del self.matrices[key]
        if nid in self.vectors:
            del self.vectors[nid]
        if nid in self.neighbors:
            del self.neighbors[nid]


class PBQPSolver:


    def __init__(self, graph: PBQPGraph):
        self.g = graph
        self.solution: Dict[int, int] = {}
        self.best_cost = float('inf')
        # Track isolated-node costs separately after removing those nodes from the graph.
        self._reduced_cost = 0.0

    def solve(self) -> Tuple[Dict[int, int], float]:
        self.solution = {}
        self.best_cost = float('inf')
        self._reduced_cost = 0.0

        self._reduce_r0()

        remaining = list(self.g.vectors.keys())
        if not remaining:
            cost = self._compute_cost(self.solution)
            return self.solution, cost

        if len(remaining) <= 12:
            self._exact_backtrack(remaining)
        else:
            self._heuristic_solve(remaining)

        return self.solution, self.best_cost

    def _reduce_r0(self):

        changed = True
        while changed:
            changed = False
            isolated = [nid for nid in self.g.vectors if not self.g.neighbors.get(nid)]
            for nid in isolated:
                best_opt, best_unary = self.g.vectors[nid].min_cost()
                self.solution[nid] = best_opt
                self._reduced_cost += best_unary
                self.g.remove_node(nid)
                changed = True

    def _exact_backtrack(self, nodes: List[int]):
        """Use branch-and-bound search for graphs of at most twelve nodes."""
        nodes = list(nodes)
        nodes.sort(key=lambda nid: (len(self.g.vectors[nid]), len(self.g.neighbors.get(nid, set()))))

        def partial_cost(assignment: Dict[int, int], nid: int, opt: int) -> float:
            c = self.g.vectors[nid][opt]
            for nb in self.g.neighbors.get(nid, set()):
                if nb in assignment:
                    u, v = (nid, nb) if nid < nb else (nb, nid)
                    mat = self.g.matrices[(u, v)]
                    if nid < nb:
                        c += mat[opt, assignment[nb]]
                    else:
                        c += mat[assignment[nb], opt]
            return c

        best_local: Dict[int, int] = {}
        best_local_cost = float('inf')

        def backtrack(idx: int, assignment: Dict[int, int], current_cost: float):
            nonlocal best_local, best_local_cost
            if current_cost >= best_local_cost:
                return
            if idx == len(nodes):
                if current_cost < best_local_cost:
                    best_local_cost = current_cost
                    best_local = dict(assignment)
                return

            nid = nodes[idx]
            opts = list(range(len(self.g.vectors[nid])))
            opts.sort(key=lambda o: partial_cost(assignment, nid, o))

            for opt in opts:
                new_cost = current_cost + partial_cost(assignment, nid, opt)
                if new_cost >= best_local_cost:
                    continue
                assignment[nid] = opt
                backtrack(idx + 1, assignment, new_cost)
                del assignment[nid]

        backtrack(0, {}, 0.0)

        if best_local_cost < self.best_cost:
            self.best_cost = best_local_cost
            self.solution.update(best_local)

    def _heuristic_solve(self, nodes: List[int]):
        """Use greedy assignments, local improvement, and random restarts for larger graphs."""
        nodes = list(nodes)

        def full_cost(assignment: Dict[int, int]) -> float:
            c = 0.0
            for nid, opt in assignment.items():
                c += self.g.vectors[nid][opt]
            for (u, v), mat in self.g.matrices.items():
                if u in assignment and v in assignment:
                    c += mat[assignment[u], assignment[v]]
            return c

        def marginal_cost(assignment: Dict[int, int], nid: int) -> float:
            opt = assignment[nid]
            c = self.g.vectors[nid][opt]
            for nb in self.g.neighbors.get(nid, set()):
                if nb in assignment:
                    u, v = (nid, nb) if nid < nb else (nb, nid)
                    mat = self.g.matrices[(u, v)]
                    if nid < nb:
                        c += mat[opt, assignment[nb]]
                    else:
                        c += mat[assignment[nb], opt]
            return c

        def greedy(order: List[int]) -> Tuple[Dict[int, int], float]:
            assignment: Dict[int, int] = {}
            for nid in order:
                best_opt = 0
                best_cost = float('inf')
                for opt in range(len(self.g.vectors[nid])):
                    c = self.g.vectors[nid][opt]
                    for nb in self.g.neighbors.get(nid, set()):
                        if nb in assignment:
                            u, v = (nid, nb) if nid < nb else (nb, nid)
                            mat = self.g.matrices[(u, v)]
                            if nid < nb:
                                c += mat[opt, assignment[nb]]
                            else:
                                c += mat[assignment[nb], opt]
                    if c < best_cost:
                        best_cost = c
                        best_opt = opt
                assignment[nid] = best_opt
            return assignment, full_cost(assignment)

        def improve(assignment: Dict[int, int]) -> Tuple[Dict[int, int], float]:
            improved = True
            while improved:
                improved = False
                for nid in nodes:
                    curr = assignment[nid]
                    curr_marginal = marginal_cost(assignment, nid)
                    for opt in range(len(self.g.vectors[nid])):
                        if opt == curr:
                            continue
                        assignment[nid] = opt
                        new_marginal = marginal_cost(assignment, nid)
                        if new_marginal < curr_marginal - 1e-9:
                            curr_marginal = new_marginal
                            curr = opt
                            improved = True
                        else:
                            assignment[nid] = curr
                    if improved:
                        break
            return assignment, full_cost(assignment)

        best_assign: Dict[int, int] = {}
        best_cost = float('inf')


        order = sorted(nodes, key=lambda nid: len(self.g.neighbors.get(nid, set())), reverse=True)
        assign, _ = greedy(order)
        assign, cost = improve(assign)
        if cost < best_cost:
            best_cost = cost
            best_assign = assign


        for _ in range(20):
            import random
            order = list(nodes)
            random.shuffle(order)
            assign, _ = greedy(order)
            assign, cost = improve(assign)
            if cost < best_cost:
                best_cost = cost
                best_assign = assign

        if best_cost < self.best_cost:
            self.best_cost = best_cost
            self.solution.update(best_assign)

    def _compute_cost(self, assignment: Dict[int, int]) -> float:
        c = self._reduced_cost
        for nid, opt in assignment.items():
            vector = self.g.vectors.get(nid)
            if vector is None:

                continue
            c += vector[opt]
        for (u, v), mat in self.g.matrices.items():
            if u in assignment and v in assignment:
                c += mat[assignment[u], assignment[v]]
        return c
