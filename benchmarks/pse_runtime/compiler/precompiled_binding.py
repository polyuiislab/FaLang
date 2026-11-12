"""首次编译时生成异常分支的固定资源方案；不预占正常生产资源。"""
from dataclasses import dataclass, field, replace
from copy import copy
from hashlib import sha256

from compiler.factory_target_machine import FactoryState, AVAIL_WORKSTATION, AVAIL_HOLDING
from compiler.ir import ControlFlowGraph
from compiler.mdg_builder import MDGBuilder
from compiler.residual_coloring import ResidualGraphColorer, GraphColoringSelector
from compiler.target_lowering import LoweringResult


@dataclass
class HandlerBinding:
    allocation: dict = field(default_factory=dict)
    # 指令 id -> (资源种类, 工位或 AGV 池 id, 固定执行槽/车辆序号)
    actions: dict = field(default_factory=dict)
    exception_type: str = ''
    instructions: tuple = ()


class StaticHandlerSelector(GraphColoringSelector):
    """同等搬运/占位条件下，按程序点固定分散备用资源，不集中到最小编号。"""

    def __init__(self, program_key):
        self.program_key = program_key

    def sort_key(self, vreg, site_id, colorer):
        joint = super().sort_key(vreg, site_id, colorer)
        rank = sha256(f'{self.program_key}|{joint[2]}'.encode()).digest()
        return (*joint[:2], rank, joint[2], joint[3])


def compile_handler_bindings(handler, f_t, lowering):
    """按异常表程序点及有限故障对象展开方案，所有选址仅在这里执行。"""
    plans = {}
    for entry in handler.exception_table:
        trigger = entry.trigger_instr
        instructions = handler.recovery_instructions(entry.exception_type, entry.catch_block.id)
        outputs = list(trigger.implicit_targets or []) + ([trigger.target] if trigger.target else [])
        materials = {v for i in instructions for v in list(i.implicit_targets or []) + ([i.target] if i.target else [])}
        materials.update(outputs)
        materials &= set(lowering.eligible_sites)
        candidates = {p for v in outputs for p in lowering.eligible_sites.get(v, [])}
        faults = [(None, None)]
        if entry.exception_type == 'MachineFailure':
            faults += [(AVAIL_WORKSTATION, w) for w in sorted({f_t.holding_site(p).workstation_id for p in candidates})]
        elif entry.exception_type == 'BufferUnavailable':
            faults += [(AVAIL_HOLDING, p) for p in sorted(candidates)]
        # 静态分配不借用后来的队列状态，AGV 实例按程序点稳定分散。
        tag = '|'.join(sorted(materials)) + entry.exception_type + str(trigger.params) + '|'.join(trigger.args)
        vehicle = int.from_bytes(sha256(tag.encode()).digest()[:4], 'big')
        base = None
        for kind, resource in faults:
            scratch = ControlFlowGraph()
            scratch_lowering = LoweringResult()
            action_vertices = {}
            action_outputs = {}
            for index, original in enumerate(instructions):
                lowered = lowering.operations[id(original)]
                if not lowered.is_physical or lowered.is_logistics:
                    continue
                # 恢复动作无物料输出时，用编译期资源端点表示其工位需求。
                instr = copy(original)
                definitions = [v for v in list(original.implicit_targets or []) +
                               ([original.target] if original.target else []) if v in materials]
                vertex = definitions[0] if definitions else f'handler_resource_{index}'
                instr.target = vertex
                instr.implicit_targets = []
                instr.args = list(original.args[:1])
                scratch.entry_block.add_instr(instr)
                scratch_lowering.operations[id(instr)] = replace(lowered, instr=instr)
                sites = {p for w in lowered.candidate_workstations for p in f_t.holding_sites_of_workstation(w)}
                for v in definitions:
                    sites &= set(lowering.eligible_sites[v])
                scratch_lowering.eligible_sites[vertex] = sorted(sites)
                action_vertices[id(original)] = vertex
                action_outputs[id(original)] = definitions
            assigned_outputs = {v for vs in action_outputs.values() for v in vs}
            for index, vertex in enumerate(sorted(materials - assigned_outputs)):
                instr = copy(trigger)
                instr.target = vertex
                instr.implicit_targets = []
                instr.args = list(trigger.args[:1])
                scratch.entry_block.add_instr(instr)
                scratch_lowering.operations[id(instr)] = replace(lowering.operations[id(trigger)], instr=instr)
                scratch_lowering.eligible_sites[vertex] = list(lowering.eligible_sites[vertex])
                scratch_lowering.consuming_capabilities[vertex] = set(lowering.consuming_capabilities.get(vertex, ()))
            state = FactoryState()
            if kind is not None:
                state.unavailable_until[kind, resource] = float('inf')
            isolated = f_t.with_state(state)
            scratch_mdg = MDGBuilder(target_model=f_t._tm).build(scratch)
            colorer = ResidualGraphColorer(scratch, scratch_mdg, isolated, scratch_lowering,
                                          selector=StaticHandlerSelector(tag), emit_moves=False)
            result = colorer.color(0.0)
            assigned = result.colored
            binding = HandlerBinding(dict(base.allocation) if base else {}, dict(base.actions) if base else {},
                                     entry.exception_type, tuple(instructions))
            binding.allocation.update({v:p for v,p in assigned.items() if v in materials})
            for instr in instructions:
                lowered = lowering.operations[id(instr)]
                if lowered.is_logistics:
                    logistics = f_t.logistics_resource()
                    binding.actions[id(instr)] = ('logistics', logistics.palu_id, vehicle % logistics.quantity)
                elif id(instr) in action_vertices and action_vertices[id(instr)] in assigned:
                    p = assigned[action_vertices[id(instr)]]
                    binding.actions[id(instr)] = ('workstation', f_t.holding_site(p).workstation_id, 0)
                    binding.allocation.update({v:p for v in action_outputs[id(instr)]})
            # 无备用资源的故障方案沿用正常分支绑定，运行时按既有规则等待修复。
            plans[entry.entry_id, kind, resource] = binding
            if kind is None:
                base = binding
    return plans
