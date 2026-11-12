import re
from typing import Dict, List, Optional, Set

from compiler.ir import ControlFlowGraph, BasicBlock, OpInstr, PhiInstr, BranchInstr, JumpInstr
from compiler.mdg import ManufacturingDependencyGraph, MDGNode, MDGEdge
from core.target_model import TargetModelLoader


class MDGBuilder:
    """Build material, control, exception, and structural dependencies from the SSA CFG."""

    def __init__(self, target_model: Optional[TargetModelLoader] = None):
        self.target_model = target_model
        self.mdg = ManufacturingDependencyGraph()
        # Phi elimination can define the merged SSA name in multiple predecessor blocks.


        self._def_table: Dict[str, List[MDGNode]] = {}
        self._resource_names: set = set()

    def build(self, cfg: ControlFlowGraph) -> ManufacturingDependencyGraph:
        MDGNode._id_counter = 0
        self.mdg = ManufacturingDependencyGraph()
        self._def_table = {}
        self._resource_names = set(getattr(cfg, "resource_names", set()) or set())


        self._create_nodes_and_sequence_edges(cfg)


        self._build_def_table(cfg)


        self._add_dependency_edges(cfg)


        self._inject_resource_competition_setup_edges(cfg)

        return self.mdg

    @staticmethod
    def _is_versioned_var(name: str, resource_names: set) -> bool:

        if not isinstance(name, str) or not name:
            return False
        if name.isdigit() or name.startswith('"'):
            return False
        if name in resource_names:
            return False
        parts = name.rsplit('_', 1)
        return len(parts) == 2 and parts[1].isdigit()

    @staticmethod
    def _parse_weight_for_node(node: MDGNode, target_model: Optional[TargetModelLoader]) -> float:
        """Weight the outgoing dependency using the source operation and target-model timings."""
        if target_model is None or node.instr is None:
            return 0.0

        instr = node.instr
        if isinstance(instr, PhiInstr):
            return 0.0

        if isinstance(instr, OpInstr):
            op_type = instr.op_type


            if op_type in ("Process", "Inspect", "Adjust"):
                if instr.opcode and instr.func and instr.rp:
                    t = target_model.get_expected_time(instr.opcode, instr.func, instr.rp)
                    if t is not None:
                        return float(t)
                return 0.0


            if op_type in ("Move", "Assign"):
                return target_model.get_default_move_time()


            if op_type == "Load":
                return target_model.get_default_load_time()


            if op_type == "Store":
                return target_model.get_default_store_time()


            return 0.0

        return 0.0

    @staticmethod
    def _parse_weight_for_edge(
        src: MDGNode,
        dst: MDGNode,
        target_model: Optional[TargetModelLoader],
    ) -> float:
        weight = MDGBuilder._parse_weight_for_node(src, target_model)
        if (
            target_model is not None
            and isinstance(getattr(src, "instr", None), OpInstr)
            and isinstance(getattr(dst, "instr", None), OpInstr)
        ):
            weight += target_model.get_setup_time(
                getattr(src.instr, "params", None),
                getattr(dst.instr, "params", None),
            )
        return weight

    def _create_nodes_and_sequence_edges(self, cfg: ControlFlowGraph):
        for block in cfg.blocks:
            entry_node = MDGNode(None, block, "entry")
            exit_node = MDGNode(None, block, "exit")
            self.mdg.add_node(entry_node)
            self.mdg.add_node(exit_node)
            self.mdg.block_entry[block.id] = entry_node
            self.mdg.block_exit[block.id] = exit_node

            for instr in block.instructions:
                node_type = "phi" if isinstance(instr, PhiInstr) else "op"
                node = MDGNode(instr, block, node_type)
                self.mdg.add_node(node)
                self.mdg.instr_to_node[id(instr)] = node


            if block.instructions:
                first_node = self.mdg.get_node_for_instr(block.instructions[0])
                if first_node is not None:
                    self.mdg.add_edge(MDGEdge(entry_node, first_node, 0.0, "struct"))
                last_node = self.mdg.get_node_for_instr(block.instructions[-1])
                if last_node is not None:
                    self.mdg.add_edge(MDGEdge(last_node, exit_node, 0.0, "struct"))
            else:
                # Empty blocks retain a zero-time entry-to-exit edge for CFG continuity.

                self.mdg.add_edge(MDGEdge(entry_node, exit_node, 0.0, "struct"))

    def _build_def_table(self, cfg: ControlFlowGraph):

        def add_definition(name: str, node: MDGNode):
            if not name:
                return
            definitions = self._def_table.setdefault(name, [])
            if node not in definitions:
                definitions.append(node)

        for block in cfg.blocks:
            for instr in block.instructions:
                node = self.mdg.get_node_for_instr(instr)
                if node is None:
                    continue

                if isinstance(instr, PhiInstr):
                    if instr.target:
                        add_definition(instr.target, node)

                elif isinstance(instr, OpInstr):

                    if instr.target:
                        add_definition(instr.target, node)

                    for it in (instr.implicit_targets or []):
                        add_definition(it, node)

    def _definition_nodes(self, name: str) -> List[MDGNode]:
        """Return every path-specific definition of an SSA name."""
        return list(self._def_table.get(name, []))

    def _add_dependency_edges(self, cfg: ControlFlowGraph):
        for block in cfg.blocks:
            for instr in block.instructions:
                node = self.mdg.get_node_for_instr(instr)
                if node is None:
                    continue

                if isinstance(instr, PhiInstr):

                    for pred_block, var_version in (instr.choices or {}).items():
                        for def_node in self._definition_nodes(var_version):
                            weight = self._parse_weight_for_edge(def_node, node, self.target_model)
                            self.mdg.add_edge(MDGEdge(def_node, node, weight, "phi"))

                elif isinstance(instr, OpInstr):

                    for arg in (instr.args or []):
                        if self._is_versioned_var(arg, self._resource_names):
                            for def_node in self._definition_nodes(arg):
                                if def_node == node:
                                    continue
                                weight = self._parse_weight_for_edge(def_node, node, self.target_model)
                                self.mdg.add_edge(MDGEdge(def_node, node, weight, "data"))


                    for catch_block in (instr.catch_edges or []):
                        catch_entry = self.mdg.block_entry.get(catch_block.id)
                        if catch_entry is not None:
                            self.mdg.add_edge(MDGEdge(node, catch_entry, 0.0, "exc"))

                elif isinstance(instr, BranchInstr):
                    if self._is_versioned_var(instr.cond_var, self._resource_names):
                        for def_node in self._definition_nodes(instr.cond_var):
                            if def_node == node:
                                continue
                            weight = self._parse_weight_for_edge(def_node, node, self.target_model)
                            self.mdg.add_edge(MDGEdge(def_node, node, weight, "data"))


        for block in cfg.blocks:
            exit_node = self.mdg.block_exit.get(block.id)
            if exit_node is None:
                continue
            for succ in block.successors:
                succ_entry = self.mdg.block_entry.get(succ.id)
                if succ_entry is not None:
                    self.mdg.add_edge(MDGEdge(exit_node, succ_entry, 0.0, "control"))


        for block in cfg.blocks:
            entry_node = self.mdg.block_entry.get(block.id)
            if entry_node is None:
                continue
            if block.name != "Entry" and not entry_node.in_edges:
                self.mdg.unreachable_blocks.add(block.id)


    @staticmethod
    def _get_device_name_from_node(node: MDGNode, resource_names: Set[str]) -> Optional[str]:

        if not isinstance(node.instr, OpInstr):
            return None
        instr = node.instr
        if instr.op_type in ("Process", "Inspect", "Adjust"):
            if instr.args:
                dev = instr.args[0]
                if dev in resource_names:
                    return dev
        return None

    @staticmethod
    def _compute_setup_between_ops(
        node_a: MDGNode,
        node_b: MDGNode,
        target_model: Optional[TargetModelLoader],
    ) -> float:
        """Return the setup time for switching models of the same component."""
        if target_model is None:
            return 0.0
        instr_a = node_a.instr
        instr_b = node_b.instr
        if not isinstance(instr_a, OpInstr) or not isinstance(instr_b, OpInstr):
            return 0.0

        params_a = getattr(instr_a, "params", None)
        params_b = getattr(instr_b, "params", None)
        if not params_a or not params_b:
            return 0.0


        setup_ab = target_model.get_setup_time(params_a, params_b)
        setup_ba = target_model.get_setup_time(params_b, params_a)
        return max(setup_ab, setup_ba)

    def _inject_resource_competition_setup_edges(self, cfg: ControlFlowGraph):
        """Add bidirectional setup edges between operations using the same virtual device."""
        from collections import defaultdict

        if self.target_model is None:
            return


        device_ops: Dict[str, List[MDGNode]] = defaultdict(list)
        for node in self.mdg.nodes:
            if node.node_type != "op":
                continue
            dev = self._get_device_name_from_node(node, self._resource_names)
            if dev:
                device_ops[dev].append(node)


        injected_count = 0
        for dev, nodes in device_ops.items():
            if len(nodes) < 2:
                continue
            for i, node_a in enumerate(nodes):
                for j in range(i + 1, len(nodes)):
                    node_b = nodes[j]
                    setup = self._compute_setup_between_ops(node_a, node_b, self.target_model)
                    if setup > 0:
                        # Both directions represent a shared-device setup relation.
                        self.mdg.add_edge(MDGEdge(node_a, node_b, setup, "setup"))
                        self.mdg.add_edge(MDGEdge(node_b, node_a, setup, "setup"))
                        injected_count += 1

        if injected_count > 0:

            pass  
