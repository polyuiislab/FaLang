"""Map each block to a Composite FB and each operation to a Basic FB; CFG edges become event connections."""
from typing import Dict, List, Optional, Set, Tuple, Any
from compiler.ir import OpInstr, BranchInstr, JumpInstr, BasicBlock, ControlFlowGraph
from compiler.fb_config.fb_model import (
    FBConfig, FBNetwork, FBInstance, Connection,
    CompositeFBDef, BasicFBDef, FBInterface, FBPort,
    Algorithm, ECCState, ECCTransition,
)
from core.target_model import TargetModelLoader


class FBConfigBuilder:


    def __init__(self, target_model: TargetModelLoader):
        self.tm = target_model
        self._resource_names: Set[str] = set()
        self._catch_triggers_loaded = False


    def build(self, cfg: ControlFlowGraph, allocation: Dict[str, Any]) -> FBConfig:

        self._resource_names = set(getattr(cfg, "resource_names", set()) or set())
        config = FBConfig(network_name="FaLang_FB_Network")


        block_cfb_types: Dict[int, str] = {}
        block_cfb_instances: Dict[int, FBInstance] = {}
        for block in cfg.blocks:
            cfb_type_name = f"BlockCFB_{block.id}"
            cfb_def, cfb_inst = self._build_block_cfb(block, allocation, cfb_type_name, config)
            config.composite_fb_types[cfb_type_name] = cfb_def
            config.top_level.instances.append(cfb_inst)
            block_cfb_types[block.id] = cfb_type_name
            block_cfb_instances[block.id] = cfb_inst
            config.block_to_cfb[block.id] = cfb_inst.instance_id


        config.basic_fb_types.update(self._basic_fb_type_cache)
        self._add_catch_runtime_bfb_defs(config)


        seen_top_conns: set = set()

        def add_top_conn(conn: Connection):
            key = (conn.source, conn.destination, conn.conn_type)
            if key not in seen_top_conns:
                seen_top_conns.add(key)
                config.top_level.connections.append(conn)

        for block in cfg.blocks:
            src_cfb = block_cfb_instances[block.id]
            src_type = config.composite_fb_types[block_cfb_types[block.id]]


            has_branch = any(isinstance(i, BranchInstr) for i in block.instructions)
            if has_branch and len(block.successors) == 2:

                add_top_conn(Connection(
                    source=f"{src_cfb.instance_id}.EO_TRUE",
                    destination=f"{block_cfb_instances[block.successors[0].id].instance_id}.EI_ENTRY",
                    conn_type="EVENT",
                    comment="branch true",
                ))

                add_top_conn(Connection(
                    source=f"{src_cfb.instance_id}.EO_FALSE",
                    destination=f"{block_cfb_instances[block.successors[1].id].instance_id}.EI_ENTRY",
                    conn_type="EVENT",
                    comment="branch false",
                ))
            elif block.successors:

                for succ in block.successors:
                    if succ.id in block_cfb_instances:
                        add_top_conn(Connection(
                            source=f"{src_cfb.instance_id}.EO_EXIT",
                            destination=f"{block_cfb_instances[succ.id].instance_id}.EI_ENTRY",
                            conn_type="EVENT",
                            comment="sequential",
                        ))


            if self._block_has_exc_port(src_type):
                for instr_idx, instr in enumerate(block.instructions):
                    if isinstance(instr, OpInstr) and instr.catch_edges:
                        for catch_block in instr.catch_edges:
                            if catch_block.id in block_cfb_instances:
                                catch_cfb = block_cfb_instances[catch_block.id]
                                self._add_exception_runtime_network(
                                    config,
                                    add_top_conn,
                                    src_cfb,
                                    instr,
                                    catch_block,
                                    catch_cfb,
                                    block.id,
                                    instr_idx,
                                    f"EO_EXC_I{instr_idx}",
                                )

        return config

    def _add_catch_runtime_bfb_defs(self, config: FBConfig):
        for name in (
            "CatchDispatcher_FB",
            "LandingPad_FB",
            "CatchResourceRequest_FB",
            "DeferredCatchQueue_FB",
            "LateRecovery_FB",
            "SafeDegrade_FB",
        ):
            if name in config.basic_fb_types:
                continue
            d = BasicFBDef(type_name=name)
            d.interface.event_inputs = [FBPort("EI_EXE", "EVENT", "IN")]
            d.interface.event_outputs = [
                FBPort("EO_DONE", "EVENT", "OUT"),
                FBPort("EO_DEFER", "EVENT", "OUT"),
                FBPort("EO_LATE", "EVENT", "OUT"),
            ]
            d.interface.data_inputs = [
                FBPort("DI_deadline", "DATA", "IN", data_type="REAL"),
                FBPort("DI_scenario", "DATA", "IN", data_type="STRING"),
            ]
            d.ecc_states = [ECCState("START"), ECCState("RUN", algorithm="ALG_RUN")]
            d.ecc_transitions = [
                ECCTransition("START", "RUN", "EI_EXE"),
                ECCTransition("RUN", "START", "EO_DONE"),
                ECCTransition("RUN", "START", "EO_DEFER"),
                ECCTransition("RUN", "START", "EO_LATE"),
            ]
            d.algorithms = [Algorithm("ALG_RUN", name.replace("_FB", ""))]
            config.basic_fb_types[name] = d

    def _add_exception_runtime_network(
        self,
        config: FBConfig,
        add_top_conn,
        src_cfb: FBInstance,
        instr: OpInstr,
        catch_block: BasicBlock,
        catch_cfb: FBInstance,
        block_id: int,
        instr_idx: int,
        source_event_port: str = "EO_EXC",
    ):
        """Connect an exception-raising operation to the exception runtime and catch entry."""
        base = f"EXC_B{block_id}_I{instr_idx}_C{catch_block.id}"
        runtime_instances = [
            FBInstance(f"{base}_Dispatcher", "CatchDispatcher_FB", x=src_cfb.x + 80, y=src_cfb.y + 180),
            FBInstance(f"{base}_LandingPad", "LandingPad_FB", x=src_cfb.x + 260, y=src_cfb.y + 180),
            FBInstance(f"{base}_ResourceRequest", "CatchResourceRequest_FB", x=src_cfb.x + 440, y=src_cfb.y + 180),
            FBInstance(f"{base}_DeferredQueue", "DeferredCatchQueue_FB", x=src_cfb.x + 620, y=src_cfb.y + 260),
            FBInstance(f"{base}_LateRecovery", "LateRecovery_FB", x=src_cfb.x + 800, y=src_cfb.y + 260),
            FBInstance(f"{base}_SafeDegrade", "SafeDegrade_FB", x=src_cfb.x + 980, y=src_cfb.y + 260),
        ]
        existing = {inst.instance_id for inst in config.top_level.instances}
        for inst in runtime_instances:
            if inst.instance_id not in existing:
                config.top_level.instances.append(inst)
                existing.add(inst.instance_id)

        dispatcher = runtime_instances[0].instance_id
        landing = runtime_instances[1].instance_id
        request = runtime_instances[2].instance_id
        queue = runtime_instances[3].instance_id
        late = runtime_instances[4].instance_id
        safe = runtime_instances[5].instance_id

        add_top_conn(Connection(f"{src_cfb.instance_id}.{source_event_port}", f"{dispatcher}.EI_EXE", "EVENT", "exception dispatch"))
        add_top_conn(Connection(f"{dispatcher}.EO_DONE", f"{landing}.EI_EXE", "EVENT", "landing pad lookup"))
        add_top_conn(Connection(f"{landing}.EO_DONE", f"{request}.EI_EXE", "EVENT", "catch resource request"))
        add_top_conn(Connection(f"{request}.EO_DONE", f"{catch_cfb.instance_id}.EI_CATCH", "EVENT", "catch start"))
        add_top_conn(Connection(f"{request}.EO_DEFER", f"{queue}.EI_EXE", "EVENT", "defer catch until resource idle"))
        add_top_conn(Connection(f"{request}.EO_LATE", f"{late}.EI_EXE", "EVENT", "deadline miss"))
        add_top_conn(Connection(f"{queue}.EO_DONE", f"{catch_cfb.instance_id}.EI_CATCH", "EVENT", "deferred catch start"))
        add_top_conn(Connection(f"{queue}.EO_LATE", f"{late}.EI_EXE", "EVENT", "late deferred catch"))
        add_top_conn(Connection(f"{late}.EO_DONE", f"{safe}.EI_EXE", "EVENT", "safe degrade"))


    def _build_block_cfb(
        self, block: BasicBlock, allocation: Dict[str, Any], cfb_type_name: str,
        config: FBConfig,
    ) -> Tuple[CompositeFBDef, FBInstance]:

        cfb_def = CompositeFBDef(type_name=cfb_type_name)
        cfb_def.interface = FBInterface()


        cfb_def.interface.event_inputs.append(FBPort("EI_ENTRY", "EVENT", "IN", comment="block entry"))
        cfb_def.interface.event_outputs.append(FBPort("EO_EXIT", "EVENT", "OUT", comment="normal exit"))


        has_branch = any(isinstance(i, BranchInstr) for i in block.instructions)
        has_exc = any(
            isinstance(i, OpInstr) and i.catch_edges for i in block.instructions
        )
        is_catch = block.name.startswith("Catch_")
        is_comp = block.name.startswith("Compensate")

        if has_branch:
            cfb_def.interface.event_outputs.append(FBPort("EO_TRUE", "EVENT", "OUT", comment="branch true"))
            cfb_def.interface.event_outputs.append(FBPort("EO_FALSE", "EVENT", "OUT", comment="branch false"))
        if has_exc:
            cfb_def.interface.event_outputs.append(FBPort("EO_EXC", "EVENT", "OUT", comment="exception"))
            for idx, instr in enumerate(block.instructions):
                if isinstance(instr, OpInstr) and instr.catch_edges:
                    cfb_def.interface.event_outputs.append(
                        FBPort(f"EO_EXC_I{idx}", "EVENT", "OUT", comment=f"exception from instr {idx}")
                    )
        if is_catch:
            cfb_def.interface.event_inputs.append(FBPort("EI_CATCH", "EVENT", "IN", comment="catch entry"))
        if is_comp:
            cfb_def.interface.event_inputs.append(FBPort("EI_COMP", "EVENT", "IN", comment="compensate entry"))


        phys_instrs: List[Tuple[int, Any]] = []
        for idx, instr in enumerate(block.instructions):
            if isinstance(instr, (OpInstr, BranchInstr)):
                phys_instrs.append((idx, instr))

        # An empty block uses a NOP FB to carry its entry-to-exit event connection.
        if not phys_instrs:
            nop_type = "NOP_FB"
            if nop_type not in self._basic_fb_type_cache:
                self._basic_fb_type_cache[nop_type] = self._make_nop_bfb_def()
            nop_inst = FBInstance(
                instance_id=f"B{block.id}_NOP",
                fb_type=nop_type,
                block_id=block.id,
                instr_index=-1,
            )
            cfb_def.internal_network.instances.append(nop_inst)

            cfb_def.internal_network.connections.append(
                Connection(".EI_ENTRY", f"{nop_inst.instance_id}.EI_EXE", "EVENT")
            )
            cfb_def.internal_network.connections.append(
                Connection(f"{nop_inst.instance_id}.EO_DONE", ".EO_EXIT", "EVENT")
            )
        else:
            prev_bfb_id: Optional[str] = None
            for local_idx, (instr_idx, instr) in enumerate(phys_instrs):
                bfb_inst = self._make_bfb_for_instr(block.id, instr_idx, instr, allocation)
                cfb_def.internal_network.instances.append(bfb_inst)
                config_key = f"{block.id}_{instr_idx}_{self._instr_fb_type_suffix(instr)}"
                config.instr_to_bfb[config_key] = bfb_inst.instance_id


                bfb_type_name = bfb_inst.fb_type
                if bfb_type_name not in self._basic_fb_type_cache:
                    self._basic_fb_type_cache[bfb_type_name] = self._make_bfb_type_def(
                        bfb_type_name, instr, allocation
                    )

                if local_idx == 0:

                    cfb_def.internal_network.connections.append(
                        Connection(".EI_ENTRY", f"{bfb_inst.instance_id}.EI_EXE", "EVENT")
                    )
                if prev_bfb_id is not None:

                    cfb_def.internal_network.connections.append(
                        Connection(f"{prev_bfb_id}.EO_DONE", f"{bfb_inst.instance_id}.EI_EXE", "EVENT")
                    )
                prev_bfb_id = bfb_inst.instance_id


            last_bfb_id = prev_bfb_id
            if has_branch:

                cfb_def.internal_network.connections.append(
                    Connection(f"{last_bfb_id}.EO_TRUE", ".EO_TRUE", "EVENT")
                )
                cfb_def.internal_network.connections.append(
                    Connection(f"{last_bfb_id}.EO_FALSE", ".EO_FALSE", "EVENT")
                )
            else:
                cfb_def.internal_network.connections.append(
                    Connection(f"{last_bfb_id}.EO_DONE", ".EO_EXIT", "EVENT")
                )


            if has_exc:
                for _idx, _instr in phys_instrs:
                    if isinstance(_instr, OpInstr) and _instr.catch_edges:
                        bfb_inst_id = f"B{block.id}_I{_idx}_{_instr.op_type}"
                        cfb_def.internal_network.connections.append(
                            Connection(f"{bfb_inst_id}.EO_EXC", f".EO_EXC_I{_idx}", "EVENT")
                        )
                        cfb_def.internal_network.connections.append(
                            Connection(f"{bfb_inst_id}.EO_EXC", ".EO_EXC", "EVENT")
                        )

        instance_id = f"Block_{block.id}"
        cfb_inst = FBInstance(
            instance_id=instance_id,
            fb_type=cfb_type_name,
            block_id=block.id,
            x=block.id * 200,
            y=100,
            comment=block.name or f"Block {block.id}",
        )
        return cfb_def, cfb_inst


    def _make_bfb_for_instr(
        self, block_id: int, instr_idx: int, instr: Any, allocation: Dict[str, Any]
    ) -> FBInstance:

        if isinstance(instr, BranchInstr):
            fb_type = "COND_BRANCH_FB"
            inst_id = f"B{block_id}_BR{instr_idx}"
        elif isinstance(instr, OpInstr):
            has_exc = bool(instr.catch_edges)
            fb_type = f"{instr.op_type}_EXC_FB" if has_exc else f"{instr.op_type}_FB"
            inst_id = f"B{block_id}_I{instr_idx}_{instr.op_type}"
        else:
            fb_type = "NOP_FB"
            inst_id = f"B{block_id}_NOP{instr_idx}"

        return FBInstance(
            instance_id=inst_id,
            fb_type=fb_type,
            block_id=block_id,
            instr_index=instr_idx,
        )


    _basic_fb_type_cache: Dict[str, BasicFBDef] = {}

    def _make_nop_bfb_def(self) -> BasicFBDef:
        d = BasicFBDef(type_name="NOP_FB")
        d.interface.event_inputs = [FBPort("EI_EXE", "EVENT", "IN")]
        d.interface.event_outputs = [FBPort("EO_DONE", "EVENT", "OUT")]
        d.ecc_states = [
            ECCState("START"),
            ECCState("EXEC", algorithm="ALG_NOP"),
        ]
        d.ecc_transitions = [
            ECCTransition("START", "EXEC", "EI_EXE"),
            ECCTransition("EXEC", "START", "EO_DONE"),
        ]
        d.algorithms = [Algorithm("ALG_NOP", "No operation")]
        return d

    def _make_bfb_type_def(self, type_name: str, instr: Any, allocation: Dict[str, Any]) -> BasicFBDef:
        if isinstance(instr, BranchInstr):
            return self._make_branch_bfb_def(type_name, instr, allocation)
        if isinstance(instr, OpInstr):
            return self._make_op_bfb_def(type_name, instr, allocation)
        return self._make_nop_bfb_def()

    def _make_op_bfb_def(self, type_name: str, instr: OpInstr, allocation: Dict[str, Any]) -> BasicFBDef:
        has_exc = bool(instr.catch_edges)
        d = BasicFBDef(type_name=type_name)


        d.interface.event_inputs = [FBPort("EI_EXE", "EVENT", "IN", comment="execute")]
        event_outputs = [FBPort("EO_DONE", "EVENT", "OUT", comment="done")]
        if has_exc:
            event_outputs.append(FBPort("EO_EXC", "EVENT", "OUT", comment="exception"))
        d.interface.event_outputs = event_outputs


        data_ports: List[FBPort] = []
        device = self._get_device(instr)
        data_ports.append(FBPort("DI_device", "DATA", "IN", data_type="STRING", comment=device or ""))
        if getattr(instr, "exception_table_entry_id", None):
            data_ports.append(FBPort("DI_exception_entry", "DATA", "IN", data_type="STRING", comment=str(instr.exception_table_entry_id)))
        if getattr(instr, "exception_handler_address", None) is not None:
            data_ports.append(FBPort("DI_exception_handler", "DATA", "IN", data_type="INT", comment=str(instr.exception_handler_address)))
        if getattr(instr, "contingency_scenario_id", None):
            data_ports.append(FBPort("DI_scenario", "DATA", "IN", data_type="STRING", comment=str(instr.contingency_scenario_id)))
            data_ports.append(FBPort("DI_contingency_state", "DATA", "IN", data_type="STRING", comment=str(instr.contingency_state or "")))
            data_ports.append(FBPort("DI_contingency_start", "DATA", "IN", data_type="REAL", comment=str(instr.contingency_start)))
            data_ports.append(FBPort("DI_contingency_finish", "DATA", "IN", data_type="REAL", comment=str(instr.contingency_finish)))
            data_ports.append(FBPort("DI_contingency_deadline", "DATA", "IN", data_type="REAL", comment=str(instr.contingency_deadline)))
            data_ports.append(FBPort("DI_deadline_source", "DATA", "IN", data_type="STRING", comment=str(instr.contingency_deadline_source or "")))
        if getattr(instr, "assigned_workstation_id", None) is not None:
            data_ports.append(FBPort("DI_workstation", "DATA", "IN", data_type="INT", comment=str(instr.assigned_workstation_id)))
        if getattr(instr, "assigned_palu_id", None) is not None:
            data_ports.append(FBPort("DI_palu", "DATA", "IN", data_type="INT", comment=str(instr.assigned_palu_id)))


        rs, rt, rd = self._get_registers(instr, allocation)
        data_ports.append(FBPort("DI_rs", "DATA", "IN", data_type="INT", comment=str(rs)))
        data_ports.append(FBPort("DI_rt", "DATA", "IN", data_type="INT", comment=str(rt)))
        data_ports.append(FBPort("DI_rd", "DATA", "IN", data_type="INT", comment=str(rd)))


        data_ports.append(FBPort("DI_rp", "DATA", "IN", data_type="STRING", comment=instr.rp or ""))

        d.interface.data_inputs = data_ports


        d.ecc_states = [
            ECCState("START"),
            ECCState("RUN", algorithm="ALG_RUN"),
        ]
        transitions = [
            ECCTransition("START", "RUN", "EI_EXE"),
        ]
        if has_exc:
            # The executing device selects EO_DONE or EO_EXC at runtime.

            transitions.append(ECCTransition("RUN", "START", "EO_DONE"))
            transitions.append(ECCTransition("RUN", "START", "EO_EXC"))
        else:
            transitions.append(ECCTransition("RUN", "START", "EO_DONE"))
        d.ecc_transitions = transitions

        d.algorithms = [Algorithm("ALG_RUN", f"Execute {instr.op_type}")]
        return d

    def _make_branch_bfb_def(self, type_name: str, instr: BranchInstr, allocation: Dict[str, Any]) -> BasicFBDef:
        d = BasicFBDef(type_name=type_name)
        d.interface.event_inputs = [FBPort("EI_EXE", "EVENT", "IN")]
        d.interface.event_outputs = [
            FBPort("EO_TRUE", "EVENT", "OUT"),
            FBPort("EO_FALSE", "EVENT", "OUT"),
        ]
        cond_preg = 0
        if isinstance(instr.cond_var, str):
            preg = allocation.get(instr.cond_var, 0)
            if preg is not None and preg != "memory":
                try:
                    cond_preg = int(preg)
                except Exception:
                    pass
        d.interface.data_inputs = [
            FBPort("DI_cond_var", "DATA", "IN", data_type="STRING", comment=instr.cond_var or ""),
            FBPort("DI_cond_preg", "DATA", "IN", data_type="INT", comment=str(cond_preg)),
        ]
        d.ecc_states = [
            ECCState("START"),
            ECCState("EVAL", algorithm="ALG_EVAL"),
        ]
        d.ecc_transitions = [
            ECCTransition("START", "EVAL", "EI_EXE"),
            ECCTransition("EVAL", "START", "EO_TRUE"),
            ECCTransition("EVAL", "START", "EO_FALSE"),
        ]
        d.algorithms = [Algorithm("ALG_EVAL", "Evaluate condition")]
        return d


    def _is_vreg(self, name: str) -> bool:
        if not isinstance(name, str) or not name:
            return False
        if name.isdigit() or name.startswith('"'):
            return False
        if name in self._resource_names:
            return False
        parts = name.rsplit("_", 1)
        return len(parts) == 2 and parts[1].isdigit()

    def _material_arg_indices(self, op_type: str, argc: int):
        if op_type == "Process":
            return range(1, argc)
        if op_type == "Inspect":
            return [1] if argc >= 2 else []
        if op_type in ("Move", "Load", "Store"):
            return [0] if argc >= 1 else []
        if op_type == "Assign":
            return [0] if argc >= 1 else []
        return []

    def _get_device(self, instr: OpInstr) -> Optional[str]:
        assigned = getattr(instr, "assigned_device_id", None)
        if assigned:
            return str(assigned)
        if instr.op_type in ("Process", "Inspect", "Adjust"):
            if instr.args and instr.args[0] not in self._resource_names:
                return None
            if instr.args:
                return instr.args[0]
        return None

    def _get_registers(self, instr: OpInstr, allocation: Dict[str, Any]) -> Tuple[int, int, int]:
        rs = rt = rd = 0
        if instr.target and isinstance(instr.target, str):
            preg = getattr(instr, "assigned_preg", None)
            if preg is None:
                preg = allocation.get(instr.target, 0)
            if preg != "memory" and preg is not None:
                try:
                    rd = int(preg) & 0x1F
                except Exception:
                    rd = 0

        if instr.op_type == "Load":
            if len(instr.args) >= 2:
                arg2 = instr.args[1]
                if isinstance(arg2, str) and arg2.isdigit():
                    rt = int(arg2) & 0x1F
        elif instr.op_type == "Store":
            if len(instr.args) >= 2:
                arg2 = instr.args[1]
                if isinstance(arg2, str) and arg2.isdigit():
                    rs = int(arg2) & 0x1F
        elif instr.op_type in ("Move", "Assign"):
            if instr.args:
                vreg = instr.args[0]
                if isinstance(vreg, str) and vreg:
                    preg = getattr(instr, "assigned_preg", None)
                    if preg is None:
                        preg = allocation.get(vreg, 0)
                    if preg != "memory" and preg is not None:
                        try:
                            rs = int(preg) & 0x1F
                        except Exception:
                            rs = 0
                if len(instr.args) >= 2:
                    arg2 = instr.args[1]
                    if isinstance(arg2, str) and arg2.isdigit():
                        rt = int(arg2) & 0x1F
        else:
            idxs = self._material_arg_indices(instr.op_type, len(instr.args))
            regs = []
            for i in idxs:
                if i < len(instr.args):
                    vreg = instr.args[i]
                    if isinstance(vreg, str) and vreg:
                        preg = getattr(instr, "assigned_preg", None)
                        if preg is None:
                            preg = allocation.get(vreg, 0)
                        if preg != "memory" and preg is not None:
                            try:
                                regs.append(int(preg) & 0x1F)
                            except Exception:
                                pass
            if len(regs) >= 1:
                rs = regs[0]
            if len(regs) >= 2:
                rt = regs[1]
        return rs, rt, rd

    def _instr_fb_type_suffix(self, instr: Any) -> str:
        if isinstance(instr, BranchInstr):
            return "BRANCH"
        if isinstance(instr, OpInstr):
            return f"{instr.op_type}_EXC" if instr.catch_edges else instr.op_type
        return "NOP"

    def _block_has_exc_port(self, cfb_def: CompositeFBDef) -> bool:
        return any(p.name == "EO_EXC" for p in cfb_def.interface.event_outputs)
