# F_IDE_Project/core/compiler/semantic.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from .ast_nodes import (
    ASTNode,
    CallStmt,
    ForeachStmt,
    IfStmt,
    IntDecl,
    MaterialDecl,
    Operation,
    ParallelBlock,
    Program,
    ResourceDecl,
    ReturnStmt,
    Statement,
    TaskDef,
    TryCatchStmt,
)
from .errors import SemanticError
from core.target_model import TargetModelLoader


@dataclass(frozen=True)
class TaskSignature:
    name: str
    param_count: int
    return_count: int  # 0 = void, 1 = has return (value), >1 invalid

    @property
    def has_return(self) -> bool:
        return self.return_count == 1


class SymbolTable:
    def __init__(self):
        self.resources: Dict[str, dict] = {}
        self.materials: Set[str] = set()
        self.tasks: Dict[str, TaskDef] = {}
        self.task_sigs: Dict[str, TaskSignature] = {}

    def define_resource(self, name: str, category: str, capabilities: List[str]):
        if name in self.resources:
            raise SemanticError(f"Duplicate resource definition: {name}")
        self.resources[name] = {"cat": category, "caps": capabilities}

    def define_material(self, name: str):
        self.materials.add(name)

    def define_task(self, task: TaskDef):
        if task.name in self.tasks:
            raise SemanticError(f"Duplicate task definition: {task.name}")
        self.tasks[task.name] = task

    def define_task_signature(self, sig: TaskSignature):
        self.task_sigs[sig.name] = sig


@dataclass
class _TaskScope:
    materials: Set[str]
    ints: Set[str]


