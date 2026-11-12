import json
import time
# from.target_model import TargetModelLoader
from .target_model import TargetModelLoader

# 引入扩展目录中的 FaLang 编译器前端组件
# 使用绝对导入，以便直接通过 python main.py 启动时不会出现相对导入错误。
# 由于脚本是从 F_IDE_Project 目录下执行，
# 顶层模块名为 compiler 而不是 F_IDE_Project.compiler。
from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.semantic import SemanticAnalyzer
from compiler.errors import CompilerError

# --- [新增] 引入中端组件 (IR构建 与 SSA转换) ---
from compiler.ir_builder import IRBuilder
from compiler.ssa import SSAPass
from compiler.visualizer import CFGVisualizer
from compiler.mdg_builder import MDGBuilder
from compiler.ir_opt import optimize_cfg
from compiler.scheduler_region import RegionScheduler

class CompilerProxy:
    """
    F-Compiler 的前端代理接口。
    
    功能：
    1. 接收 F-Lang 源代码。
    2. 执行正式的词法、语法、语义分析。
    3. 生成带 SSA 形式的中间表示 (IR)。
    4. 生成符合规范的 FISA 机器码 (Hex格式)。
    5. 输出 COE 文件内容用于 FPGA 初始化。
    """
    
    def __init__(self, target_model_path: str = None):
        # target_model_path 允许同一套编译流水线服务不同的制造 domain：
        # 换一个目标机模型即可，编译器本身不需要改动。
        self.target_model = TargetModelLoader(target_model_path) if target_model_path \
            else TargetModelLoader()
        self.target_model.load() # 加载工厂目标机模型
        
        # 实例化语义分析器及中端组件
        self.semantic_analyzer = SemanticAnalyzer(target_model=self.target_model)
        self.ir_builder = IRBuilder(target_model=self.target_model)
        self.ssa_pass = SSAPass(target_model=self.target_model)

    def _reset_frontend_state(self):
        """
        每次编译前重置前端状态。

        SemanticAnalyzer 的符号表、IRBuilder 的块编号与内联计数、SSAPass 的版本计数器
        都保存在实例上。同一个 CompilerProxy 连续编译第二份源码时，若不清空这些状态，
        第二次编译会因为"资源重复定义"直接失败——state-aware JIT 的重编译正是这种情形。
        """
        self.semantic_analyzer = SemanticAnalyzer(target_model=self.target_model)
        self.ir_builder = IRBuilder(target_model=self.target_model)
        self.ssa_pass = SSAPass(target_model=self.target_model)

    def compile_source(self, source_code, include_artifacts=False, emit_outputs=True,
                       schedule_state_override=None, device_last_model_override=None):
        """
        执行编译过程。
        输入: F-Lang 源代码字符串
        可选参数:
          - schedule_state_override: dict，运行时状态覆盖（用于state-aware JIT）
          - device_last_model_override: dict，设备最后model追踪（用于setup计算）
        输出: 包含 success, messages, coe_data 的字典
        """
        results = {
            "success": False,
            #"messages":,
            #"coe_data":
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
            # 1. 词法分析阶段 (Lexical Analysis)
            lexer = Lexer(source_code)
            tokens = lexer.tokenize()
            results["messages"].append(f"Info: Lexical Analysis passed. Generated {len(tokens)} tokens.")
            
            # 2. 语法分析阶段 (Syntax Analysis)
            parser = Parser(tokens)
            ast = parser.parse()
            results["messages"].append("Info: Syntax Analysis passed. AST constructed.")
            
            # 3. 语义分析阶段 (Semantic Analysis)
            # 根据目标机模型进行物理约束和能力验证
            self.semantic_analyzer.analyze(ast)
            results["messages"].append("Info: Semantic Analysis passed. Physical constraints verified.")
            
            # === [新增] 中端编译阶段 ===
            # 4. 构建控制流图 (IR & CFG Construction)
            results["messages"].append("Info: Unrolling loops and flattening branch logics...")
            # Default entry: Task Main(...) if present, else first task.
            cfg = self.ir_builder.build(ast, entry_task="Main")
            results["messages"].append(f"Info: CFG built successfully with {len(cfg.blocks)} basic blocks.")

            # 4.5. Machine-independent IR optimizations (before SSA / target-model weighting)
            opt_stats = optimize_cfg(cfg)
            if opt_stats.removed_redundant_loads or opt_stats.removed_redundant_moves:
                results["messages"].append(
                    "Info: IR optimizations applied: "
                    f"removed_loads={opt_stats.removed_redundant_loads}, "
                    f"removed_moves={opt_stats.removed_redundant_moves}."
                )
            
            # 5. 静态单赋值转换 (SSA Conversion)
            results["messages"].append("Info: Converting IR to Static Single Assignment (SSA) form...")
            self.ssa_pass.convert(cfg, target_model=self.target_model)
            results["messages"].append("Info: SSA conversion complete. Ready for Factory DAG generation.")

            # --- [新增] 可视化 CFG 导出 (SSA 之后，Phi 消除之前) ---
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
            # ==========================

            # 5.5 Phi 消除（物理移动插入法）
            results["messages"].append("Info: Eliminating Phi nodes...")
            from compiler.phi_elim import PhiElimination
            phi_elim = PhiElimination()
            phi_elim.eliminate(cfg)
            results["messages"].append("Info: Phi elimination complete.")

            # 5.6 对 Phi 消除引入的 Assign 进行冗余消除
            opt_stats_post = optimize_cfg(cfg)
            if opt_stats_post.removed_redundant_loads or opt_stats_post.removed_redundant_moves:
                results["messages"].append(
                    "Info: Post-phi optimizations applied: "
                    f"removed_loads={opt_stats_post.removed_redundant_loads}, "
                    f"removed_moves={opt_stats_post.removed_redundant_moves}."
                )

            # --- [新增] MDG 构建与导出 (Phi 消除之后) ---
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
            # ==========================

            # --- [新增] 前寄存器分配调度 (Intra-block List + Optional Region Scheduling) ---
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
            # ==========================

            # --- [新增] PBQP 寄存器分配（工位感知存储分配） ---
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
            # ==========================

            # --- [新增] 后寄存器分配调度（Post-RA Scheduling） ---
            results["messages"].append("Info: Running post-register-allocation scheduling...")
            try:
                from compiler.post_ra_scheduler import PostRAScheduler
                post_ra = PostRAScheduler(self.target_model)
                post_ra.schedule(cfg, alloc_map)
                results["messages"].append("Info: Post-RA scheduling complete.")
            except Exception as pr_err:
                results["messages"].append(f"Warning: Post-RA scheduling skipped: {pr_err}")
            # ==========================

            # --- 零成本异常表 + Landing Pad 元数据 ---
            results["messages"].append("Info: Building exception table and landing pads...")
            try:
                from compiler.exception_table import ExceptionTableBuilder
                exc_entries = ExceptionTableBuilder().build(cfg)
                results["messages"].append(
                    f"Info: Exception table built with {len(exc_entries)} entries."
                )
            except Exception as exc_err:
                results["messages"].append(f"Warning: Exception table generation skipped: {exc_err}")
            # ==========================

            # --- Catch 补偿延迟计划：异常路径不预留风险窗口资源，但必须生成明确执行语义 ---
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
            # ==========================

            # --- [新增] Post-RA 调度后重新构建 MDG 并导出可视化 ---
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
            # ==========================

            # --- [新增] FB 配置生成（Post-RA 后，与代码生成并行独立） ---
            results["messages"].append("Info: Generating Function Block configuration...")
            try:
                from compiler.fb_config.fb_builder import FBConfigBuilder
                from compiler.fb_config.fb_exporter import FBExporter
                # alloc_map 可能因寄存器分配失败而未定义，安全获取
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
            # ==========================

            # 6. 代码生成阶段 (Code Generation)
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
            # 捕获编译器前端抛出的词法、语法或语义错误
            results["messages"].append(f"Compiler Error: {str(e)}")
            results["success"] = False
        except Exception as e:
            # 捕获其他未知内部异常
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
        """
        轻量级编译：仅执行到 Post-RA 调度，跳过代码生成、FB 配置、异常表等。
        用于 state-aware JIT compilation 的快速路径。
        """
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
                # 跳过 Pre-RA 调度：创建空的 ScheduleResult，寄存器分配器从 MDG 拓扑计算生命期
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

            # 寄存器分配
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
        """
        Paper 1 流水线：from planning to programming。

        阶段序列：
          1. 词法 / 语法分析
          2. 语义分析（logical-only：只验证订单自身的逻辑语义，不查目标机）
          3. VMRS / VMAS 逻辑资源角色提取
          4. 目标无关 IR 与 CFG（不写入 opcode/func/rp）
          5. SSA 版本化 + Phi 消除
          6. Target Lowering：在 Factory Target Machine Model F_t 上完成动作编码解析、
             资源可行性校验，并算出每个物料版本的候选物理持有位置
          7. MDG 构建（为占位区间提供 ASAP 传播）
          8. 异常表与 catch 补偿延迟规划（U1 的预编译异常处理基础）
          9. Residual Graph Coloring（只判可行、不比较代价；无 spill、无 pre-RA 调度）

        本流水线**不调用** Pre-RA list scheduling、PBQP 分配与 Post-RA scheduling，
        也**不会**把物料版本落到 warehouse。
        """
        from compiler.ir import ControlFlowGraph  # noqa: F401  （保持导入一致性）

        results = {"success": False, "messages": [], "artifacts": {}}
        cfg = None
        mdg = None
        lowering = None
        coloring = None
        alloc_map = {}
        workset = None

        try:
            f_t = f_t if f_t is not None else self.build_factory_target_machine(factory_state)

            # 1. 词法 / 语法
            lexer = Lexer(source_code)
            tokens = lexer.tokenize()
            parser = Parser(tokens)
            ast = parser.parse()
            results["messages"].append(f"Info: frontend parsed {len(tokens)} tokens.")

            # 2. 逻辑语义（不查目标机）
            from compiler.semantic import SemanticAnalyzer
            logical_analyzer = SemanticAnalyzer(target_model=None, logical_only=True)
            logical_analyzer.analyze(ast)
            results["messages"].append("Info: logical semantic analysis passed.")

            # 3. VMRS / VMAS
            from compiler.vmrs import VirtualResourceSpace, attach_to_cfg
            vmrs = VirtualResourceSpace.from_ast(ast)
            results["messages"].append(f"Info: {vmrs.summary()}")

            # 4. 目标无关 IR
            from compiler.ir_builder import IRBuilder
            from compiler.ir_opt import optimize_cfg
            builder = IRBuilder(target_model=self.target_model, defer_target_codes=True)
            cfg = builder.build(ast, entry_task="Main")
            attach_to_cfg(cfg, vmrs)
            optimize_cfg(cfg)
            results["messages"].append(f"Info: CFG built with {len(cfg.blocks)} basic blocks (target-independent).")

            # 5. SSA + Phi 消除（目标无关：SSA 只需知道哪些操作会改写物料）
            from compiler.ssa import SSAPass
            from compiler.phi_elim import PhiElimination
            SSAPass().convert(cfg)
            PhiElimination().eliminate(cfg)
            optimize_cfg(cfg)
            results["messages"].append("Info: SSA conversion and phi elimination complete.")

            # 6. Target Lowering
            from compiler.target_lowering import TargetLowering
            lowering = TargetLowering(f_t, vmrs).lower(cfg)
            for message in lowering.diagnostics:
                results["messages"].append(f"Warning: {message}")
            results["messages"].append(
                f"Info: target lowering bound {len(lowering.operations)} instructions to F_t."
            )

            # 7. MDG
            from compiler.mdg_builder import MDGBuilder
            mdg = MDGBuilder(target_model=self.target_model).build(cfg)
            results["messages"].append(f"Info: MDG built with {len(mdg.nodes)} nodes and {len(mdg.edges)} edges.")

            # 8. 残余工作集（可选：由 Workset Composer 提供）
            if residual_vregs is not None or pinned_locations is not None:
                from compiler.residual_ir import ResidualWorkset
                workset = ResidualWorkset(
                    pending_vregs=set(residual_vregs or ()),
                    pinned_locations=dict(pinned_locations or {}),
                )
                results["messages"].append(f"Info: residual workset = {workset.snapshot()}")

            # 9. Residual Graph Coloring
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

            # 10. 异常表 + catch 补偿延迟计划（U1 预编译异常处理的基础，始终保留）
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
        except Exception as error:  # noqa: BLE001 - 与 compile_source 一致的兜底
            results["messages"].append(f"Paper 1 compilation failed: {type(error).__name__}: {error}")

        return results

    def build_factory_target_machine(self, factory_state=None):
        """按当前目标机模型构造 F_t。"""
        from compiler.factory_target_machine import FactoryTargetMachine
        return FactoryTargetMachine(self.target_model, factory_state)

    def generate_coe_file(self, filename, hex_data):
        """将机器码写入.coe 文件"""
        try:
            if isinstance(hex_data, dict):
                words = list(hex_data.get("flat") or [])
            else:
                words = list(hex_data or [])
            with open(filename, 'w') as f:
                f.write("memory_initialization_radix = 16;\n")
                f.write("memory_initialization_vector =\n")
                # 格式化输出，每行一个指令，最后一行用分号
                for i, code in enumerate(words):
                    suffix = ";" if i == len(words) - 1 else ","
                    f.write(f"{code}{suffix}\n")
            if isinstance(hex_data, dict):
                return True, f"Saved segmented FISA COE to {filename}"
            return True, f"Saved to {filename}"
        except Exception as e:
            return False, str(e)
