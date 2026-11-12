import json
import time
from .target_model import TargetModelLoader

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.semantic import SemanticAnalyzer
from compiler.errors import CompilerError

from compiler.ir_builder import IRBuilder
from compiler.ssa import SSAPass
from compiler.visualizer import CFGVisualizer
from compiler.mdg_builder import MDGBuilder
from compiler.ir_opt import optimize_cfg
from compiler.scheduler_region import RegionScheduler

class CompilerProxy:
    """Compile FaLang programs against a factory target model."""

    def __init__(self, target_model_path: str = None):
        self.target_model = TargetModelLoader(target_model_path) if target_model_path \
            else TargetModelLoader()
        self.target_model.load()

        self.semantic_analyzer = SemanticAnalyzer(target_model=self.target_model)
        self.ir_builder = IRBuilder(target_model=self.target_model)
        self.ssa_pass = SSAPass(target_model=self.target_model)

    def _reset_frontend_state(self):
        """Reset symbols, block IDs, and SSA counters for the next compilation."""
        self.semantic_analyzer = SemanticAnalyzer(target_model=self.target_model)
        self.ir_builder = IRBuilder(target_model=self.target_model)
        self.ssa_pass = SSAPass(target_model=self.target_model)

    def compile_source(self, source_code, include_artifacts=False, emit_outputs=True,
                       schedule_state_override=None, device_last_model_override=None):
        """Compile source to FISA/COE and Function Block configuration.

        Runtime state and device setup overrides support just-in-time compilation."""
        results = {
            "success": False,
            "messages": [],
            "coe_data": []
        }
        cfg = None
        mdg = None
        schedule_result = None
        alloc_map = {}
        mdg_post_ra = None

        results["messages"].append(" Build started...")
        self._reset_frontend_state()

        try:
            lexer = Lexer(source_code)
            tokens = lexer.tokenize()
            results["messages"].append(f"Info: Lexical Analysis passed. Generated {len(tokens)} tokens.")

            parser = Parser(tokens)
            ast = parser.parse()
            results["messages"].append("Info: Syntax Analysis passed. AST constructed.")

            self.semantic_analyzer.analyze(ast)
            results["messages"].append("Info: Semantic Analysis passed. Physical constraints verified.")

            results["messages"].append("Info: Unrolling loops and flattening branch logics...")
            # Use Main when present; otherwise compile the first task.
            cfg = self.ir_builder.build(ast, entry_task="Main")
            results["messages"].append(f"Info: CFG built successfully with {len(cfg.blocks)} basic blocks.")

            opt_stats = optimize_cfg(cfg)
            if opt_stats.removed_redundant_loads or opt_stats.removed_redundant_moves:
                results["messages"].append(
                    "Info: IR optimizations applied: "
                    f"removed_loads={opt_stats.removed_redundant_loads}, "
                    f"removed_moves={opt_stats.removed_redundant_moves}."
                )

            results["messages"].append("Info: Converting IR to Static Single Assignment (SSA) form...")
            self.ssa_pass.convert(cfg, target_model=self.target_model)
            results["messages"].append("Info: SSA conversion complete. Ready for Factory DAG generation.")

            if emit_outputs:
                results["messages"].append("Info: Generating CFG visualization (DOT + SVG + HTML)...")
                inter_success, html_path = CFGVisualizer.export_interactive_html(cfg, "factory_ssa_cfg.html", "factory_ssa_cfg.dot")
                if inter_success:
                    results["messages"].append(f"Info: CFG visualization HTML generated at {html_path}")
                    results["messages"].append("Info: CFG visualization SVG generated at factory_ssa_cfg.svg")
                else:
                    results["messages"].append(f"Warning: Failed to generate CFG visualization: {html_path}")
            else:
                results["messages"].append("Info: CFG visualization skipped for simulation run.")

            results["messages"].append("Info: Eliminating Phi nodes...")
            from compiler.phi_elim import PhiElimination
            phi_elim = PhiElimination()
            phi_elim.eliminate(cfg)
            results["messages"].append("Info: Phi elimination complete.")

            # Remove redundant moves introduced by Phi elimination.
            opt_stats_post = optimize_cfg(cfg)
            if opt_stats_post.removed_redundant_loads or opt_stats_post.removed_redundant_moves:
                results["messages"].append(
                    "Info: Post-phi optimizations applied: "
                    f"removed_loads={opt_stats_post.removed_redundant_loads}, "
                    f"removed_moves={opt_stats_post.removed_redundant_moves}."
                )

            results["messages"].append("Info: Building Manufacturing Dependency Graph (MDG)...")
            try:
                mdg_builder = MDGBuilder(target_model=self.target_model)
                mdg = mdg_builder.build(cfg)
                results["messages"].append(f"Info: MDG built with {len(mdg.nodes)} nodes and {len(mdg.edges)} edges.")

                import subprocess as _sub
                if emit_outputs:
                    dot_path = "factory_mdg.dot"
                    with open(dot_path, "w", encoding="utf-8") as f:
                        f.write(mdg.to_dot())
                    results["messages"].append(f"Info: MDG DOT generated at {dot_path}")

                    try:
                        out_name = "factory_mdg.svg"
                        proc = _sub.run(
                            ["dot", "-Tsvg", dot_path],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
                        )
                        with open(out_name, "w", encoding="utf-8") as f:
                            f.write(proc.stdout)
                        results["messages"].append(f"Info: MDG SVG generated at {out_name}")
                    except Exception as dot_err:
                        results["messages"].append(f"Warning: Failed to generate MDG SVG: {dot_err}")
            except Exception as mdg_err:
                results["messages"].append(f"Warning: MDG generation skipped: {mdg_err}")

            results["messages"].append("Info: Running instruction scheduling...")
            try:
                region_scheduler = RegionScheduler(target_model=self.target_model)
                schedule_result = region_scheduler.schedule(
                    cfg, mdg,
                    initial_state_override=schedule_state_override,
                    device_last_model_override=device_last_model_override,
                )
                results["messages"].append(
                    f"Info: Scheduling complete. Makespan={schedule_result.global_makespan:.1f}, "
                    f"Regions={len(schedule_result.mfr_boundaries)}"
                )
                for lvl, peak in schedule_result.cache_peak.items():
                    results["messages"].append(f"Info: Cache peak occupancy {lvl}={peak}")
            except Exception as sched_err:
                results["messages"].append(f"Warning: Scheduling skipped: {sched_err}")

            results["messages"].append("Info: Running register allocation (PBQP)...")
            try:
                from compiler.register_alloc import RegisterAllocator
                reg_alloc = RegisterAllocator(cfg, mdg, schedule_result, self.target_model)
                alloc_map = reg_alloc.allocate()
                results["messages"].append(
                    f"Info: Register allocation complete. {len(alloc_map)} material versions assigned."
                )
            except Exception as ra_err:
                results["messages"].append(f"Warning: Register allocation skipped: {ra_err}")

            results["messages"].append("Info: Running post-register-allocation scheduling...")
            try:
                from compiler.post_ra_scheduler import PostRAScheduler
                post_ra = PostRAScheduler(self.target_model)
                post_ra.schedule(cfg, alloc_map)
                results["messages"].append("Info: Post-RA scheduling complete.")
            except Exception as pr_err:
                results["messages"].append(f"Warning: Post-RA scheduling skipped: {pr_err}")

            results["messages"].append("Info: Building exception table and landing pads...")
            try:
                from compiler.exception_table import ExceptionTableBuilder
                exc_entries = ExceptionTableBuilder().build(cfg)
                results["messages"].append(
                    f"Info: Exception table built with {len(exc_entries)} entries."
                )
            except Exception as exc_err:
                results["messages"].append(f"Warning: Exception table generation skipped: {exc_err}")

            # Catch paths receive execution plans without reserving normal-path resources.
            results["messages"].append("Info: Planning catch contingency schedules...")
            try:
                from compiler.catch_contingency import CatchContingencyPlanner
                catch_planner = CatchContingencyPlanner(self.target_model)
                catch_results = catch_planner.plan(
                    cfg,
                    locals().get("schedule_result"),
                    locals().get("alloc_map", {}),
                )
                if catch_results:
                    for cr in catch_results:
                        level = "Warning" if cr.state != "scheduled" else "Info"
                        results["messages"].append(f"{level}: {cr.message}")
                else:
                    results["messages"].append("Info: No catch contingency scenarios found.")
            except Exception as cc_err:
                results["messages"].append(f"Warning: Catch contingency planning fell back to runtime deferred mode: {cc_err}")

            results["messages"].append("Info: Rebuilding MDG after Post-RA scheduling...")
            try:
                mdg_builder_post = MDGBuilder(target_model=self.target_model)
                mdg_post_ra = mdg_builder_post.build(cfg)

                if emit_outputs:
                    dot_path_post = "factory_mdg_post_ra.dot"
                    with open(dot_path_post, "w", encoding="utf-8") as f:
                        f.write(mdg_post_ra.to_dot())
                    results["messages"].append(f"Info: Post-RA MDG DOT generated at {dot_path_post}")

                    try:
                        out_name_post = "factory_mdg_post_ra.svg"
                        proc = _sub.run(
                            ["dot", "-Tsvg", dot_path_post],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
                        )
                        with open(out_name_post, "w", encoding="utf-8") as f:
                            f.write(proc.stdout)
                        results["messages"].append(f"Info: Post-RA MDG SVG generated at {out_name_post}")
                    except Exception as dot_err:
                        results["messages"].append(f"Warning: Failed to generate Post-RA MDG SVG: {dot_err}")
            except Exception as mdg_err:
                results["messages"].append(f"Warning: Post-RA MDG generation skipped: {mdg_err}")

            results["messages"].append("Info: Generating Function Block configuration...")
            try:
                from compiler.fb_config.fb_builder import FBConfigBuilder
                from compiler.fb_config.fb_exporter import FBExporter
                fb_alloc_map = locals().get("alloc_map", {})
                fb_builder = FBConfigBuilder(self.target_model)
                fb_network = fb_builder.build(cfg, fb_alloc_map)
                if emit_outputs:
                    FBExporter.export_json(fb_network, "factory_fb_config.json")
                    FBExporter.export_iec61499_xml(fb_network, "factory_fb_config.xml")
                n_inst = len(fb_network.top_level.instances)
                results["messages"].append(
                    f"Info: FB config generated: {n_inst} CFB instances, "
                    f"{len(fb_network.basic_fb_types)} BFB types."
                )
                if emit_outputs:
                    results["messages"].append("Info: factory_fb_config.json / .xml exported.")
                else:
                    results["messages"].append("Info: FB export skipped for simulation run.")
            except Exception as fb_err:
                results["messages"].append(f"Warning: FB configuration skipped: {fb_err}")

            results["messages"].append("Info: Generating Physical Instructions...")
            try:
                from compiler.codegen import CodeGenerator
                codegen = CodeGenerator(self.target_model)
                machine_codes = codegen.generate(cfg, alloc_map)
                results["text_data"] = machine_codes
                results["coe_data"] = getattr(codegen, "coe_image", None) or machine_codes
                results["exception_table"] = getattr(codegen, "exception_table", [])
                results["success"] = True
                results["messages"].append("Compilation Successful.")
                results["messages"].append(f"Generated {len(machine_codes)} FISA instructions.")
                coe_image = getattr(codegen, "coe_image", None) or {}
                results["messages"].append(
                    f"Segmented Memory Initialization Vector created: .text={len(machine_codes)}, "
                    f".exctab={len(coe_image.get('exctab', []))}, .meta={len(coe_image.get('meta', []))}."
                )
            except Exception as cg_err:
                results["messages"].append(f"Warning: Code generation failed: {cg_err}")
                results["coe_data"] = []
                results["success"] = False

        except CompilerError as e:
            results["messages"].append(f"Compiler Error: {str(e)}")
            results["success"] = False
        except Exception as e:
            results["messages"].append(f"Compiler Internal Error: {str(e)}")
            results["success"] = False

        if include_artifacts:
            results["artifacts"] = {
                "cfg": cfg,
                "mdg": mdg,
                "schedule_result": schedule_result,
                "alloc_map": alloc_map,
                "mdg_post_ra": mdg_post_ra,
            }

        return results

    def compile_source_lightweight(
        self,
        source_code,
        schedule_state_override=None,
        device_last_model_override=None,
        allocation_mode="pbqp",
        enable_try_catch=True,
        skip_pre_ra=False,
    ):
        """Compile through Post-RA scheduling for just-in-time planning."""
        results = {"success": False, "messages": [], "artifacts": {}}
        self._reset_frontend_state()

        try:
            from compiler.lexer import Lexer
            from compiler.parser import Parser
            from compiler.ir_builder import IRBuilder
            from compiler.ssa import SSAPass
            from compiler.phi_elim import PhiElimination
            from compiler.ir_opt import optimize_cfg
            from compiler.mdg_builder import MDGBuilder
            from compiler.scheduler_region import RegionScheduler
            from compiler.register_alloc import RegisterAllocator
            from compiler.post_ra_scheduler import PostRAScheduler

            lexer = Lexer(source_code)
            tokens = lexer.tokenize()
            parser = Parser(tokens)
            ast = parser.parse()
            self.semantic_analyzer.analyze(ast)
            cfg = self.ir_builder.build(ast, entry_task="Main")
            optimize_cfg(cfg)
            self.ssa_pass.convert(cfg, target_model=self.target_model)

            phi_elim = PhiElimination()
            phi_elim.eliminate(cfg)
            optimize_cfg(cfg)

            mdg_builder = MDGBuilder(target_model=self.target_model)
            mdg = mdg_builder.build(cfg)

            if skip_pre_ra:
                # The allocator derives lifetimes from the MDG when no pre-RA schedule exists.
                from compiler.scheduler import ScheduleResult
                schedule_result = ScheduleResult(
                    block_schedules={},
                    global_makespan=0.0,
                    device_timeline={},
                    material_timeline={},
                    cache_peak={},
                    mfr_boundaries=[],
                    exit_states={},
                )
            else:
                region_scheduler = RegionScheduler(target_model=self.target_model)
                schedule_result = region_scheduler.schedule(
                    cfg, mdg,
                    initial_state_override=schedule_state_override,
                    device_last_model_override=device_last_model_override,
                )

            reg_alloc = RegisterAllocator(cfg, mdg, schedule_result, self.target_model)
            if allocation_mode == "pbqp":
                alloc_map = reg_alloc.allocate()
            elif allocation_mode == "graph_coloring":
                alloc_map = reg_alloc.allocate_graph_coloring()
            elif allocation_mode == "fast":
                alloc_map = reg_alloc.allocate_fast()
            else:
                raise ValueError(f"Unknown allocation_mode: {allocation_mode}")

            post_ra = PostRAScheduler(self.target_model)
            post_ra.schedule(cfg, alloc_map)

            exc_entries = []
            catch_scenarios = []
            if enable_try_catch:
                from compiler.exception_table import ExceptionTableBuilder
                exc_entries = ExceptionTableBuilder().build(cfg)

                from compiler.catch_contingency import CatchContingencyPlanner
                catch_planner = CatchContingencyPlanner(self.target_model)
                catch_scenarios = catch_planner.plan(cfg, schedule_result, alloc_map)

            results["success"] = True
            results["artifacts"] = {
                "cfg": cfg,
                "mdg": mdg,
                "schedule_result": schedule_result,
                "alloc_map": alloc_map,
                "exception_table": exc_entries,
                "catch_contingency": catch_scenarios,
            }
            results["messages"].append("Lightweight compilation complete.")
        except Exception as e:
            results["messages"].append(f"Lightweight compilation failed: {e}")

        return results

    def compile_order_program(
        self,
        source_code,
        f_t=None,
        factory_state=None,
        residual_vregs=None,
        pinned_locations=None,
        emit_outputs=False,
    ):
        """Compile an order representation, lower it to the target model, and color its residual workset."""
        from compiler.ir import ControlFlowGraph  # noqa: F401

        results = {"success": False, "messages": [], "artifacts": {}}
        cfg = None
        mdg = None
        lowering = None
        coloring = None
        alloc_map = {}
        workset = None

        try:
            f_t = f_t if f_t is not None else self.build_factory_target_machine(factory_state)

            lexer = Lexer(source_code)
            tokens = lexer.tokenize()
            parser = Parser(tokens)
            ast = parser.parse()
            results["messages"].append(f"Info: frontend parsed {len(tokens)} tokens.")

            from compiler.semantic import SemanticAnalyzer
            logical_analyzer = SemanticAnalyzer(target_model=None, logical_only=True)
            logical_analyzer.analyze(ast)
            results["messages"].append("Info: logical semantic analysis passed.")

            from compiler.vmrs import VirtualResourceSpace, attach_to_cfg
            vmrs = VirtualResourceSpace.from_ast(ast)
            results["messages"].append(f"Info: {vmrs.summary()}")

            from compiler.ir_builder import IRBuilder
            from compiler.ir_opt import optimize_cfg
            builder = IRBuilder(target_model=self.target_model, defer_target_codes=True)
            cfg = builder.build(ast, entry_task="Main")
            attach_to_cfg(cfg, vmrs)
            optimize_cfg(cfg)
            results["messages"].append(f"Info: CFG built with {len(cfg.blocks)} basic blocks (target-independent).")

            from compiler.ssa import SSAPass
            from compiler.phi_elim import PhiElimination
            SSAPass().convert(cfg)
            PhiElimination().eliminate(cfg)
            optimize_cfg(cfg)
            results["messages"].append("Info: SSA conversion and phi elimination complete.")

            from compiler.target_lowering import TargetLowering
            lowering = TargetLowering(f_t, vmrs).lower(cfg)
            for message in lowering.diagnostics:
                results["messages"].append(f"Warning: {message}")
            results["messages"].append(
                f"Info: target lowering bound {len(lowering.operations)} instructions to F_t."
            )

            from compiler.mdg_builder import MDGBuilder
            mdg = MDGBuilder(target_model=self.target_model).build(cfg)
            results["messages"].append(f"Info: MDG built with {len(mdg.nodes)} nodes and {len(mdg.edges)} edges.")

            if residual_vregs is not None or pinned_locations is not None:
                from compiler.residual_ir import ResidualWorkset
                workset = ResidualWorkset(
                    pending_vregs=set(residual_vregs or ()),
                    pinned_locations=dict(pinned_locations or {}),
                )
                results["messages"].append(f"Info: residual workset = {workset.snapshot()}")

            from compiler.residual_coloring import ResidualGraphColorer
            colorer = ResidualGraphColorer(
                cfg, mdg, f_t, lowering,
                state=factory_state,
                residual_vregs=residual_vregs,
                pinned_locations=pinned_locations,
            )
            outcome = colorer.run(time=getattr(factory_state, "time", 0.0) if factory_state else 0.0)
            alloc_map = outcome.allocation
            results["messages"].append(
                f"Info: residual graph coloring assigned {len(alloc_map)} material versions "
                f"over {len(outcome.rounds)} round(s); waiting={len(outcome.waiting)}."
            )

            from compiler.exception_table import ExceptionTableBuilder
            exc_entries = ExceptionTableBuilder().build(cfg)
            results["messages"].append(f"Info: exception table built with {len(exc_entries)} entries.")

            from compiler.catch_contingency import CatchContingencyPlanner
            catch_planner = CatchContingencyPlanner(self.target_model)
            catch_scenarios = catch_planner.plan(cfg, None, alloc_map)
            results["messages"].append(
                f"Info: catch contingency planned {len(catch_scenarios)} scenario(s)."
            )

            from compiler.regional_residual import PrecompiledExceptionHandler
            from compiler.precompiled_binding import compile_handler_bindings
            handler = PrecompiledExceptionHandler(cfg, exc_entries, catch_scenarios)
            cfg.precompiled_handler_bindings = compile_handler_bindings(handler, f_t, lowering)

            results["artifacts"] = {
                "cfg": cfg,
                "mdg": mdg,
                "vmrs": vmrs,
                "f_t": f_t,
                "lowering": lowering,
                "coloring": outcome,
                "alloc_map": alloc_map,
                "exception_table": exc_entries,
                "catch_contingency": catch_scenarios,
                "workset": workset,
            }
            results["success"] = True
            results["messages"].append("Paper 1 compilation complete.")
        except CompilerError as error:
            results["messages"].append(f"Compiler Error: {error}")
        except Exception as error:  # noqa: BLE001
            results["messages"].append(f"Paper 1 compilation failed: {type(error).__name__}: {error}")

        return results

    def build_factory_target_machine(self, factory_state=None):
        from compiler.factory_target_machine import FactoryTargetMachine
        return FactoryTargetMachine(self.target_model, factory_state)

    def generate_coe_file(self, filename, hex_data):
        try:
            if isinstance(hex_data, dict):
                words = list(hex_data.get("flat") or [])
            else:
                words = list(hex_data or [])
            with open(filename, 'w') as f:
                f.write("memory_initialization_radix = 16;\n")
                f.write("memory_initialization_vector =\n")
                for i, code in enumerate(words):
                    suffix = ";" if i == len(words) - 1 else ","
                    f.write(f"{code}{suffix}\n")
            if isinstance(hex_data, dict):
                return True, f"Saved segmented FISA COE to {filename}"
            return True, f"Saved to {filename}"
        except Exception as e:
            return False, str(e)
