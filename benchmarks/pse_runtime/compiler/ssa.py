# F_IDE_Project/compiler/ssa.py
from compiler.ir import *
import copy
from core.target_model import TargetModelLoader

class SSAPass:
    """
    将 CFG 转换为静态单赋值 (SSA) 形式。
    通过拓扑排序确保环境就绪，在汇聚点插入 Phi 节点合并隐式变异的物理状态。
    """
    def __init__(self, target_model: TargetModelLoader | None = None):
        self.var_counters = {}  
        self.block_envs = {}    
        self.target_model = target_model

    def convert(self, cfg: ControlFlowGraph, target_model: TargetModelLoader | None = None) -> ControlFlowGraph:
        if target_model is not None:
            self.target_model = target_model
        # Resources (devices/buffers/storage) are persistent assets and must NOT be versioned.
        # Materials (and scalar variables like ints) can be versioned.
        resource_names = set(getattr(cfg, "resource_names", set()) or set())

        def should_version(name: str) -> bool:
            if not isinstance(name, str) or not name:
                return False
            if name.isdigit() or name.startswith('"'):
                return False
            return name not in resource_names

        def material_arg_indices(op_type: str, argc: int):
            if op_type == "Process":
                return range(1, argc)  # args[0] is device
            if op_type == "Inspect":
                return [1] if argc >= 2 else []
            if op_type in ("Move", "Load", "Store"):
                return [0] if argc >= 1 else []
            if op_type == "Assign":
                return [0] if argc >= 1 else []
            return []

        def is_physical_operation(instr: OpInstr) -> bool:
            tm = self.target_model
            if tm is None:
                return instr.op_type in ("Move", "Process", "Load", "Inspect", "Adjust", "Store")
            if not instr.opcode or not instr.func or not instr.rp:
                return False
            return tm.is_rp_supported(instr.opcode, instr.func, instr.rp)

        # 【修改点 2】：放弃 DFS，改用拓扑排序 (Topological Sort) 遍历 CFG
        # 确保在处理汇聚节点（如 If_End）之前，它的所有前驱节点的环境已经被完全计算
        in_degree = {block: 0 for block in cfg.blocks}
        for block in cfg.blocks:
            for succ in block.successors:
                in_degree[succ] += 1
                
        # 初始化工作队列
        worklist = [block for block in cfg.blocks if in_degree[block] == 0]
        self.block_envs[cfg.entry_block] = {}
        
        while worklist:
            block = worklist.pop(0)
            
            # --- 1. 汇聚前驱环境 ---
            current_env = {}
            if len(block.predecessors) == 1:
                pred = block.predecessors[0]
                if pred in self.block_envs:
                    current_env = copy.deepcopy(self.block_envs[pred])
                    
            elif len(block.predecessors) > 1:
                # 收集所有前驱分支传过来的变量
                all_vars = set()
                for pred in block.predecessors:
                    if pred in self.block_envs:
                        all_vars.update(self.block_envs[pred].keys())
                
                # sorted 保证每次编译生成的 Phi 节点顺序一致
                for var in sorted(all_vars):
                    versions = {}
                    for pred in block.predecessors:
                        if pred in self.block_envs and var in self.block_envs[pred]:
                            versions[pred] = f"{var}_{self.block_envs[pred][var]}"
                    
                    # 【修改点 2】：Phi 节点生成
                    # 如果不同分支流过来的变量版本不同，产生 Phi 节点合并
                    if len(set(versions.values())) > 1:
                        new_version = self._get_new_version(var)
                        current_env[var] = new_version
                        phi = PhiInstr(f"{var}_{new_version}", versions)
                        block.instructions.insert(0, phi)
                    elif len(versions) > 0:
                        # 状态无歧义，沿用已知的版本
                        version_str = list(versions.values())[0]
                        current_env[var] = int(version_str.split('_')[-1])

            # --- 2. 块内指令变量版本更名 ---
            for instr in block.instructions:
                if isinstance(instr, PhiInstr):
                    continue
                    
                if isinstance(instr, OpInstr):
                    original_args = instr.args.copy()

                    # 读取操作 (Source)
                    new_args = []
                    for arg in instr.args:
                        if should_version(arg):
                            if arg in current_env:
                                new_args.append(f"{arg}_{current_env[arg]}")
                            else:
                                current_env[arg] = 0
                                new_args.append(f"{arg}_0")
                        else:
                            new_args.append(arg)
                    instr.args = new_args
                    
                    # 写入操作 (Target)
                    if instr.target:
                        if should_version(instr.target):
                            new_version = self._get_new_version(instr.target)
                            current_env[instr.target] = new_version
                            instr.target = f"{instr.target}_{new_version}"

                    # 【隐式物理状态变异追踪】：无赋值符但物理上改变了物料状态
                    if isinstance(instr, OpInstr) and (is_physical_operation(instr) or instr.op_type == "Assign"):
                        idxs = set(material_arg_indices(instr.op_type, len(original_args)))
                        implicit_defs = []
                        for i, orig_arg in enumerate(original_args):
                            if i not in idxs:
                                continue
                            if should_version(orig_arg):
                                new_version = self._get_new_version(orig_arg)
                                current_env[orig_arg] = new_version
                                implicit_defs.append(f"{orig_arg}_{new_version}")
                        if implicit_defs:
                            instr.implicit_targets = implicit_defs
                                
                elif isinstance(instr, BranchInstr):
                    if should_version(instr.cond_var):
                        if instr.cond_var in current_env:
                            instr.cond_var = f"{instr.cond_var}_{current_env[instr.cond_var]}"
                        else:
                            current_env[instr.cond_var] = 0
                            instr.cond_var = f"{instr.cond_var}_0"

            self.block_envs[block] = current_env
            
            # 减去后继节点的入度，如果该块所有前驱都已计算完毕，即可处理
            for succ in block.successors:
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    worklist.append(succ)
                    
        self._simplify_phis(cfg)
        return cfg

    def _get_new_version(self, var_name: str) -> int:
        if var_name not in self.var_counters:
            self.var_counters[var_name] = 0
        else:
            self.var_counters[var_name] += 1
        return self.var_counters[var_name]

    def _simplify_phis(self, cfg: ControlFlowGraph) -> None:
        """
        Phi-node simplification:
        - If all incoming versions are the same, replace phi target with that single version.
        - If all incoming versions are the same except for self-references, replace with the single non-self version.
        """

        def resolve(name: str, repl: dict) -> str:
            while name in repl and repl[name] != name:
                name = repl[name]
            return name

        changed = True
        replacements = {}
        while changed:
            changed = False

            # 1) Find simplifiable phi nodes and record replacements.
            for block in cfg.blocks:
                new_instrs = []
                for instr in block.instructions:
                    if not isinstance(instr, PhiInstr):
                        new_instrs.append(instr)
                        continue

                    target = resolve(instr.target, replacements)
                    incoming = [resolve(v, replacements) for v in (instr.choices or {}).values()]
                    if not incoming:
                        new_instrs.append(instr)
                        continue

                    unique_all = set(incoming)
                    if len(unique_all) == 1:
                        replacements[target] = next(iter(unique_all))
                        changed = True
                        continue

                    unique_non_self = {v for v in unique_all if v != target}
                    if len(unique_non_self) == 1 and unique_all.issubset({target, next(iter(unique_non_self))}):
                        replacements[target] = next(iter(unique_non_self))
                        changed = True
                        continue

                    # Update in-place with resolved names to help future iterations.
                    instr.target = target
                    instr.choices = {k: resolve(v, replacements) for k, v in (instr.choices or {}).items()}
                    new_instrs.append(instr)

                block.instructions = new_instrs

            if not changed:
                break

            # 2) Apply replacements across the CFG (args, targets, branch conds, and phi choices).
            for block in cfg.blocks:
                for instr in block.instructions:
                    if isinstance(instr, OpInstr):
                        instr.args = [resolve(a, replacements) for a in (instr.args or [])]
                        if instr.target:
                            instr.target = resolve(instr.target, replacements)
                    elif isinstance(instr, BranchInstr):
                        instr.cond_var = resolve(instr.cond_var, replacements)
                    elif isinstance(instr, PhiInstr):
                        instr.target = resolve(instr.target, replacements)
                        instr.choices = {k: resolve(v, replacements) for k, v in (instr.choices or {}).items()}
