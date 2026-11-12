# F_IDE_Project/compiler/ir_builder.py
from compiler.ast_nodes import *
from compiler.ir import *
import copy
from core.target_model import TargetModelLoader

class IRBuilder:
    """
    将 AST 转换为包含基本块的控制流图 (CFG)。
    执行循环展开 (Loop Unrolling) 和条件分支逻辑的扁平化。
    """
    def __init__(self, target_model: TargetModelLoader | None = None, defer_target_codes: bool = False):
        self.cfg = None
        self.current_block = None
        # Paper 1：为真时 IR 保持目标机无关，动作编码由 Target Lowering 填入。
        self.defer_target_codes = defer_target_codes
        # Default to 1: without explicit batch size information, unrolling can introduce
        # misleading extra iterations in the IR/CFG visualization.
        self.loop_unroll_factor = 1
        self._material_quantities = {}  # name -> quantity (when declared)
        self._resource_names = set()
        self._material_names = set()
        # 【修改点 1】：引入栈来追踪当前处于哪些 try 块的保护范围内
        # Stack of active try/catch scopes: [(exception_type, catch_entry_block), ...]
        self.active_catch_blocks: list[tuple[str, BasicBlock]] = []
        self._tasks = {}
        self._inline_counter = 0
        self.target_model = target_model

    # def build(self, ast: Program) -> ControlFlowGraph:
        # BasicBlock._id_counter = 0  # 重置块ID
    
    def build(self, ast: Program, entry_task: str = "Main") -> ControlFlowGraph:
        # 【修改点 1】：先重置计数器，再初始化 CFG，确保 Entry 块唯一独占 ID=0
        BasicBlock._id_counter = 0  
        self.cfg = ControlFlowGraph()
        self.current_block = self.cfg.entry_block
        # Cache declared material quantities for foreach unrolling decisions.
        self._material_quantities = {}
        self._resource_names = set()
        self._material_names = set()
        for decl in getattr(ast, "declarations", []) or []:
            if type(decl).__name__ == "MaterialDecl":
                name = getattr(decl, "name", None)
                qty = getattr(decl, "quantity", None)
                if isinstance(name, str) and isinstance(qty, int) and qty > 0:
                    self._material_quantities[name] = qty
                    self._material_names.add(name)
            elif type(decl).__name__ == "ResourceDecl":
                name = getattr(decl, "name", None)
                if isinstance(name, str):
                    self._resource_names.add(name)

        # Expose symbol info for later passes (e.g. SSA needs to avoid versioning resources).
        self.cfg.material_names = set(self._material_names)
        self.cfg.resource_names = set(self._resource_names)
        
        self._tasks = {t.name: t for t in (getattr(ast, "tasks", []) or [])}
        if not self._tasks:
            return self.cfg

        entry = self._tasks.get(entry_task)
        if entry is None:
            entry = (getattr(ast, "tasks", []) or [])[0]

        # Task params that look like due-time inputs are order metadata, not materials.
        self.cfg.order_due_time_param = None
        self.cfg.order_due_time_value = None
        for p in getattr(entry, "params", []) or []:
            if isinstance(p, str) and p:
                if p.lower() in ("due_time", "order_due_time", "due", "deadline"):
                    self.cfg.order_due_time_param = p
                else:
                    self._material_names.add(p)
        self.cfg.material_names = set(self._material_names)
        self.cfg.order_due_time_value = self._find_order_due_time_value(entry.body)

        self._visit_block(entry.body)
            
        return self.cfg

    def _find_order_due_time_value(self, stmts: List):
        due_names = {"due_time", "order_due_time", "due", "deadline"}
        for stmt in stmts or []:
            if type(stmt).__name__ == "IntDecl":
                name = getattr(stmt, "name", "")
                if isinstance(name, str) and name.lower() in due_names:
                    try:
                        return float(getattr(stmt, "value"))
                    except Exception:
                        return None
            if type(stmt).__name__ == "TryCatchStmt":
                found = self._find_order_due_time_value(getattr(stmt, "try_block", []) or [])
                if found is not None:
                    return found
                found = self._find_order_due_time_value(getattr(stmt, "catch_block", []) or [])
                if found is not None:
                    return found
                found = self._find_order_due_time_value(getattr(stmt, "compensate_block", []) or [])
                if found is not None:
                    return found
            elif type(stmt).__name__ == "IfStmt":
                found = self._find_order_due_time_value(getattr(stmt, "true_block", []) or [])
                if found is not None:
                    return found
                found = self._find_order_due_time_value(getattr(stmt, "false_block", []) or [])
                if found is not None:
                    return found
            elif type(stmt).__name__ == "ParallelBlock":
                found = self._find_order_due_time_value(getattr(stmt, "statements", []) or [])
                if found is not None:
                    return found
            elif type(stmt).__name__ == "ForeachStmt":
                found = self._find_order_due_time_value(getattr(stmt, "body", []) or [])
                if found is not None:
                    return found
        return None

    def _visit_block(self, stmts: List):
        for stmt in stmts:
            self._visit_statement(stmt)

    def _annotate_current_block(self, stmt: Statement, desc: str):
        if stmt is None:
            return
        start_line = getattr(stmt, 'start_line', None)
        end_line = getattr(stmt, 'end_line', None)
        self.current_block.add_source_range(start_line, end_line, desc)

    def _visit_statement(self, stmt: Statement):
        if type(stmt).__name__ == "IntDecl":
            return

        if type(stmt).__name__ == "CallStmt":
            self._annotate_current_block(stmt, f"Call {stmt.callee}")
            self._lower_call(stmt)
            return

        if type(stmt).__name__ == "ReturnStmt":
            return

        if isinstance(stmt, Operation):
            self._annotate_current_block(stmt, f"Operation {stmt.op_type}")
            instr, resolved = self._lower_operation_to_instr(stmt)
            self.current_block.add_instr(instr)

            # 【修改点 1】：“物理状态时空错乱” 修复 —— 基本块截断 (Block Splitting)
            # 如果当前指令处于 try 块中，并且它是一个可能触发中断的物理操作
            #
            # 嵌套 try 表示"同一道工序可能以几种方式出错"：外层 try 保护范围更大，
            # 内层 try 保护范围更小。因此对**每一层**活动作用域都要判断是否会触发，
            # 而不是只取最内层——否则外层预定义的处理步骤永远不会被挂上异常边。
            if self.active_catch_blocks and stmt.op_type in ('Move', 'Process', 'Load', 'Inspect', 'Adjust', 'Store'):
                triggered = [
                    (exc_type, catch_target)
                    for exc_type, catch_target in self.active_catch_blocks
                    if self._operation_triggers_exception(exc_type, instr, resolved)
                ]
                if triggered:
                    for _exc_type, catch_target in triggered:
                        # 异常边：发生故障的那一瞬间，精准跳向对应的 Catch 块
                        self.current_block.add_successor(catch_target)
                        instr.catch_edges.append(catch_target)

                    # 强制截断：创建正常执行的后续流基本块 (Fall-through)
                    #
                    # 截断同样会发生在 catch / compensate 块**内部**——处置动作被外层的
                    # 异常边再次截断时就是这种情况。此时新块仍属于异常路径，必须沿用
                    # 所在块的命名前缀，否则后续阶段（残余工作集、寄存器分配、代码生成）
                    # 会把处置动作当成常规工序来执行。
                    next_block = self.cfg.new_block(self._fallthrough_name())
                    self.current_block.add_successor(next_block)
                    self.current_block = next_block

        elif isinstance(stmt, IfStmt):
            self._annotate_current_block(stmt, f"If {stmt.cond_var} == {stmt.cond_val}")
            self._build_if(stmt)
            
        elif isinstance(stmt, ParallelBlock):
            self._annotate_current_block(stmt, "Parallel block")
            # 在中间表示中，并行块内的指令属于同一个基本块
            # 它们没有严格的先后跳跃关系，后续生成的 DAG 将允许乱序(OoO)发射
            for s in stmt.statements:
                self._visit_statement(s)
                
        elif isinstance(stmt, TryCatchStmt):
            self._annotate_current_block(stmt, f"TryCatch {stmt.exception_type}")
            catch_entry = self.cfg.new_block(f"Catch_{stmt.exception_type}")
            
            # 将当前 (exception_type, catch_entry) 入栈，使得 try 块内部的物理指令能找到精准跃迁目标
            self.active_catch_blocks.append((stmt.exception_type, catch_entry))
            self._visit_block(stmt.try_block)
            self.active_catch_blocks.pop()

            # 保存 try 块顺利执行完毕后的退出块
            try_exit_block = self.current_block

            self.current_block = catch_entry
            self._visit_block(stmt.catch_block)
            
            comp_entry = self.cfg.new_block("Compensate")
            # 连接 catch 块到 compensate 块
            self.current_block.add_successor(comp_entry)
            self.current_block = comp_entry
            self._visit_block(stmt.compensate_block)
            
            # 创建 Resume 块，让主路径(无异常)和异常处理路径最终闭环
            resume_block = self.cfg.new_block("Resume")
            try_exit_block.add_successor(resume_block)        # 主路径结束，直接跳到 Resume
            self.current_block.add_successor(resume_block)  # 补偿结束后也跳到 Resume
            
            self.current_block = resume_block

        elif type(stmt).__name__ == 'ForeachStmt': 
            self._annotate_current_block(stmt, f"Foreach {stmt.item_var} in {stmt.collection}")
            # 假定 AST 支持 ForeachStmt：在工厂计划阶段，批量通常是已知的
            # 编译器通过“循环展开”消除跳转开销，挖掘最大的指令级并行度(ILP)
            self._unroll_loop(stmt)

    def _fallthrough_name(self) -> str:
        """截断产生的新基本块的名字：继承所在块的异常路径属性。"""
        name = getattr(self.current_block, "name", "") or ""
        for prefix in ("Catch_", "Compensate"):
            if name.startswith(prefix):
                return f"{name}_Fallthrough"
        return "Normal_Fallthrough"

    def _build_if(self, stmt: IfStmt):
        true_block = self.cfg.new_block("If_True")
        false_block = self.cfg.new_block("If_False")
        end_block = self.cfg.new_block("If_End")

        # 插入分支
        branch = BranchInstr(stmt.cond_var, true_block, false_block)
        self.current_block.add_instr(branch)
        self.current_block.add_successor(true_block)
        self.current_block.add_successor(false_block)

        # 构建 True 路径
        self.current_block = true_block
        self._visit_block(stmt.true_block)
        self.current_block.add_instr(JumpInstr(end_block))
        self.current_block.add_successor(end_block)

        # 构建 False 路径
        self.current_block = false_block
        self._visit_block(stmt.false_block)
        self.current_block.add_instr(JumpInstr(end_block))
        self.current_block.add_successor(end_block)

        self.current_block = end_block

    # def _unroll_loop(self, stmt: Any):
        # 将工厂批处理循环在编译期展开（此处为逻辑示意）
        # for _ in range(self.loop_unroll_factor):
            # self._visit_block(stmt.body)

    def _unroll_loop(self, stmt: 'ForeachStmt'):
        # 将工厂批处理循环在编译期展开
        # 每次展开必须使用不同的 item 符号，避免重复对同一个“item”执行相同工艺。
        def substitute(node, old: str, new: str):
            if node is None:
                return

            if isinstance(node, Operation):
                node.args = [new if a == old else a for a in (node.args or [])]
                if node.target == old:
                    node.target = new
                return

            if isinstance(node, IfStmt):
                if node.cond_var == old:
                    node.cond_var = new
                for s in node.true_block or []:
                    substitute(s, old, new)
                for s in node.false_block or []:
                    substitute(s, old, new)
                return

            if isinstance(node, ParallelBlock):
                for s in node.statements or []:
                    substitute(s, old, new)
                return

            if isinstance(node, TryCatchStmt):
                for s in node.try_block or []:
                    substitute(s, old, new)
                for s in node.catch_block or []:
                    substitute(s, old, new)
                for s in node.compensate_block or []:
                    substitute(s, old, new)
                return

            if isinstance(node, ForeachStmt):
                # 避免捕获嵌套 foreach 的迭代变量
                if node.item_var != old:
                    for s in node.body or []:
                        substitute(s, old, new)
                return

        original_item = stmt.item_var
        factor = self._material_quantities.get(stmt.collection, self.loop_unroll_factor)
        try:
            factor = int(factor)
        except Exception:
            factor = self.loop_unroll_factor
        if factor <= 0:
            factor = 1

        for i in range(factor):
            iter_item = f"{original_item}_{i}"
            iter_body = copy.deepcopy(stmt.body or [])
            for s in iter_body:
                substitute(s, original_item, iter_item)
            self._visit_block(iter_body)

    def _lower_call(self, stmt: 'CallStmt'):
        callee = getattr(stmt, "callee", None)
        if not isinstance(callee, str) or not callee:
            return
        task = self._tasks.get(callee)
        if task is None:
            return

        inlined_stmts, returned = self._inline_task(task, list(getattr(stmt, "args", []) or []))
        for s in inlined_stmts:
            self._visit_statement(s)

        target = getattr(stmt, "target", None)
        if isinstance(target, str) and target and isinstance(returned, str) and returned:
            self.current_block.add_instr(OpInstr("Assign", target, [returned]))

    def _inline_task(self, task: 'TaskDef', actual_args: List[str]):
        call_id = self._inline_counter
        self._inline_counter += 1
        prefix = f"__{task.name}_{call_id}_"

        params = list(getattr(task, "params", []) or [])
        param_map = {p: a for p, a in zip(params, actual_args)}

        local_defs = set()

        def scan(node):
            if node is None:
                return
            tname = type(node).__name__
            if tname == "Operation":
                tgt = getattr(node, "target", None)
                if isinstance(tgt, str) and tgt:
                    local_defs.add(tgt)
                return
            if tname == "IntDecl":
                name = getattr(node, "name", None)
                if isinstance(name, str) and name:
                    local_defs.add(name)
                return
            if tname == "CallStmt":
                tgt = getattr(node, "target", None)
                if isinstance(tgt, str) and tgt:
                    local_defs.add(tgt)
                return
            if tname == "ReturnStmt":
                return
            if tname == "ForeachStmt":
                iv = getattr(node, "item_var", None)
                if isinstance(iv, str) and iv:
                    local_defs.add(iv)
                for s in getattr(node, "body", []) or []:
                    scan(s)
                return
            if tname == "IfStmt":
                for s in getattr(node, "true_block", []) or []:
                    scan(s)
                for s in getattr(node, "false_block", []) or []:
                    scan(s)
                return
            if tname == "TryCatchStmt":
                for s in getattr(node, "try_block", []) or []:
                    scan(s)
                for s in getattr(node, "catch_block", []) or []:
                    scan(s)
                for s in getattr(node, "compensate_block", []) or []:
                    scan(s)
                return
            if tname == "ParallelBlock":
                for s in getattr(node, "statements", []) or []:
                    scan(s)
                return

        for s in getattr(task, "body", []) or []:
            scan(s)

        local_defs.difference_update(param_map.keys())

        def map_name(name: str) -> str:
            if not isinstance(name, str) or not name:
                return name
            if name in param_map:
                return param_map[name]
            if name in local_defs:
                return f"{prefix}{name}"
            return name

        def rewrite(node):
            if node is None:
                return None
            tname = type(node).__name__
            if tname == "Operation":
                node.args = [map_name(a) for a in (getattr(node, "args", []) or [])]
                tgt = getattr(node, "target", None)
                if isinstance(tgt, str) and tgt:
                    node.target = map_name(tgt)
                return node
            if tname == "IntDecl":
                node.name = map_name(getattr(node, "name", ""))
                return node
            if tname == "CallStmt":
                node.args = [map_name(a) for a in (getattr(node, "args", []) or [])]
                tgt = getattr(node, "target", None)
                if isinstance(tgt, str) and tgt:
                    node.target = map_name(tgt)
                return node
            if tname == "ReturnStmt":
                node.value = map_name(getattr(node, "value", ""))
                return node
            if tname == "IfStmt":
                node.cond_var = map_name(getattr(node, "cond_var", ""))
                for s in getattr(node, "true_block", []) or []:
                    rewrite(s)
                for s in getattr(node, "false_block", []) or []:
                    rewrite(s)
                return node
            if tname == "TryCatchStmt":
                for s in getattr(node, "try_block", []) or []:
                    rewrite(s)
                for s in getattr(node, "catch_block", []) or []:
                    rewrite(s)
                for s in getattr(node, "compensate_block", []) or []:
                    rewrite(s)
                return node
            if tname == "ParallelBlock":
                for s in getattr(node, "statements", []) or []:
                    rewrite(s)
                return node
            if tname == "ForeachStmt":
                node.item_var = map_name(getattr(node, "item_var", ""))
                node.collection = map_name(getattr(node, "collection", ""))
                for s in getattr(node, "body", []) or []:
                    rewrite(s)
                return node
            return node

        body_copy = copy.deepcopy(getattr(task, "body", []) or [])
        returned = None
        inlined = []
        for s in body_copy:
            s = rewrite(s)
            if type(s).__name__ == "ReturnStmt":
                returned = getattr(s, "value", None)
                break
            inlined.append(s)
        return inlined, returned

    def _lower_operation_to_instr(self, stmt: Operation) -> tuple:
        """
        Attach target-model metadata (opcode/func/rp/expected_time) onto OpInstr.
        RP is expressed in FaLang source by its *description* string in param_str.

        返回 (instr, resolved)，其中 resolved = (opcode, func, rp_code, expected_time)。
        当 defer_target_codes 为真时，动作编码不写入指令，IR 保持目标机无关，
        由 compiler/target_lowering.py 在 Factory Target Machine Model 上填入；
        resolved 仅用于判断该动作是否会触发异常。
        """
        opcode = None
        func = None
        rp_code = None
        expected_time = None

        tm = self.target_model
        if tm is not None:
            inst = tm.optype_to_instruction(getattr(stmt, "op_type", ""))
            if inst is not None:
                opcode, func = inst.opcode, inst.func
                rp_code = tm.resolve_rp_code(opcode, func, getattr(stmt, "param_str", None))
                if rp_code:
                    expected_time = tm.get_expected_time(opcode, func, rp_code)

        if self.defer_target_codes:
            instr = OpInstr(stmt.op_type, stmt.target, stmt.args, stmt.param_str)
        else:
            instr = OpInstr(
                stmt.op_type,
                stmt.target,
                stmt.args,
                stmt.param_str,
                opcode=opcode,
                func=func,
                rp=rp_code,
                expected_time=expected_time,
            )
        return instr, (opcode, func, rp_code, expected_time)

    def _operation_triggers_exception(self, exception_type: str, instr: OpInstr, resolved=None) -> bool:
        tm = self.target_model
        if tm is None:
            # Backwards compatible fallback: previous behavior assumed all such ops may fault in try.
            return True
        if not exception_type:
            return False
        opcode, func, rp_code, _ = resolved if resolved else (None, None, None, None)
        if not opcode or not func or not rp_code:
            opcode = opcode or getattr(instr, "opcode", None)
            func = func or getattr(instr, "func", None)
            rp_code = rp_code or getattr(instr, "rp", None)
        if not opcode or not func or not rp_code:
            # If we cannot resolve rp, be conservative and keep old split behavior.
            return True
        return tm.triggers_exception(exception_type, opcode, func, rp_code)
