# F_IDE_Project/core/compiler/ast_nodes.py
from dataclasses import dataclass
from typing import List, Optional, Union

class ASTNode: pass

@dataclass
class Program(ASTNode):
    declarations: List  # 包含 Resource, Buffer, Storage, Material
    tasks: List

@dataclass
class ResourceDecl(ASTNode):
    name: str
    category: str      # 'Device', 'Register', 'Memory'
    capabilities: List[str] # e.g., ['Process']
    capacity: Optional[int] # 用于 Buffer 约束溢出
    # 声明关键字本身（'Resource' / 'Buffer' / 'Storage'）。VMRS 需要它来判定逻辑角色：
    # `Buffer X: Memory<>` 与 `Storage X: Memory<>` 的 category 相同，但角色不同。
    decl_kind: str = "Resource"

@dataclass
class MaterialDecl(ASTNode):
    name: str
    quantity: int

@dataclass
class TaskDef(ASTNode):
    name: str
    params: List[str]
    body: List

class Statement(ASTNode):
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class CallStmt(Statement):
    callee: str
    args: List[str]
    target: Optional[str] = None  # out = SubTask(...)
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class ReturnStmt(Statement):
    value: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class IntDecl(Statement):
    name: str
    value: int
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class Operation(Statement):
    op_type: str       # 'Load', 'Process', 'Inspect', 'Adjust'
    args: List[str]
    target: Optional[str] = None # 用于 Result = Op(...)
    param_str: Optional[str] = None # 工艺参数字符串
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class IfStmt(Statement):
    cond_var: str
    cond_val: int
    true_block: List
    false_block: List
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class TryCatchStmt(Statement):
    try_block: List
    exception_type: str
    catch_block: List
    compensate_block: List
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class ParallelBlock(Statement):
    statements: List
    start_line: Optional[int] = None
    end_line: Optional[int] = None

@dataclass
class ForeachStmt(Statement):
    item_var: str        # 迭代的元素变量名，例如 'item'
    collection: str      # 被迭代的集合或托盘变量名，例如 'Pallet'
    body: List           # 循环体内的语句列表
    start_line: Optional[int] = None
    end_line: Optional[int] = None
