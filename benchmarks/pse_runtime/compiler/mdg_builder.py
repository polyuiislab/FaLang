# F_IDE_Project/compiler/mdg_builder.py
import re
from typing import Dict, List, Optional, Set

from compiler.ir import ControlFlowGraph, BasicBlock, OpInstr, PhiInstr, BranchInstr, JumpInstr
from compiler.mdg import ManufacturingDependencyGraph, MDGNode, MDGEdge
from core.target_model import TargetModelLoader


class MDGBuilder:
    """
    从 SSA CFG 构建 Manufacturing Dependency Graph（制造依赖图）。

    构建方式：
    1. 为每个基本块创建 entry/exit 节点，为每条指令创建节点。
    2. 在基本块内建立顺序边（entry -> instr0 -> ... -> exit）。
    3. 建立定义字典 {版本化变量名 -> 定义节点}。
    4. 再次遍历所有指令，为每个输入参数查找定义节点并建立数据依赖边。
    5. 为 Phi 指令建立来自不同前驱块的 phi 依赖边。
    6. 为基本块间建立控制依赖边（exit -> successor entry）。
    7. 根据工厂目标机模型为边赋权重。
    """

    def __init__(self, target_model: Optional[TargetModelLoader] = None):
        self.target_model = target_model
        self.mdg = ManufacturingDependencyGraph()
        # Phi elimination may create one path-specific Assign definition for the
        # same merged SSA name in each predecessor block.  Keep every definition;
        # a single-node table would let the last scanned predecessor overwrite the
        # others and leave valid path copies without a consumer edge in the MDG.
        self._def_table: Dict[str, List[MDGNode]] = {}
        self._resource_names: set = set()

    def build(self, cfg: ControlFlowGraph) -> ManufacturingDependencyGraph:
        MDGNode._id_counter = 0
        self.mdg = ManufacturingDependencyGraph()
        self._def_table = {}
        self._resource_names = set(getattr(cfg, "resource_names", set()) or set())

        # 阶段 1：创建节点 + 块内顺序边
        self._create_nodes_and_sequence_edges(cfg)

        # 阶段 2：建立定义字典
        self._build_def_table(cfg)

        # 阶段 3：添加数据依赖边、phi 边、控制边，并赋权重
        self._add_dependency_edges(cfg)

        # 阶段 4：注入资源竞争setup边（state-aware JIT）
        self._inject_resource_competition_setup_edges(cfg)

        return self.mdg

    @staticmethod
    def _is_versioned_var(name: str, resource_names: set) -> bool:
        """判断一个名字是否是 SSA 版本化变量（如 part_0, x_2）。"""
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
        """根据目标机模型和源节点指令类型计算边权重。"""
        if target_model is None or node.instr is None:
            return 0.0

        instr = node.instr
        if isinstance(instr, PhiInstr):
            return 0.0

        if isinstance(instr, OpInstr):
            op_type = instr.op_type

            # R-type 工艺指令（Process / Inspect / Adjust）
            if op_type in ("Process", "Inspect", "Adjust"):
                if instr.opcode and instr.func and instr.rp:
                    t = target_model.get_expected_time(instr.opcode, instr.func, instr.rp)
                    if t is not None:
                        return float(t)
                return 0.0

            # MOVE：同一缓存级别内物流
            if op_type in ("Move", "Assign"):
                return target_model.get_default_move_time()

            # I-type Load：从不同缓存级别加载
            if op_type == "Load":
                return target_model.get_default_load_time()

            # I-type Store：存放到不同缓存级别
            if op_type == "Store":
                return target_model.get_default_store_time()

            # IsAvailable / Assign 等无物理执行时间
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

            # 结构边：entry -> 第一条指令，最后一条指令 -> exit
            if block.instructions:
                first_node = self.mdg.get_node_for_instr(block.instructions[0])
                if first_node is not None:
                    self.mdg.add_edge(MDGEdge(entry_node, first_node, 0.0, "struct"))
                last_node = self.mdg.get_node_for_instr(block.instructions[-1])
                if last_node is not None:
                    self.mdg.add_edge(MDGEdge(last_node, exit_node, 0.0, "struct"))
            else:
                # 空基本块仍然承载 CFG 控制流。显式连接 entry/exit，避免汇合块或
                # Resume 块在 MDG 中断链，并保持该结构边为零执行开销。
                self.mdg.add_edge(MDGEdge(entry_node, exit_node, 0.0, "struct"))

    def _build_def_table(self, cfg: ControlFlowGraph):
        """遍历所有节点，建立 版本化变量 -> 所有定义节点 的映射。"""
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
                    # 显式 target
                    if instr.target:
                        add_definition(instr.target, node)
                    # 隐式 target（SSA 物理状态变异）
                    for it in (instr.implicit_targets or []):
                        add_definition(it, node)

    def _definition_nodes(self, name: str) -> List[MDGNode]:
        """返回一个 SSA 名称的全部路径定义节点。"""
        return list(self._def_table.get(name, []))

    def _add_dependency_edges(self, cfg: ControlFlowGraph):
        for block in cfg.blocks:
            for instr in block.instructions:
                node = self.mdg.get_node_for_instr(instr)
                if node is None:
                    continue

                if isinstance(instr, PhiInstr):
                    # Phi 依赖边：来自各个前驱块中对应版本的定义节点
                    for pred_block, var_version in (instr.choices or {}).items():
                        for def_node in self._definition_nodes(var_version):
                            weight = self._parse_weight_for_edge(def_node, node, self.target_model)
                            self.mdg.add_edge(MDGEdge(def_node, node, weight, "phi"))

                elif isinstance(instr, OpInstr):
                    # 数据依赖：遍历 args
                    for arg in (instr.args or []):
                        if self._is_versioned_var(arg, self._resource_names):
                            for def_node in self._definition_nodes(arg):
                                if def_node == node:
                                    continue
                                weight = self._parse_weight_for_edge(def_node, node, self.target_model)
                                self.mdg.add_edge(MDGEdge(def_node, node, weight, "data"))

                    # 异常控制边：从可能触发异常的指令直接连向 catch 块 entry
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

        # 基本块间控制依赖边：exit -> 后继 entry
        for block in cfg.blocks:
            exit_node = self.mdg.block_exit.get(block.id)
            if exit_node is None:
                continue
            for succ in block.successors:
                succ_entry = self.mdg.block_entry.get(succ.id)
                if succ_entry is not None:
                    self.mdg.add_edge(MDGEdge(exit_node, succ_entry, 0.0, "control"))

        # 死代码检测：entry 没有入边且不是 Entry 块的标记为不可达
        for block in cfg.blocks:
            entry_node = self.mdg.block_entry.get(block.id)
            if entry_node is None:
                continue
            if block.name != "Entry" and not entry_node.in_edges:
                self.mdg.unreachable_blocks.add(block.id)

    # ------------------------------------------------------------------
    # 阶段 4：资源竞争 setup 边注入（state-aware JIT compilation）
    # ------------------------------------------------------------------
    @staticmethod
    def _get_device_name_from_node(node: MDGNode, resource_names: Set[str]) -> Optional[str]:
        """从 MDGNode 提取其使用的虚拟设备名（Process/Inspect/Adjust 的第一个参数）。"""
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
        """计算两个operation之间的setup time。若它们处理同一component的不同model，返回切换cost。"""
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

        # 尝试两个方向的setup time（取非零值）
        setup_ab = target_model.get_setup_time(params_a, params_b)
        setup_ba = target_model.get_setup_time(params_b, params_a)
        return max(setup_ab, setup_ba)

    def _inject_resource_competition_setup_edges(self, cfg: ControlFlowGraph):
        """
        对所有使用同一虚拟device的operation节点对，添加setup类型的双向边。

        这些边建模了同一设备处理不同型号组件时的切换开销。
        调度器在结构冒险时可以通过这些边的权重来确定最优的发射顺序；
        寄存器分配器可以通过这些边将同型号组件分配到同一工位。
        """
        from collections import defaultdict

        if self.target_model is None:
            return

        # 按device分组operations
        device_ops: Dict[str, List[MDGNode]] = defaultdict(list)
        for node in self.mdg.nodes:
            if node.node_type != "op":
                continue
            dev = self._get_device_name_from_node(node, self._resource_names)
            if dev:
                device_ops[dev].append(node)

        # 对每组内的所有operation对添加setup边
        injected_count = 0
        for dev, nodes in device_ops.items():
            if len(nodes) < 2:
                continue
            for i, node_a in enumerate(nodes):
                for j in range(i + 1, len(nodes)):
                    node_b = nodes[j]
                    setup = self._compute_setup_between_ops(node_a, node_b, self.target_model)
                    if setup > 0:
                        # 双向边建模无向的资源竞争setup关系
                        self.mdg.add_edge(MDGEdge(node_a, node_b, setup, "setup"))
                        self.mdg.add_edge(MDGEdge(node_b, node_a, setup, "setup"))
                        injected_count += 1

        if injected_count > 0:
            # 当存在跨订单setup边时，需要重新计算关键路径权重
            pass  # CP权重在scheduler初始化时计算，无需在此重复
