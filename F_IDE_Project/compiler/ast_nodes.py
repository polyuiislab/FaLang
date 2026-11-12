from dataclasses import dataclass
from typing import List, Optional, Union

class ASTNode: pass

@dataclass
class Program(ASTNode):
    declarations: List  
    tasks: List

@dataclass
class ResourceDecl(ASTNode):
    name: str
    category: str      
    capabilities: List[str] 
    capacity: Optional[int] 
    # The declaration keyword distinguishes Buffer and Storage roles with the same category.

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
    target: Optional[str] = None  
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
    op_type: str       
    args: List[str]
    target: Optional[str] = None 
    param_str: Optional[str] = None 
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
    item_var: str        
    collection: str      
    body: List           
    start_line: Optional[int] = None
    end_line: Optional[int] = None
