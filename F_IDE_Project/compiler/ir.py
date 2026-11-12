from typing import List, Dict, Optional, Tuple

class IRInstr:
    pass

class OpInstr(IRInstr):
    def __init__(
        self,
        op_type: str,
        target: str,
        args: List[str],
        params: str = None,
        opcode: str = None,
        func: str = None,
        operator: str = None,
        rp: str = None,
        expected_time: float = None,
        implicit_targets: List[str] = None,
    ):
        self.op_type = op_type
        self.target = target
        self.args = args
        self.params = params

        self.opcode = opcode
        self.func = func
        self.operator = operator
        self.rp = rp
        self.expected_time = expected_time
        # Implicit material-state versions used to build manufacturing dependencies.
        self.implicit_targets = implicit_targets or []

        self.catch_edges: List['BasicBlock'] = []

        self.scheduled_start: Optional[float] = None
        self.scheduled_finish: Optional[float] = None

        self.assigned_preg = None
        self.assigned_workstation_id: Optional[int] = None
        self.assigned_palu_id: Optional[int] = None
        self.assigned_device_id: Optional[str] = None
        # Catch instructions execute only along an activated exception path.
        self.contingency_scenario_id: Optional[str] = None
        self.contingency_start: Optional[float] = None
        self.contingency_finish: Optional[float] = None
        self.contingency_deadline: Optional[float] = None
        self.contingency_deadline_source: Optional[str] = None
        self.contingency_state: Optional[str] = None  
        # Runtime exception-table lookup leaves the normal instruction path unchanged.
        self.exception_table_entry_id: Optional[str] = None
        self.exception_handler_address: Optional[int] = None

        self.initial_busy_reserve: bool = False  
        self.reserve_duration: Optional[float] = None  
        self.runtime_state_origin: Optional[str] = None  

    def __str__(self):
        args_str = ", ".join(self.args)
        param_str = f'"{self.params}"' if self.params else ""
        all_args = ", ".join(filter(None, [args_str, param_str]))
        if self.target:
            return f"{self.target} = {self.op_type}({all_args})"
        return f"{self.op_type}({all_args})"

class BranchInstr(IRInstr):
    def __init__(self, cond_var: str, true_block: 'BasicBlock', false_block: 'BasicBlock'):
        self.cond_var = cond_var
        self.true_block = true_block
        self.false_block = false_block

    def __str__(self):
        return f"Branch {self.cond_var}? Block_{self.true_block.id} : Block_{self.false_block.id}"

class JumpInstr(IRInstr):
    def __init__(self, target_block: 'BasicBlock'):
        self.target_block = target_block

    def __str__(self):
        return f"Jump Block_{self.target_block.id}"

class PhiInstr(IRInstr):
    def __init__(self, target: str, choices: Dict):
        self.target = target
        self.choices = choices  # Predecessor block to incoming SSA version.

    def __str__(self):
        pairs = []
        for block, var in self.choices.items():
            pairs.append(f"[%{var}, %Block_{block.id}]")
        choices_str = ", ".join(pairs)
        return f"{self.target} = Phi({choices_str})"

class BasicBlock:
    _id_counter = 0

    def __init__(self, name=""):
        self.id = BasicBlock._id_counter
        BasicBlock._id_counter += 1
        self.name = name
        self.instructions: List[IRInstr] = []
        self.predecessors: List[BasicBlock] = []
        self.successors: List[BasicBlock] = []

        self.source_ranges: List[tuple] = []  # (start_line, end_line, description) ranges for source navigation.

    def add_source_range(self, start_line: int, end_line: int, description: str):
        if start_line is None:
            return
        if end_line is None:
            end_line = start_line
        self.source_ranges.append((start_line, end_line, description))

    def get_source_range_summary(self):
        if not self.source_ranges:
            return ""
        ranges = []
        for (s, e, desc) in self.source_ranges:
            if s == e:
                ranges.append(f"{s}: {desc}")
            else:
                ranges.append(f"{s}-{e}: {desc}")
        return "; ".join(ranges)

    def add_instr(self, instr: IRInstr):
        self.instructions.append(instr)


    schedule_result: Optional[List] = None

    def add_successor(self, block: 'BasicBlock'):
        if block not in self.successors:
            self.successors.append(block)
        if self not in block.predecessors:
            block.predecessors.append(self)

class ControlFlowGraph:
    def __init__(self):
        self.entry_block = BasicBlock("Entry")
        self.blocks: List = [self.entry_block]

    def new_block(self, name="") -> BasicBlock:
        block = BasicBlock(name)
        self.blocks.append(block)
        return block
