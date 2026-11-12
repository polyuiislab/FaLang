# F_IDE_Project/compiler/codegen.py
"""
FaLang 代码生成器（Code Generator）。

将完成寄存器分配和 Post-RA 调度的 IR 翻译为 FISA 机器码（32-bit Hex）。
支持的指令格式：
- R-type: OP(6) | RS(5) | RT(5) | RP(5) | RD(5) | func(3) | operator(3)
- I-type: OP(6) | RS(5) | RT(5) | RP(5) | immediate(8) | operator(3)

控制流指令（Jump、Branch）沿用 MIPS 指令格式。
"""
from typing import Dict, List, Optional

from compiler.exception_table import ExceptionTableBuilder
from compiler.ir import OpInstr, BranchInstr, JumpInstr, BasicBlock, ControlFlowGraph
from core.target_model import TargetModelLoader


class CodeGenerator:
    """FaLang FISA 机器码生成器。"""

    IMAGE_MAGIC = "F15AC0E1"
    IMAGE_VERSION = "00000001"
    IMAGE_HEADER_WORDS = 8
    EXCTAB_MAGIC = "F15AEC7B"
    EXCTAB_ENTRY_WORDS = 4
    META_MAGIC = "F15A4E7A"

    def __init__(self, target_model: TargetModelLoader):
        self.tm = target_model
        self.coe_image = None

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    def generate(self, cfg: ControlFlowGraph, allocation: Dict[str, any]) -> List[str]:
        """
        遍历 CFG 所有基本块，逐条指令编码为 32-bit Hex 字符串。
        返回机器码列表。
        """
        self._last_allocation = allocation
        self._block_address, self._instr_address = self._compute_addresses(cfg)
        self.exception_table = self._build_codegen_exception_table(cfg)
        machine_codes: List[str] = []
        for block in cfg.blocks:
            for instr in block.instructions:
                code = self._encode_instr(instr, allocation)
                if code is not None:
                    machine_codes.append(code)
        self.coe_image = self.build_segmented_coe_image(machine_codes, self.exception_table)
        return machine_codes

    def _compute_addresses(self, cfg: ControlFlowGraph):
        addr = 0
        block_addr: Dict[int, int] = {}
        instr_addr: Dict[int, int] = {}
        for block in cfg.blocks:
            block_addr[block.id] = addr
            for instr in block.instructions:
                if self._encode_instr(instr, getattr(self, "_last_allocation", {})) is not None:
                    instr_addr[id(instr)] = addr
                    addr += 1
        return block_addr, instr_addr

    def _build_codegen_exception_table(self, cfg: ControlFlowGraph) -> List[dict]:
        entries = list(getattr(cfg, "exception_table", []) or [])
        if not entries:
            entries = ExceptionTableBuilder().build(cfg)
        table = []
        for landing_idx, entry in enumerate(entries, start=1):
            trigger_pc = self._instr_address.get(id(entry.trigger_instr), self._block_address.get(entry.trigger_block_id, 0))
            handler_addr = self._block_address.get(entry.catch_block.id, 0)
            entry.handler_address = handler_addr
            entry.trigger_instr.exception_table_entry_id = entry.entry_id
            entry.trigger_instr.exception_handler_address = handler_addr
            entry.landing_pad_numeric_id = landing_idx
            table.append({
                "entry_id": entry.entry_id,
                "trigger_pc": trigger_pc,
                "trigger_block_id": entry.trigger_block_id,
                "trigger_instr_index": entry.trigger_instr_index,
                "exception_type": entry.exception_type,
                "landing_pad_id": entry.landing_pad.landing_pad_id,
                "landing_pad_numeric_id": landing_idx,
                "faulted_materials": list(entry.landing_pad.faulted_materials),
                "catch_block_id": entry.catch_block.id,
                "handler_address": handler_addr,
            })
        cfg.codegen_exception_table = table
        return table

    def build_segmented_coe_image(self, text_words: List[str], exception_table: List[dict]) -> dict:
        exctab_words = self._encode_exctab(exception_table)
        meta_words = self._encode_meta(text_words, exctab_words, exception_table)
        text_base = self.IMAGE_HEADER_WORDS
        exctab_base = text_base + len(text_words)
        meta_base = exctab_base + len(exctab_words)
        header = [
            self.IMAGE_MAGIC,
            self.IMAGE_VERSION,
            f"{text_base:08X}",
            f"{len(text_words):08X}",
            f"{exctab_base:08X}",
            f"{len(exctab_words):08X}",
            f"{meta_base:08X}",
            f"{len(meta_words):08X}",
        ]
        flat_words = header + list(text_words) + exctab_words + meta_words
        return {
            "format": "FISA_SEGMENTED_COE_V1",
            "text": list(text_words),
            "exctab": exctab_words,
            "meta": meta_words,
            "flat": flat_words,
            "manifest": {
                "text_base": text_base,
                "text_words": len(text_words),
                "exctab_base": exctab_base,
                "exctab_words": len(exctab_words),
                "meta_base": meta_base,
                "meta_words": len(meta_words),
                "entries": exception_table,
            },
        }

    def _encode_exctab(self, exception_table: List[dict]) -> List[str]:
        words = [
            self.EXCTAB_MAGIC,
            f"{len(exception_table):08X}",
            f"{self.EXCTAB_ENTRY_WORDS:08X}",
            "00000001",
        ]
        for entry in exception_table:
            trigger_pc = int(entry.get("trigger_pc", 0)) & 0xFFFFFFFF
            handler_pc = int(entry.get("handler_address", 0)) & 0xFFFFFFFF
            exc_code = self._exception_type_code(str(entry.get("exception_type", "")))
            flags = self._exception_flags(entry)
            fault_policy = 1
            landing_id = int(entry.get("landing_pad_numeric_id", 0)) & 0xFF
            material_mask = self._material_mask(entry.get("faulted_materials") or [])
            deadline_mode = 0
            word1 = ((exc_code & 0xFF) << 24) | ((flags & 0xFF) << 16) | ((fault_policy & 0xFF) << 8)
            word3 = ((landing_id & 0xFF) << 24) | ((material_mask & 0xFFFF) << 8) | (deadline_mode & 0xFF)
            words.extend([
                f"{trigger_pc:08X}",
                f"{word1:08X}",
                f"{handler_pc:08X}",
                f"{word3:08X}",
            ])
        return words

    def _encode_meta(self, text_words: List[str], exctab_words: List[str], exception_table: List[dict]) -> List[str]:
        checksum = 0
        for word in list(text_words) + list(exctab_words):
            try:
                checksum = (checksum + int(word, 16)) & 0xFFFFFFFF
            except Exception:
                continue
        return [
            self.META_MAGIC,
            "00000001",
            f"{len(text_words):08X}",
            f"{len(exception_table):08X}",
            f"{checksum:08X}",
            "00000000",
            "00000000",
            "00000000",
        ]

    def _exception_type_code(self, exception_type: str) -> int:
        """异常类型号由目标机模型给出，避免类型码表在 lexer / 模型 / codegen 三处各写一份。"""
        getter = getattr(self.tm, "get_exception_code", None)
        if getter is not None:
            return getter(exception_type)
        from core.target_model import DEFAULT_EXCEPTION_CODES, UNKNOWN_EXCEPTION_CODE
        return DEFAULT_EXCEPTION_CODES.get(exception_type, UNKNOWN_EXCEPTION_CODE)

    def _exception_flags(self, entry: dict) -> int:
        flags = 0x02
        if entry.get("faulted_materials"):
            flags |= 0x01
        return flags

    def _material_mask(self, materials: List[str]) -> int:
        mask = 0
        allocation = getattr(self, "_last_allocation", {}) or {}
        for mat in materials:
            preg = allocation.get(mat)
            if preg is None or preg == "memory":
                continue
            try:
                rid = int(preg)
            except Exception:
                continue
            if 0 <= rid < 16:
                mask |= 1 << rid
        return mask

    # ------------------------------------------------------------------
    # 指令编码分发
    # ------------------------------------------------------------------
    def _encode_instr(self, instr, allocation: Dict[str, any]) -> Optional[str]:
        if isinstance(instr, OpInstr):
            return self._encode_op(instr, allocation)
        if isinstance(instr, JumpInstr):
            return self._encode_jump(instr)
        if isinstance(instr, BranchInstr):
            return self._encode_branch(instr)
        return None

    # ------------------------------------------------------------------
    # OpInstr 编码
    # ------------------------------------------------------------------
    def _encode_op(self, instr: OpInstr, allocation: Dict[str, any]) -> Optional[str]:
        key = self.tm.optype_to_instruction(instr.op_type)
        if key is None:
            return None

        opcode = key.opcode
        func = key.func
        palu_id = self._resolve_palu_id(instr, key)
        operator = f"{palu_id:03b}"
        rp_val = int(instr.rp, 2) if instr.rp else 0

        rs, rt, rd = self._get_registers(instr, allocation)

        if instr.op_type in ("Load", "Store"):
            # I-type
            immediate = 0
            return self._encode_i_type(opcode, rs, rt, rp_val, immediate, operator)
        else:
            # R-type
            return self._encode_r_type(opcode, rs, rt, rp_val, rd, func, operator)

    # ------------------------------------------------------------------
    # Jump / Branch 编码（MIPS 格式）
    # ------------------------------------------------------------------
    def _encode_jump(self, instr: JumpInstr) -> str:
        """MIPS J-type: OP(6) | target(26)。OP=000010(2)。"""
        target_addr = getattr(instr.target_block, "id", 0) & 0x3FFFFFF
        val = (2 << 26) | target_addr
        return f"{val:08X}"

    def _encode_branch(self, instr: BranchInstr) -> str:
        """
        MIPS I-type branch: OP(6) | RS(5) | RT(5) | offset(16)。
        使用 beq (OP=000100=4)。
        RS 取 cond_var 的 PReg（若未分配则为 0），RT=$zero(0)，offset 取 true_block.id。
        """
        rs = 0
        if isinstance(instr.cond_var, str):
            preg = None
            if hasattr(self, "_last_allocation"):
                preg = self._last_allocation.get(instr.cond_var, 0)
            if preg is not None and preg != "memory":
                try:
                    rs = int(preg) & 0x1F
                except (ValueError, TypeError):
                    rs = 0
        offset = getattr(instr.true_block, "id", 0) & 0xFFFF
        val = (4 << 26) | (rs << 21) | (0 << 16) | offset
        return f"{val:08X}"

    # ------------------------------------------------------------------
    # 编码辅助函数
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_r_type(
        op: str, rs: int, rt: int, rp: int, rd: int, func: str, operator: str
    ) -> str:
        """R-type: OP(6) | RS(5) | RT(5) | RP(5) | RD(5) | func(3) | operator(3)"""
        val = int(op, 2) << 26
        val |= (rs & 0x1F) << 21
        val |= (rt & 0x1F) << 16
        val |= (rp & 0x1F) << 11
        val |= (rd & 0x1F) << 6
        val |= int(func, 2) << 3
        val |= int(operator, 2)
        return f"{val:08X}"

    @staticmethod
    def _encode_i_type(
        op: str, rs: int, rt: int, rp: int, immediate: int, operator: str
    ) -> str:
        """I-type: OP(6) | RS(5) | RT(5) | RP(5) | immediate(8) | operator(3)"""
        val = int(op, 2) << 26
        val |= (rs & 0x1F) << 21
        val |= (rt & 0x1F) << 16
        val |= (rp & 0x1F) << 11
        val |= (immediate & 0xFF) << 3
        val |= int(operator, 2)
        return f"{val:08X}"

    # ------------------------------------------------------------------
    # 寄存器解析
    # ------------------------------------------------------------------
    def _material_holding(self, instr: OpInstr, allocation: Dict[str, any]) -> int:
        """
        取 Load/Store 涉及的物理持有位置编号。

        两种写法都要支持：
        - 指令显式给出位置（`Load(pc, "20")`，第二个参数是数字串）；
        - 只给出物料，位置由分配结果决定（源码里的 `Load(pc, "Warehouse_To_PM_Buffer")`
          只说明"从仓库取"，具体落到哪个库位由编译器在 F_t 上确定）。
        """
        if len(instr.args) >= 2:
            explicit = instr.args[1]
            if isinstance(explicit, str) and explicit.isdigit():
                return int(explicit) & 0x1F
        preg = getattr(instr, "assigned_preg", None)
        if preg is None and instr.args:
            preg = allocation.get(instr.args[0])
        if isinstance(preg, int):
            return preg & 0x1F
        return 0

    def _get_registers(self, instr: OpInstr, allocation: Dict[str, any]):
        """根据指令类型和分配映射，返回 (rs, rt, rd) 三个 5-bit 寄存器编号。"""
        rs = 0
        rt = 0
        rd = 0

        # RD: target 的 PReg
        if instr.target and isinstance(instr.target, str):
            preg = getattr(instr, "assigned_preg", None)
            if preg is None:
                preg = allocation.get(instr.target, 0)
            if preg != "memory" and preg is not None:
                try:
                    rd = int(preg) & 0x1F
                except (ValueError, TypeError):
                    rd = 0

        # 按 op_type 分别解析 RS/RT
        if instr.op_type == "Load":
            # I-type：把物料从仓储取到它被分配的持有位置。
            # RS = 基址(0)，RT = 目标持有位置。
            rt = self._material_holding(instr, allocation)
            rs = 0

        elif instr.op_type == "Store":
            # I-type：把物料从它当前的持有位置放回仓储。
            # RS = 源持有位置，RT = 0。
            rs = self._material_holding(instr, allocation)
            rt = 0

        elif instr.op_type in ("Move", "Assign"):
            # Move(vreg, dst_reg_str) / Assign(vreg, dst_reg_str): R-type
            # RS = vreg 的 PReg, RT = dst_reg_str
            if instr.args:
                vreg = instr.args[0]
                if isinstance(vreg, str) and vreg:
                    preg = allocation.get(vreg, 0)
                    if getattr(instr, "assigned_preg", None) is not None:
                        preg = getattr(instr, "assigned_preg")
                    if preg != "memory" and preg is not None:
                        try:
                            rs = int(preg) & 0x1F
                        except (ValueError, TypeError):
                            rs = 0
                if len(instr.args) >= 2:
                    arg2 = instr.args[1]
                    if isinstance(arg2, str) and arg2.isdigit():
                        rt = int(arg2) & 0x1F

        else:
            # Process / Inspect / Adjust: 取物料参数的 PReg
            idxs = self._material_arg_indices(instr.op_type, len(instr.args))
            regs = []
            for i in idxs:
                if i < len(instr.args):
                    vreg = instr.args[i]
                    if isinstance(vreg, str) and vreg:
                        preg = allocation.get(vreg, 0)
                        if getattr(instr, "assigned_preg", None) is not None:
                            preg = getattr(instr, "assigned_preg")
                        if preg != "memory" and preg is not None:
                            try:
                                regs.append(int(preg) & 0x1F)
                            except (ValueError, TypeError):
                                pass
            if len(regs) >= 1:
                rs = regs[0]
            if len(regs) >= 2:
                rt = regs[1]

        return rs, rt, rd

    # ------------------------------------------------------------------
    # PALU ID 解析（直接读取寄存器分配阶段写入的 assigned_palu_id）
    # ------------------------------------------------------------------
    def _resolve_palu_id(self, instr: OpInstr, key) -> int:
        """
        读取 RegisterAllocator 在分配阶段为指令显式写入的 assigned_palu_id。
        若不存在（旧 IR 或第三方调用），回退到全局能力查询。
        """
        palu_id = getattr(instr, "assigned_palu_id", None)
        if isinstance(palu_id, int):
            return palu_id

        # 回退：全局查询 palu_definitions
        ics = self.tm.data.get("instruction_capability_set", {}) if self.tm.data else {}
        palus = ics.get("palu_definitions", [])
        for palu in palus:
            if not isinstance(palu, dict):
                continue
            pid = palu.get("palu_id")
            if not isinstance(pid, int):
                continue
            for cap in palu.get("supported_capabilities", []):
                if not isinstance(cap, dict):
                    continue
                if (str(cap.get("opcode", "")) == key.opcode and
                        str(cap.get("func", "")) == key.func):
                    rp_code = instr.rp
                    if rp_code and str(cap.get("rp", "")) != rp_code:
                        continue
                    return pid
        return 0

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _material_arg_indices(self, op_type: str, argc: int):
        """返回指令中物料参数在 args 列表中的下标。"""
        if op_type == "Process":
            return range(1, argc)
        if op_type == "Inspect":
            return [1] if argc >= 2 else []
        if op_type in ("Move", "Load", "Store"):
            return [0] if argc >= 1 else []
        if op_type == "Assign":
            return [0] if argc >= 1 else []
        return []