class SemanticAnalyzer:
    def __init__(self, target_model: Optional[TargetModelLoader] = None, logical_only: bool = False):
        self.symtab = SymbolTable()
        self.target_model = target_model
        # 为真时只验证订单自身的逻辑语义（能力标注是否相容、线性物料是否被重复消费等），
        # 不查询目标机模型。目标绑定交给 Target Lowering。
        self.logical_only = logical_only

    def analyze(self, ast: Program):
        # 1) Global declarations
        for decl in ast.declarations:
            if isinstance(decl, ResourceDecl):
                self.symtab.define_resource(decl.name, decl.category, decl.capabilities)
            elif isinstance(decl, MaterialDecl):
                self.symtab.define_material(decl.name)

        # 2) Collect tasks
        for task in ast.tasks:
            self.symtab.define_task(task)

        # 3) Build task signatures (for call checking)
        for task in ast.tasks:
            return_count = self._count_returns(task.body or [])
            if return_count > 1:
                raise SemanticError(
                    f"Task {task.name} has multiple return statements; ambiguous return value."
                )
            self.symtab.define_task_signature(
                TaskSignature(task.name, param_count=len(task.params or []), return_count=return_count)
            )

        # 4) Analyze each task body
        for task in ast.tasks:
            scope = _TaskScope(materials=set(self.symtab.materials), ints=set())
            for p in task.params or []:
                if isinstance(p, str) and p.lower() in ("due_time", "order_due_time", "due", "deadline"):
                    scope.ints.add(p)
                else:
                    scope.materials.add(p)
            self._analyze_block(task.body or [], scope)

    def _count_returns(self, stmts: List[Statement]) -> int:
        count = 0
        for stmt in stmts or []:
            if isinstance(stmt, ReturnStmt):
                count += 1
            elif isinstance(stmt, IfStmt):
                count += self._count_returns(stmt.true_block or [])
                count += self._count_returns(stmt.false_block or [])
            elif isinstance(stmt, TryCatchStmt):
                count += self._count_returns(stmt.try_block or [])
                count += self._count_returns(stmt.catch_block or [])
                count += self._count_returns(stmt.compensate_block or [])
            elif isinstance(stmt, ParallelBlock):
                count += self._count_returns(stmt.statements or [])
            elif isinstance(stmt, ForeachStmt):
                count += self._count_returns(stmt.body or [])
        return count

    def _analyze_block(self, stmts: List[Statement], scope: _TaskScope):
        for stmt in stmts or []:
            if isinstance(stmt, Operation):
                self._check_operation(stmt, scope)
            elif isinstance(stmt, IntDecl):
                scope.ints.add(stmt.name)
            elif isinstance(stmt, CallStmt):
                self._check_call(stmt, scope)
            elif isinstance(stmt, ReturnStmt):
                self._check_return(stmt, scope)
            elif isinstance(stmt, IfStmt):
                self._analyze_block(stmt.true_block, scope)
                self._analyze_block(stmt.false_block, scope)
            elif isinstance(stmt, TryCatchStmt):
                self._analyze_block(stmt.try_block, scope)
                self._analyze_block(stmt.catch_block, scope)
                self._analyze_block(stmt.compensate_block, scope)
            elif isinstance(stmt, ParallelBlock):
                self._analyze_block(stmt.statements, scope)
            elif isinstance(stmt, ForeachStmt):
                # foreach item variable is a new name bound in the loop body.
                # For now treat it as a material alias and allow it in body.
                child_scope = _TaskScope(materials=set(scope.materials), ints=set(scope.ints))
                child_scope.materials.add(stmt.item_var)
                self._analyze_block(stmt.body, child_scope)

    def _check_return(self, stmt: ReturnStmt, scope: _TaskScope):
        if stmt.value not in scope.materials and stmt.value not in scope.ints:
            raise SemanticError(f"Return undefined variable: {stmt.value}")

    def _check_call(self, call: CallStmt, scope: _TaskScope):
        if call.callee not in self.symtab.tasks:
            raise SemanticError(f"Undefined task: {call.callee}")

        sig = self.symtab.task_sigs.get(call.callee)
        if sig is None:
            raise SemanticError(f"Missing task signature: {call.callee}")

        if len(call.args or []) != sig.param_count:
            raise SemanticError(
                f"Task call {call.callee} expects {sig.param_count} args, got {len(call.args or [])}."
            )

        # Current language only declares Task params as Material, so args should be materials.
        for arg in call.args or []:
            if arg not in scope.materials:
                raise SemanticError(f"Task call {call.callee} uses undefined material arg: {arg}")

        if call.target is not None and not sig.has_return:
            raise SemanticError(f"Task {call.callee} is void; cannot assign its result.")
        if call.target is None and sig.has_return:
            # Allow statement-call of returning task, but warn via error? Keep permissive for now.
            pass

        if call.target is not None:
            # Target becomes defined in current scope. Default to material (physical object).
            # If later you add explicit return typing, refine this rule.
            scope.materials.add(call.target)

    def _check_operation(self, op: Operation, scope: _TaskScope):
        tm = self.target_model

        def require_target_model():
            if tm is None or not getattr(tm, "data", None):
                raise SemanticError("Target model not loaded; cannot validate physical operations.")

        # Validate Process/Adjust/Inspect device capability
        if op.op_type in ("Process", "Adjust", "Inspect"):
            if not op.args:
                raise SemanticError(f"{op.op_type} missing arguments.")
            device_name = op.args[0]
            if device_name not in self.symtab.resources:
                raise SemanticError(f"Undefined device: {device_name}")
            res = self.symtab.resources[device_name]
            if op.op_type == "Process" and "Process" not in res["caps"]:
                raise SemanticError(f"Device {device_name} lacks <Process> capability.")

        # Validate rp legality and factory capability against target model (dimension 1)
        #
        # Paper 1 边界：动作编码（rp）与"本厂有没有能干这个活的资源"属于**目标绑定**问题，
        # 不是订单意图是否合法的问题。因此在 logical_only 模式下这里只做订单自身的逻辑检查，
        # 上述校验由 compiler/target_lowering.py 在 Factory Target Machine Model F_t 上完成。
        if op.op_type in ("Process", "Move", "Load", "Store", "Inspect", "Adjust") and not self.logical_only:
            require_target_model()
            inst = tm.optype_to_instruction(op.op_type)
            if inst is None:
                raise SemanticError(f"Unknown physical operation type: {op.op_type}")
            rp_desc = getattr(op, "param_str", None)
            rp_code = tm.resolve_rp_code(inst.opcode, inst.func, rp_desc)
            if not rp_code:
                raise SemanticError(
                    f"Unsupported rp description for {op.op_type}: {rp_desc!r}"
                )
            if not tm.is_rp_supported(inst.opcode, inst.func, rp_code):
                raise SemanticError(
                    f"Operation not in target model instruction set: opcode={inst.opcode} func={inst.func} rp={rp_code}"
                )
            # Without binding, we can only check existence of a capable PALU somewhere in factory.
            if not tm.any_palu_supports(inst.opcode, inst.func, rp_code):
                raise SemanticError(
                    f"No PALU in target model supports this operation: opcode={inst.opcode} func={inst.func} rp={rp_code}"
                )

        # Basic existence checks for identifiers used as args
        for arg in op.args or []:
            if arg in self.symtab.resources:
                continue
            if arg in scope.materials:
                continue
            if arg in scope.ints:
                continue
            # numeric literals are passed as strings in current AST
            if isinstance(arg, str) and arg.isdigit():
                continue

        # Define target in scope (int/material). Keep simple: if it was declared with `int`
        # in parser, we can't see that here reliably. Default to material.
        if op.target:
            if op.op_type == "IsAvailable":
                scope.ints.add(op.target)
            else:
                scope.materials.add(op.target)
