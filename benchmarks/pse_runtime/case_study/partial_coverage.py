"""Observe physical execution and responses under the shared AHU uncertainty generator."""
from __future__ import annotations

from dataclasses import asdict, replace
from functools import partial
from hashlib import sha256
from math import isfinite
from statistics import mean
from unittest.mock import patch
import time

from case_study import scenario as grid
from case_study import study2
from case_study.statistics import percentile
from case_study.records import Study2Row
from case_study.response_recovery import RecoveryTracker
from case_study.completion_observer import collect_completion_records, evaluate_due_factor
from case_study.domains.spec import OPCODE_FUNC_BY_OP_TYPE
from compiler.regional_residual import UncertaintyCoordinator, RegionalResidualRecompilation
from compiler.factory_target_machine import AVAIL_WORKSTATION, AVAIL_LOGISTICS, AVAIL_HOLDING
from compiler.uncertainty import (
    UncertaintyEvent, U_MACHINE_FAILURE, U_BUFFER_UNAVAILABLE, U_QUALITY_FAULT,
    U_MATERIAL_SHORTAGE, U_TOOLING_DEVIATION,
    TARGET_WORKSTATION, TARGET_HOLDING, TARGET_MATERIAL,
)


def response_path(attributes):
    if not attributes.anticipated:
        return "unanticipated"
    if not attributes.executable_at_trigger:
        return "inexecutable"
    return "contained" if attributes.contained else "partial"


def _transport_distance_summary(trips):
    """坐标缺失意味着总距离未知，不能把缺失路段当零距离。"""
    unknown = sum(trip["distance_steps"] is None for trip in trips)
    return dict(actual_transport_unknown_distance_trips=unknown,
                actual_transport_distance_steps=(None if unknown else
                    sum(trip["distance_steps"] for trip in trips)))


def _startup_operation_filter(candidate):
    """可选的机器启动风险工序清单；不增添 MachineFailure 触发能力。"""
    if "startup_failure_operations" not in candidate:
        return None
    if "startup_failure_probability" not in candidate:
        raise ValueError("startup_failure_operations requires startup_failure_probability")
    value = candidate["startup_failure_operations"]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("startup_failure_operations must be a nonempty list of (op_type, rp)")
    result = set()
    for entry in value:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or not isinstance(entry[0], str) or entry[0] not in OPCODE_FUNC_BY_OP_TYPE
                or not isinstance(entry[1], str) or len(entry[1]) != 5
                or set(entry[1]) - {"0", "1"}):
            raise ValueError("startup_failure_operations entries require a known op_type and a 5-bit string rp")
        result.add(tuple(entry))
    return frozenset(result)


def _buffer_operation_filter(candidate):
    """接收/夹具检查仅预抽声明工序，不依赖机器风险或处理器覆盖。"""
    key = "dispatch_buffer_failure_operations"
    if key not in candidate:
        return None
    if "dispatch_buffer_failure_probability" not in candidate:
        raise ValueError(key + " requires dispatch_buffer_failure_probability")
    value = candidate[key]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(key + " must be a nonempty list of (op_type, rp)")
    result = set()
    for entry in value:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or not isinstance(entry[0], str) or entry[0] not in {"Process", "Inspect"}
                or not isinstance(entry[1], str) or len(entry[1]) != 5
                or set(entry[1]) - {"0", "1"}):
            raise ValueError(key + " entries require Process/Inspect and a 5-bit string rp")
        result.add(tuple(entry))
    return frozenset(result)


def _backup_channel_status(item, op, target_station, factory, time):
    """检查同能力备用的机器与合格接收库位；繁忙/占用不等于资源停用。"""
    lowered = item.lowering.operations[id(op)]
    alternatives = sorted(set(lowered.candidate_workstations) - {target_station})
    output_sites = [set(item.lowering.eligible_sites[v]) for v in study2._op_defs(op)
                    if v in item.lowering.eligible_sites]
    qualified = set.intersection(*output_sites) if output_sites else None
    channels = []
    for station in alternatives:
        sites = set(factory.holding_sites_of_workstation(station))
        if qualified is not None:
            sites &= qualified
        available = sorted(site for site in sites if factory.is_available(AVAIL_HOLDING, site, time))
        machine_available = factory.is_available(AVAIL_WORKSTATION, station, time)
        channels.append(dict(workstation=station, machine_available=machine_available,
                             qualified_holding_sites=sorted(sites), available_holding_sites=available,
                             available=bool(machine_available and available)))
    eligible = any(channel["available"] for channel in channels)
    return dict(eligible=eligible, channels=channels,
                reason="backup_channel_available" if eligible else
                       "no_available_backup_channel" if alternatives else "no_alternative_workstation")


def _record_backup_check(opportunity, status, time):
    opportunity.update(backup_channel_checked=True, backup_channel_check_time=time,
                       backup_channel_eligible=status["eligible"],
                       backup_channel_suppressed=not status["eligible"],
                       backup_channel_reason=status["reason"], backup_channels=status["channels"])
    if not status["eligible"]:
        opportunity["suppressed_reason"] = status["reason"]


class DispatchBufferRisk:
    """每道加工/检验工序一次独立缓存启动风险，不读取方法或处理器状态。

    startup_failure 是复用既有绑定钩子的通用开工标记，不代表机器故障类别。
    机器/缓存次数分别读取 startup_machine_failure / dispatch_buffer_failure。
    """

    def __init__(self, probability, repair_delay, seed, requires_transfer=True):
        if (type(probability) not in (int, float) or not isfinite(probability)
                or not 0 <= probability <= 1):
            raise ValueError("dispatch_buffer_failure_probability must be finite and in [0,1]")
        if (not isinstance(repair_delay, (list, tuple)) or len(repair_delay) != 2
                or any(type(v) not in (int, float) or not isfinite(v) for v in repair_delay)
                or repair_delay[0] <= 0 or repair_delay[1] < repair_delay[0]):
            raise ValueError("dispatch buffer repair range requires 0 < lower <= upper")
        if type(requires_transfer) is not bool:
            raise ValueError("dispatch_buffer_requires_transfer must be boolean")
        self.probability = probability
        self.repair_delay = tuple(repair_delay)
        self.seed = seed
        self.requires_transfer = requires_transfer
        self.opportunities = {}
        self.tested = set()

    def prepare(self, items, operations=None):
        matched = False
        for item in items:
            for op, seq in zip(item.ops, item.op_seqs):
                if op.op_type not in {"Process", "Inspect"}:
                    continue
                if operations is not None and (op.op_type, op.rp) not in operations:
                    continue
                matched = True
                key = f"{item.compiled.program.order_id}:{seq}"
                digest = sha256(f"dispatch-buffer:{self.seed}:{key}".encode()).digest()
                draw = int.from_bytes(digest[:8], "big") / 2**64
                self.opportunities[key] = dict(
                    key=key, draw=draw, duration_draw=int.from_bytes(digest[8:16], "big") / 2**64,
                    selected=draw < self.probability, attempted=False, delivered=False,
                    eligible_transfers=0, not_cross_station_checks=0, ineligible_transfer_checks=0)
                if operations is not None:
                    self.opportunities[key].update(op_type=op.op_type, rp=op.rp)
        if operations is not None and not matched:
            raise ValueError("dispatch_buffer_failure_operations matched no eligible operation in generated orders")

    def event_for(self, item, op, site, time, *, source_workstation, target_workstation, backup_check=None):
        # 已开工、已锁定目的地或在途的物料不可重新检查，也不消耗其抽样机会。
        if (item.finish_time is not None or item.executing is not None or item.in_transit
                or item.dispatch_site is not None or site is None
                or op.op_type not in {"Process", "Inspect"}):
            return None
        key = f"{item.compiled.program.order_id}:{item.op_seqs[item.completed_seq + 1]}"
        if key in self.tested or key not in self.opportunities:
            return None
        opportunity = self.opportunities[key]
        source_site = item.material_location
        transfer = (source_site is not None and source_workstation is not None
                    and target_workstation is not None and source_workstation != target_workstation)
        opportunity.update(source_site=source_site, source_workstation=source_workstation,
                           target_site=site, target_workstation=target_workstation)
        if self.requires_transfer and not transfer:
            reason = ("no_valid_source" if source_site is None or source_workstation is None else
                      "no_valid_target" if target_workstation is None else "not_cross_station")
            opportunity["eligibility_reason"] = reason
            opportunity["ineligible_transfer_checks"] += 1
            opportunity["not_cross_station_checks"] += int(reason == "not_cross_station")
            return None
        self.tested.add(key)
        opportunity["eligible_transfers"] = int(transfer)
        if backup_check is not None:
            status = backup_check()
            _record_backup_check(opportunity, status, time)
            if not status["eligible"]:
                # 此次可选停机检查不开展；潜在抽样仍保留，也不改到以后重抽。
                opportunity["eligibility_reason"] = status["reason"]
                return None
        opportunity.update(attempted=True, attempt_time=time, eligible_transfers=int(transfer),
                           eligibility_reason="cross_station" if transfer else "transfer_not_required")
        if not opportunity["selected"]:
            return None
        lower, upper = self.repair_delay
        duration = lower + (upper - lower) * opportunity["duration_draw"]
        opportunity.update(delivered=True, actual_duration_min=duration)
        return UncertaintyEvent("dispatch-buffer-" + key, U_BUFFER_UNAVAILABLE, time,
            order_id=item.compiled.program.order_id, op_key=key,
            target_kind=TARGET_HOLDING, target=site, resolves_at=time + duration,
            detail={"dispatch_buffer_failure": True, "startup_failure": True, "interrupted": False,
                    "source_site": source_site, "source_workstation": source_workstation,
                    "target_workstation": target_workstation})


class DispatchQualityRisk:
    """正常检验工序一次质量发现；潜在缺陷绑定订单，不随时钟改换归属。

    这里只生成事件，返工仍由原 handler / pending_defects 路径执行。
    startup_failure 仅复用固定归属的派工绑定标记，质量机会单独审计。
    """

    def __init__(self, probability, seed):
        if (type(probability) not in (int, float) or not isfinite(probability)
                or not 0 <= probability <= 1):
            raise ValueError("dispatch_quality_probability must be finite and in [0,1]")
        self.probability = probability
        self.seed = seed
        self.opportunities = {}
        self.tested = set()

    def prepare(self, items, target_model):
        for item in items:
            # item.ops 仅含正常生产序列，不含 catch 中的返工/复检动作。
            for op, seq in zip(item.ops, item.op_seqs):
                if op.op_type != "Inspect" or not target_model.triggers_exception(
                        "QualityFault", op.opcode, op.func, op.rp):
                    continue
                order_id = item.compiled.program.order_id
                key = f"{order_id}:{seq}"
                draw = int.from_bytes(sha256(
                    f"dispatch-quality:{self.seed}:{key}".encode()).digest()[:8], "big") / 2**64
                self.opportunities[key] = dict(
                    key=key, event_id="dispatch-quality-" + key, order_id=order_id,
                    op_key=key, op_type=op.op_type, rp=op.rp, draw=draw,
                    selected=draw < self.probability, attempted=False, delivered=False)

    def event_for(self, item, op, time):
        index = item.completed_seq + 1
        if (item.finish_time is not None or item.executing is not None or item.in_transit
                or item.dispatch_site is not None or index >= len(item.ops)
                or item.ops[index] is not op or op.op_type != "Inspect"):
            return None
        key = f"{item.compiled.program.order_id}:{item.op_seqs[index]}"
        if key in self.tested or key not in self.opportunities:
            return None
        self.tested.add(key)
        opportunity = self.opportunities[key]
        opportunity.update(attempted=True, attempt_time=time)
        if not opportunity["selected"]:
            return None
        opportunity.update(delivered=True, delivery_time=time)
        return UncertaintyEvent(opportunity["event_id"], U_QUALITY_FAULT, time,
            order_id=opportunity["order_id"], op_key=key,
            target_kind=TARGET_MATERIAL, target=opportunity["order_id"],
            detail={"dispatch_quality_fault": True, "startup_failure": True,
                    "interrupted": False})


class DispatchLocalEventRisk:
    """缺料/工装风险按正常工序固定归属；只改发现时机，不改恢复语义。

    每类只使用原目标机的异常触发清单，不读取处理器、策略或覆盖程度。
    未选中的首次检查也消耗机会；被资源报警延后的检查保留原抽样。
    """

    TYPES = {kind.key: kind for kind in (U_MATERIAL_SHORTAGE, U_TOOLING_DEVIATION)}

    def __init__(self, probabilities, seed, triggers=None):
        key = "dispatch_local_event_probabilities"
        if not isinstance(probabilities, dict) or not probabilities:
            raise ValueError(key + " must be a nonempty dictionary")
        for kind, probability in probabilities.items():
            if kind not in self.TYPES:
                raise ValueError(key + " has unknown kind: " + str(kind))
            if (type(probability) not in (int, float) or not isfinite(probability)
                    or not 0 <= probability <= 1):
                raise ValueError(key + " requires finite probabilities in [0,1]: " + kind)
        self.probabilities = dict(sorted(probabilities.items()))
        self.triggers = self._validate_triggers(triggers)
        self.seed = seed
        self.opportunities = {}
        self.tested = set()

    def _validate_triggers(self, triggers):
        if triggers is None:
            return {}
        key = "dispatch_local_event_triggers"
        if not isinstance(triggers, dict) or not triggers:
            raise ValueError(key + " must be a nonempty dictionary")
        result = {}
        for kind, entries in triggers.items():
            if kind not in self.probabilities:
                raise ValueError(key + " requires a configured local-event probability: " + str(kind))
            if not isinstance(entries, (list, tuple)) or not entries:
                raise ValueError(key + " requires a nonempty list of (op_type, rp): " + kind)
            selected = set()
            for entry in entries:
                if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                        or not isinstance(entry[0], str) or entry[0] not in OPCODE_FUNC_BY_OP_TYPE
                        or not isinstance(entry[1], str) or len(entry[1]) != 5
                        or set(entry[1]) - {"0", "1"}):
                    raise ValueError(key + " entries require a known op_type and a 5-bit string rp")
                selected.add(tuple(entry))
            result[kind] = frozenset(selected)
        return result

    def prepare(self, items, target_model):
        for kind, operations in self.triggers.items():
            for op_type, rp in operations:
                if not target_model.triggers_exception(
                        self.TYPES[kind].exception_type, *OPCODE_FUNC_BY_OP_TYPE[op_type], rp):
                    raise ValueError("dispatch_local_event_triggers is not a subset of original target triggers: "
                                     + kind + " " + op_type + " " + rp)
        self.opportunities.clear()
        self.tested.clear()
        for item in items:
            for op, seq in zip(item.ops, item.op_seqs):
                for kind, probability in self.probabilities.items():
                    if kind in self.triggers and (op.op_type, op.rp) not in self.triggers[kind]:
                        continue
                    event_type = self.TYPES[kind]
                    if not target_model.triggers_exception(
                            event_type.exception_type, op.opcode, op.func, op.rp):
                        continue
                    order_id = item.compiled.program.order_id
                    op_key = f"{order_id}:{seq}"
                    key = f"{kind}:{op_key}"
                    draw = int.from_bytes(sha256(
                        f"dispatch-local:{self.seed}:{key}".encode()).digest()[:8], "big") / 2**64
                    self.opportunities[key] = dict(
                        key=key, kind=kind, event_id="dispatch-local-" + key,
                        order_id=order_id, op_key=op_key, op_type=op.op_type, rp=op.rp,
                        draw=draw, selected=draw < probability, attempted=False, delivered=False)

    def event_for(self, item, op, station, time):
        index = item.completed_seq + 1
        if (item.finish_time is not None or item.executing is not None or item.in_transit
                or item.dispatch_site is not None or index >= len(item.ops)
                or item.ops[index] is not op):
            return None
        order_id = item.compiled.program.order_id
        op_key = f"{order_id}:{item.op_seqs[index]}"
        for kind in self.probabilities:
            key = f"{kind}:{op_key}"
            if key in self.tested or key not in self.opportunities:
                continue
            self.tested.add(key)
            chance = self.opportunities[key]
            chance.update(attempted=True, attempt_time=time)
            if not chance["selected"]:
                continue
            event_type = self.TYPES[kind]
            target = order_id if event_type.target_kind == TARGET_MATERIAL else station
            chance.update(delivered=True, delivery_time=time,
                          target_kind=event_type.target_kind, target=target)
            return UncertaintyEvent(chance["event_id"], event_type, time,
                order_id=order_id, op_key=op_key,
                target_kind=event_type.target_kind, target=target,
                detail={"dispatch_local_event": True, "dispatch_local_event_kind": kind,
                        "startup_failure": True, "interrupted": False})
        return None


class MachineRepairProfile:
    """各方法共用的工位类型维修规则；不读取策略、处理器或覆盖状态。

    普通时钟事件的最终目标仍可能随运行轨迹变化，因此共同规则不等于相同外生故障。
    固定外生日历可直接传已确定的 target；启动事件传原有 duration_draw。
    """

    def __init__(self, profile, workstations, seed):
        self.workstation_types = {ws.workstation_id: ws.ws_type for ws in workstations}
        if not isinstance(profile, dict) or not profile:
            raise ValueError("machine_repair_by_workstation_type must be a nonempty mapping")
        known_types = set(self.workstation_types.values())
        self.ranges = {}
        for ws_type, bounds in profile.items():
            if not isinstance(ws_type, str) or ws_type not in known_types:
                raise ValueError("unknown machine repair workstation type: " + str(ws_type))
            if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                    or any(type(value) not in (int, float) or not isfinite(value) for value in bounds)
                    or bounds[0] <= 0 or bounds[1] < bounds[0]):
                raise ValueError("machine repair ranges require 0 < lower <= upper, finite numbers: " + ws_type)
            self.ranges[ws_type] = tuple(float(value) for value in bounds)
        self.seed = seed
        self.clock_opportunities = {}
        self.root_records = {}

    def prepare_clock(self, event):
        """在日历交付前按 seed/event_id 预抽样，不消费原模拟器的随机数。"""
        if event.type.key != "machine_failure" or "source_event" in event.detail or event.detail.get("startup_failure"):
            return
        draw = int.from_bytes(sha256(f"stage-repair:{self.seed}:{event.event_id}".encode()).digest()[:8], "big") / 2**64
        self.clock_opportunities.setdefault(event.event_id, dict(
            event_id=event.event_id, time=event.time, duration_draw=draw,
            draw_source="independent_seed_event_hash"))

    def apply(self, event, target, duration_draw=None):
        """只给机器根事件设置期限；派工派生事件继承根期限，绝不重新起算。"""
        if event.type.key != "machine_failure" or "source_event" in event.detail:
            return event
        if target not in self.workstation_types:
            raise ValueError("unknown machine repair target workstation: " + str(target))
        ws_type = self.workstation_types[target]
        if duration_draw is None:
            if event.detail.get("startup_failure"):
                raise ValueError("startup machine repair requires the existing duration_draw")
            self.prepare_clock(event)
            duration_draw = self.clock_opportunities[event.event_id]["duration_draw"]
            draw_source = "independent_seed_event_hash"
        else:
            draw_source = "startup_duration_draw" if event.detail.get("startup_failure") else "supplied_duration_draw"
        if (type(duration_draw) not in (int, float) or not isfinite(duration_draw)
                or not 0 <= duration_draw <= 1):
            raise ValueError("machine repair duration_draw must be finite and in [0,1]")
        until = event.resolves_at
        applied = ws_type in self.ranges
        if applied:
            lower, upper = self.ranges[ws_type]
            until = event.time + lower + (upper - lower) * duration_draw
        info = dict(target_workstation_type=ws_type, machine_repair_profile_applied=applied,
                    machine_repair_duration_min=None if until is None else until-event.time,
                    machine_repair_duration_draw=duration_draw, machine_repair_draw_source=draw_source)
        self.root_records[event.event_id] = dict(event_id=event.event_id, time=event.time,
            target=target, target_kind=TARGET_WORKSTATION, resolves_at=until, **info)
        return replace(event, target=target, target_kind=TARGET_WORKSTATION,
                       resolves_at=until, detail={**event.detail, **info})


def _bind_prestart_event(event, admitted, bind, event_types, opportunities):
    """只筛选声明的外部开工检查机会；不重抽风险，不改变原时钟或派工报警。"""
    if event.type.key not in event_types or "source_event" in event.detail:
        return bind(event, admitted)
    eligible = [item for item in admitted if item.finish_time is None
                and item.executing is None and not item.in_transit
                and item.dispatch_site is None]
    bound = bind(event, eligible)
    opportunity = dict(event_id=event.event_id, kind=event.type.key, time=event.time,
                       ready_orders=len(eligible), delivered=bound is not None)
    if bound is None:
        # 原绑定器还会检查工序能否触发该异常；无匹配工序同样不能投递。
        opportunity["suppressed_reason"] = "no_eligible_prestart_operation"
    else:
        opportunity["order_id"] = bound.order_id
        opportunity["op_key"] = bound.op_key
    opportunities.append(opportunity)
    return bound


class TimedCoordinator(UncertaintyCoordinator):
    """计时包括属性检查、处理器执行和区域检测；仍调用原来的算法。"""

    def handle(self, event, allocation=None):
        start = time.perf_counter()
        result = super().handle(event, allocation)
        self.last_response_time_s = time.perf_counter() - start
        return result


def summarize_events(records):
    summary = {}
    for prefix, selected in (("event", records),
                             ("external_event", [r for r in records if not r["source_event"]]),
                             ("dispatch_event", [r for r in records if r["source_event"]])):
        summary[prefix + "_count"] = len(selected)
        for path in ("contained", "partial", "inexecutable", "unanticipated"):
            count = sum(r["path"] == path for r in selected)
            summary[prefix + "_" + path + "_count"] = count
            summary[prefix + "_" + path + "_share"] = count / len(selected) if selected else 0.0
    for label, selected in (("response", records),
                            ("partial_response", [r for r in records if r["path"] == "partial"]),
                            ("external_response", [r for r in records if not r["source_event"]]),
                            ("dispatch_response", [r for r in records if r["source_event"]])):
        values = [r["response_time_s"] for r in selected]
        summary[label + "_time_total_s"] = sum(values)
        # 无事件时延迟分位数没有定义；总耗时为零，不能伪造零延迟观测。
        summary[label + "_time_mean_s"] = mean(values) if values else None
        summary[label + "_time_p50_s"] = percentile(values, .5) if values else None
        summary[label + "_time_p95_s"] = percentile(values, .95) if values else None
        summary[label + "_time_max_s"] = max(values) if values else None
    summary["coordinator_time_total_s"] = sum(r["coordinator_time_s"] for r in records)
    summary["response_region_versions_total"] = sum(r["region_versions"] for r in records)
    summary["response_changed_versions_total"] = sum(r["changed_versions"] for r in records)
    summary["response_changed_orders_total"] = sum(r["changed_orders"] for r in records)
    summary["response_outside_region_changes_total"] = sum(r["outside_region_changes"] for r in records)
    summary["partial_region_versions_total"] = sum(r["region_versions"] for r in records if r["path"] == "partial")
    summary["handler_only_events"] = sum(r["used_handler"] and not r["requested_recompile"] for r in records)
    summary["handler_plus_revision_events"] = sum(r["used_handler"] and r["requested_recompile"] for r in records)
    summary["revision_only_events"] = sum(not r["used_handler"] and r["requested_recompile"] for r in records)
    summary["region_pending_fraction_mean"] = mean([r["region_pending_fraction"] for r in records]) if records else None
    for reason in ("workstation", "logistics", "holding", "holding_workstation", "holding_outage", "holding_capacity"):
        summary["handler_unavailable_" + reason + "_events"] = sum(reason in r["unavailable_reasons"] for r in records)
    return summary


def observed_simulate(job, model, candidate):
    group, name, seed, config = job
    startup_operations = _startup_operation_filter(candidate)
    buffer_operations = _buffer_operation_filter(candidate)
    quality_risk = (DispatchQualityRisk(candidate["dispatch_quality_probability"], seed)
                    if "dispatch_quality_probability" in candidate else None)
    if "dispatch_local_event_triggers" in candidate and "dispatch_local_event_probabilities" not in candidate:
        raise ValueError("dispatch_local_event_triggers requires dispatch_local_event_probabilities")
    if "dispatch_local_event_triggers" in candidate and candidate["dispatch_local_event_triggers"] is None:
        raise ValueError("dispatch_local_event_triggers must be a nonempty dictionary")
    local_risk = (DispatchLocalEventRisk(candidate["dispatch_local_event_probabilities"], seed,
                                       candidate.get("dispatch_local_event_triggers"))
                  if "dispatch_local_event_probabilities" in candidate else None)
    requires_backup = candidate.get("dispatch_risk_requires_backup_channel", False)
    if type(requires_backup) is not bool:
        raise ValueError("dispatch_risk_requires_backup_channel must be boolean")
    if ("retain_calendar_buffer_failures" in candidate
            and type(candidate["retain_calendar_buffer_failures"]) is not bool):
        raise ValueError("retain_calendar_buffer_failures must be boolean")
    simulator = study2.LineSimulator(config, model_path=model)
    if buffer_operations is not None:
        declared = {(action.op_type, action.rp) for action in simulator.spec.actions}
        if not buffer_operations & declared:
            raise ValueError("dispatch_buffer_failure_operations matches no declared Process/Inspect operation")
    if startup_operations is not None:
        declared = {(action.op_type, action.rp) for action in simulator.spec.actions}
        eligible = {pair for pair in startup_operations & declared
                    if simulator.proxy.target_model.triggers_exception(
                        "MachineFailure", *OPCODE_FUNC_BY_OP_TYPE[pair[0]], pair[1])}
        if not eligible:
            raise ValueError("startup_failure_operations matches no declared MachineFailure trigger")
    buffer_risk = (DispatchBufferRisk(candidate["dispatch_buffer_failure_probability"],
                   candidate.get("event_duration_ranges", {}).get("buffer_unavailable", config.repair_delay), seed,
                   candidate.get("dispatch_buffer_requires_transfer", True))
                   if "dispatch_buffer_failure_probability" in candidate else None)
    repair_profile = (MachineRepairProfile(candidate["machine_repair_by_workstation_type"],
                      simulator.spec.workstations, seed)
                      if "machine_repair_by_workstation_type" in candidate else None)
    exogenous_stations = candidate.get("exogenous_machine_workstations")
    if "exogenous_machine_workstations" in candidate:
        known_stations = {ws.workstation_id for ws in simulator.spec.workstations}
        if (not isinstance(exogenous_stations, (list, tuple)) or not exogenous_stations
                or any(type(ws) is not int or ws not in known_stations for ws in exogenous_stations)
                or len(set(exogenous_stations)) != len(exogenous_stations)):
            raise ValueError("exogenous_machine_workstations must be nonempty unique existing workstation ids")
    records = []
    batches = []
    calendar = []
    peak = 0
    original_sync = simulator._sync_inventory
    original_recolor = simulator._recolor
    original_events = simulator._uncertainty_events
    original_bind = simulator._bind_to_live_order
    original_target = simulator._resolve_event_target
    suppressed = []
    clock_opportunities = []
    calendar_buffer_opportunities = []
    calendar_quality_opportunities = []
    calendar_local_opportunities = []
    isolated_calendar_opportunities = []
    overlap_opportunities = []
    prestart_event_types = frozenset(candidate.get("prestart_event_types", ()))
    prestart_opportunities = []
    opportunities = {}
    tested = set()
    last_allocations = {}
    all_changes = []
    recovery=RecoveryTracker()

    def remember_allocations(allocation, origin):
        # 保留 WAIT 前的旧址；只在重新得到具体地址时记录，覆盖延迟重绑定。
        # 此计数同时包括处理器、重编译、后续落位及派工的版本地址变化。
        for version,site in allocation.items():
            if not isinstance(site,int):
                continue
            old=last_allocations.get(version)
            if old is not None and old!=site:
                all_changes.append(dict(version=version,old_site=old,new_site=site,origin=origin))
            last_allocations[version]=site

    def startup_failure(item, op, station, t, seed):
        key=f"{item.compiled.program.order_id}:{item.op_seqs[item.completed_seq+1]}"
        if key in tested or key not in opportunities:
            return None
        tested.add(key)
        opportunity=opportunities[key]
        opportunity["station"]=station
        if requires_backup:
            status = _backup_channel_status(item, op, station, simulator.f_t, t)
            _record_backup_check(opportunity, status, t)
            if not status["eligible"]:
                return None
        opportunity["attempt_time"]=t
        if not opportunity["selected"]:
            return None
        stations=item.lowering.operations[id(op)].candidate_workstations
        if candidate.get("single_startup_outage_per_stage",candidate.get("single_outage_per_stage")) and not all(
                simulator.f_t.is_available(AVAIL_WORKSTATION,s,t) for s in stations):
            opportunity["suppressed_reason"]="stage_already_has_an_outage"
            return None
        delay=simulator.config.repair_delay
        duration=delay[0]+(delay[1]-delay[0])*opportunity["duration_draw"]
        event=UncertaintyEvent("startup-"+key,U_MACHINE_FAILURE,t,
            order_id=item.compiled.program.order_id,op_key=key,target_kind=TARGET_WORKSTATION,
            target=station,resolves_at=t+duration,detail={"startup_failure":True,"interrupted":False})
        if repair_profile is not None:
            event = repair_profile.apply(event, station, opportunity["duration_draw"])
            opportunity.update(target_workstation_type=event.detail["target_workstation_type"],
                               actual_duration_min=event.resolves_at-event.time)
        opportunity["delivered"]=True
        calendar.append(dict(event_id=event.event_id,kind=event.type.key,time=t,
                             resolves_at=event.resolves_at,target_kind=event.target_kind,target=station))
        return event

    def dispatch_failures(item, op, station, t, seed):
        # 缓存先检查；若真实投递，机器机会留到故障处置后的下一次可执行派工。
        # 未选中缓存风险时可在本次继续检查机器，两个机会集合互不消费。
        if buffer_risk is not None:
            if (item.finish_time is not None or item.executing is not None
                    or item.in_transit or item.dispatch_site is not None):
                return None
            site = study2._output_site(op, simulator._planned)
            source = simulator.f_t.holding_site(item.material_location)
            event = buffer_risk.event_for(item, op, site, t,
                source_workstation=source.workstation_id if source is not None else None,
                target_workstation=station,
                backup_check=(lambda: _backup_channel_status(item, op, station, simulator.f_t, t))
                             if requires_backup else None)
            if event is not None:
                calendar.append(dict(event_id=event.event_id, kind=event.type.key, time=t,
                    resolves_at=event.resolves_at, target_kind=event.target_kind, target=site,
                    dispatch_buffer_failure=True, startup_failure=True))
                return event
        event = startup_failure(item, op, station, t, seed)
        if event is not None:
            return event
        # 所有资源自检优先；固定订单的局部风险在其后首次可执行的派工点发现。
        if local_risk is not None:
            event = local_risk.event_for(item, op, station, t)
            if event is not None:
                duration = simulator._recovery_duration(event)
                event = replace(event, resolves_at=t + duration)
                local_risk.opportunities[f"{event.type.key}:{event.op_key}"]["event_duration_min"] = duration
                calendar.append(dict(event_id=event.event_id, kind=event.type.key, time=t,
                    resolves_at=event.resolves_at, target_kind=event.target_kind, target=event.target,
                    order_id=event.order_id, op_key=event.op_key,
                    dispatch_local_event=True, startup_failure=True))
                return event
        if quality_risk is None:
            return None
        # 资源自检优先；完成其处置后，原检验工序再发现已预抽的质量问题。
        event = quality_risk.event_for(item, op, t)
        if event is not None:
            duration = simulator._recovery_duration(event)
            event = replace(event, resolves_at=t + duration)
            quality_risk.opportunities[event.op_key]["event_duration_min"] = duration
            calendar.append(dict(event_id=event.event_id, kind=event.type.key, time=t,
                resolves_at=event.resolves_at, target_kind=event.target_kind, target=event.target,
                order_id=event.order_id, op_key=event.op_key,
                dispatch_quality_fault=True, startup_failure=True))
        return event

    def bind_event(event, admitted):
        bound = _bind_prestart_event(event, admitted, original_bind,
                                    prestart_event_types, prestart_opportunities)
        if (repair_profile is not None and bound is not None and bound.type.key == "machine_failure"
                and "source_event" not in bound.detail and not bound.detail.get("exogenous_resource")):
            target = original_target(bound, admitted)
            if target is not None:
                bound = repair_profile.apply(bound, target)
        return bound

    def isolated_stage_failure(event, admitted):
        """可选可靠性条件：可互备工段最多一台设备同时故障。

        原始报警机会日历保持不变。已有停机的工段不再接受新的外部机器故障，
        但派工报警仍照常处理。不读取策略、处理器可执行性或 containment 判据。
        所有未投递机会均保留，不能把这种条件化生成器写成独立同分布故障。
        """
        if event.detail.get("startup_failure") or event.detail.get("exogenous_resource"):
            return event
        if (not candidate.get("single_outage_per_stage") or event.type.key != "machine_failure"
                or "source_event" in event.detail):
            return bind_event(event, admitted)
        if candidate.get("stage_overlap_probability",0):
            # 独立预抽样的共因停机机会，允许部分时钟故障越过单故障工段条件。
            # 不读取策略名称、处理器覆盖判据或实验收益。
            draw=int.from_bytes(sha256(f"overlap:{seed}:{event.event_id}".encode()).digest()[:8],"big")/2**64
            allowed=draw<candidate["stage_overlap_probability"]
            overlap_opportunities.append(dict(event_id=event.event_id,time=event.time,draw=draw,allowed=allowed))
            if allowed:
                return bind_event(event,admitted)
        eligible = []
        for item in admitted:
            if item.finish_time is not None:
                continue
            op = item.ops[item.completed_seq+1]
            lowered = item.lowering.operations.get(id(op))
            stations = lowered.candidate_workstations if lowered else ()
            if stations and all(simulator.f_t.is_available(AVAIL_WORKSTATION, s, event.time) for s in stations):
                eligible.append(item)
        bound = bind_event(event, eligible)
        if bound is None:
            suppressed.append(dict(event_id=event.event_id,time=event.time,
                                   reason="no_eligible_live_operation_in_an_operational_stage"))
        return bound

    def checked_sync(items, t):
        nonlocal peak
        recovery.sync(items)
        remember_allocations(simulator._planned,"dispatch_or_state_sync")
        original_sync(items, t)
        for site, count in simulator.f_t.state.occupied.items():
            assert count <= simulator.f_t.capacity_of(site), (name, seed, t, site, count)
            peak = max(peak, count)

    def checked_recolor(items, t):
        prior = dict(simulator._planned)
        result = original_recolor(items, t)
        workset, _, allocation, _ = result
        remember_allocations(allocation,"routine_or_deferred_coloring")
        for version in workset.pending_vregs & prior.keys() & allocation.keys():
            assert allocation[version] == prior[version], (name, seed, t, version)
        return result

    def saved_calendar(items, seed):
        recovery.register(items)
        events = original_events(items, seed)
        if local_risk is not None:
            local_risk.prepare(items, simulator.proxy.target_model)
            calendar_local_opportunities.extend(dict(
                event_id=e.event_id, kind=e.type.key, time=e.time, resolves_at=e.resolves_at,
                target_kind=e.target_kind, target=e.target, selected=False,
                reason="replaced_by_dispatch_local_event") for e in events
                if e.type.key in local_risk.probabilities)
            events = [e for e in events if e.type.key not in local_risk.probabilities]
        if quality_risk is not None:
            quality_risk.prepare(items, simulator.proxy.target_model)
            calendar_quality_opportunities.extend(dict(
                event_id=e.event_id, kind=e.type.key, time=e.time, resolves_at=e.resolves_at,
                target_kind=e.target_kind, target=e.target, selected=False,
                reason="replaced_by_dispatch_quality") for e in events if e.type.key == "quality_fault")
            events = [e for e in events if e.type.key != "quality_fault"]
        if candidate.get("retain_calendar_buffer_failures", True) is False:
            calendar_buffer_opportunities.extend(dict(
                event_id=e.event_id, kind=e.type.key, time=e.time, resolves_at=e.resolves_at,
                target_kind=e.target_kind, target=e.target, selected=False,
                reason="calendar_buffer_disabled") for e in events if e.type.key == "buffer_unavailable")
            events = [e for e in events if e.type.key != "buffer_unavailable"]
        if buffer_risk is not None:
            buffer_risk.prepare(items, buffer_operations)
        if repair_profile is not None:
            for event in events:
                repair_profile.prepare_clock(event)
        if "calendar_machine_keep_fraction" in candidate:
            retained=[]
            for event in events:
                if event.type.key!="machine_failure":
                    retained.append(event)
                    continue
                draw=int.from_bytes(sha256(f"clock:{seed}:{event.event_id}".encode()).digest()[:8],"big")/2**64
                selected=draw<candidate["calendar_machine_keep_fraction"]
                clock_opportunities.append(dict(event_id=event.event_id,time=event.time,
                                                draw=draw,selected=selected))
                if selected:
                    retained.append(event)
            events=retained
        if "startup_failure_probability" in candidate:
            # 工序启动风险替换时钟机器故障，质量/库位日历原样保留。
            if not candidate.get("retain_calendar_machine_failures"):
                events=[e for e in events if e.type.key!="machine_failure"]
            for item in items:
                for op,seq in zip(item.ops,item.op_seqs):
                    if startup_operations is not None and (op.op_type, op.rp) not in startup_operations:
                        continue
                    if not simulator.proxy.target_model.triggers_exception(
                            "MachineFailure",op.opcode,op.func,op.rp):
                        continue
                    key=f"{item.compiled.program.order_id}:{seq}"
                    digest=sha256(f"startup:{seed}:{key}".encode()).digest()
                    draw=int.from_bytes(digest[:8],"big")/2**64
                    opportunities[key]=dict(key=key,draw=draw,
                        duration_draw=int.from_bytes(digest[8:16],"big")/2**64,
                        selected=draw<candidate["startup_failure_probability"],delivered=False)
                    if startup_operations is not None:
                        opportunities[key].update(op_type=op.op_type, rp=op.rp)
            if startup_operations is not None and not opportunities:
                raise ValueError("startup_failure_operations matched no eligible operation in generated orders")
        if exogenous_stations is not None:
            from case_study.resource_calendar import fix_machine_calendar
            events = fix_machine_calendar(events, exogenous_stations, seed)
        if repair_profile is not None:
            events = [repair_profile.apply(event, event.target)
                      if event.detail.get("exogenous_resource") and event.type.key == "machine_failure"
                      else event for event in events]
        if candidate.get("exogenous_single_outage_per_stage"):
            if exogenous_stations is None:
                raise ValueError("exogenous stage isolation requires a fixed machine calendar")
            from case_study.resource_calendar import isolate_machine_calendar
            events, opportunities_by_stage = isolate_machine_calendar(events, simulator.spec.workstations)
            isolated_calendar_opportunities.extend(opportunities_by_stage)
        calendar.extend(dict(event_id=e.event_id, kind=e.type.key, time=e.time,
                             resolves_at=e.resolves_at, target_kind=e.target_kind,
                             target=e.target) for e in events)
        return events

    def observe(event, outcome, coordinator, prior, elapsed):
        # 诊断在主计时结束后读取；不参与目标选取、事件筛选或策略决定。
        application = outcome.handler_application
        affected=coordinator.detector.versions_with_invalidated_candidates(
            event,coordinator.workset,event.time,allocation=prior) if event.type.invalidates_resource else set()
        recovery.add(event,affected,response_path(outcome.attributes))
        covered = set(application.region_vregs) if application else set()
        region = set(outcome.region.expansion) if outcome.region else set()
        current = coordinator.allocation
        remember_allocations(current,"event:"+event.event_id)
        changed = {v for v, site in current.items() if v in prior and prior[v] != site}
        owners = {f.order_id for f in coordinator.workset.fragments
                  if changed & (set(f.pending_vregs) | set(f.carried_vregs))}
        # 诊断读取事件处置后的资源状态，只解释不可执行原因，不参与选事件或响应。
        reasons = set()
        if outcome.attributes.anticipated and not outcome.attributes.executable_at_trigger:
            handler = coordinator.handler_for(event)
            binding = handler.binding_for(event)
            for kind, resource, _ in binding.actions.values():
                key = AVAIL_LOGISTICS if kind == "logistics" else AVAIL_WORKSTATION
                if not coordinator.f_t.is_available(key, resource, event.time):
                    reasons.add(kind)
            if any(not coordinator._fixed_site_available(v, p, event.time)
                   for v, p in binding.allocation.items()):
                reasons.add("holding")
            for v,p in binding.allocation.items():
                if coordinator._fixed_site_available(v,p,event.time):
                    continue
                ws=coordinator.f_t.holding_site(p).workstation_id
                if not coordinator.f_t.is_available(AVAIL_WORKSTATION,ws,event.time):
                    reasons.add("holding_workstation")
                if not coordinator.f_t.is_available(AVAIL_HOLDING,p,event.time):
                    reasons.add("holding_outage")
                if coordinator.f_t.occupancy_of(p)>=coordinator.f_t.capacity_of(p):
                    reasons.add("holding_capacity")
        pending = len(coordinator.workset.pending_vregs)
        records.append(dict(
            method=name, seed=seed, event_id=event.event_id, source_event=event.detail.get("source_event", ""),
            kind=event.type.key, time=event.time, order_id=event.order_id,
            startup_failure=bool(event.detail.get("startup_failure")),
            startup_machine_failure=bool(event.detail.get("startup_failure") and event.type.key == "machine_failure"),
            interrupted=bool(event.detail.get("interrupted")),
            target_kind=event.target_kind, target=event.target,
            path=response_path(outcome.attributes), anticipated=outcome.attributes.anticipated,
            executable=outcome.attributes.executable_at_trigger, contained=outcome.attributes.contained,
            uncovered_versions=outcome.attributes.uncovered_versions or 0,
            used_handler=outcome.plan.use_handler, requested_recompile=outcome.plan.recompile,
            rebuilt=bool(outcome.recompile and outcome.recompile.rebuilt),
            region_versions=len(region), handler_versions=len(covered),
            pending_versions=pending, region_pending_fraction=len(region)/max(1, pending),
            unavailable_reasons=sorted(reasons),
            changed_versions=len(changed), changed_orders=len(owners),
            outside_region_changes=len(changed - region - covered),
            waiting_versions=len(coordinator.waiting),
            response_time_s=elapsed, coordinator_time_s=coordinator.last_response_time_s,
            recompilation_time_s=outcome.recompilation_time_s,
        ))
        if buffer_risk is not None:
            root_id = event.detail.get("source_event", event.event_id)
            records[-1]["dispatch_buffer_failure"] = root_id.startswith("dispatch-buffer-")
        if quality_risk is not None:
            records[-1]["dispatch_quality_fault"] = bool(event.detail.get("dispatch_quality_fault"))
            if event.detail.get("dispatch_quality_fault"):
                records[-1]["op_key"] = event.op_key
        if local_risk is not None:
            records[-1]["dispatch_local_event"] = bool(event.detail.get("dispatch_local_event"))
            if event.detail.get("dispatch_local_event"):
                records[-1].update(op_key=event.op_key,
                                  dispatch_local_event_kind=event.detail["dispatch_local_event_kind"])
        if repair_profile is not None and event.type.key == "machine_failure":
            root_id = event.detail.get("source_event", event.event_id)
            root = repair_profile.root_records.get(root_id, {})
            records[-1].update(resolves_at=event.resolves_at,
                target_workstation_type=repair_profile.workstation_types.get(event.target),
                machine_repair_duration_min=root.get("machine_repair_duration_min"),
                machine_repair_duration_draw=root.get("machine_repair_duration_draw"),
                machine_repair_draw_source=root.get("machine_repair_draw_source"),
                machine_repair_profile_applied=root.get("machine_repair_profile_applied", False),
                machine_repair_remaining_min=(None if event.resolves_at is None
                                             else max(0, event.resolves_at-event.time)))

    simulator._sync_inventory = checked_sync
    simulator._recolor = checked_recolor
    simulator._uncertainty_events = saved_calendar
    simulator._bind_to_live_order = isolated_stage_failure
    simulator._resolve_event_target = lambda event,items: (event.target if event.detail.get("startup_failure")
                                                         else original_target(event,items))
    if ("startup_failure_probability" in candidate or buffer_risk is not None
            or quality_risk is not None or local_risk is not None):
        simulator.dispatch_uncertainty_factory=dispatch_failures
    simulator.event_response_observer = observe
    def observe_idle_outage(event, items, allocation):
        # 空闲设备失效也记根事件；恢复责任仍由事前分配决定，不借用任意订单处理器。
        workset = simulator._compose_workset(items)
        detector = RegionalResidualRecompilation(simulator.f_t, simulator._lowerings(items))
        affected = detector.versions_with_invalidated_candidates(
            event, workset, event.time, allocation=allocation)
        recovery.add(event, affected, "no_active_owner")
    simulator.resource_outage_observer = observe_idle_outage
    def observe_batch(t, count, elapsed):
        batch_id=len(batches)
        batches.append(dict(batch_id=batch_id,time=t,count=count,elapsed_s=elapsed))
        # 无订单的资源态响应仍有完整批耗时；count=0不能误取全部既往记录。
        for record in (records[-count:] if count else ()):
            record["response_batch_id"]=batch_id
            record["response_batch_time_s"]=elapsed
            record["response_batch_event_count"]=count
    simulator.event_batch_observer=observe_batch
    simulator.collect_transport_records = True
    with patch.object(study2, "UncertaintyCoordinator", TimedCoordinator):
        result = simulator.run(seed)
    row = asdict(Study2Row.of(result, method=name, region_rule=config.region_rule))
    # 原 CSV 的全部字段及 SimulationResult 中此前未导出的三个量都保留。
    row.update({k: v for k, v in asdict(result).items() if k not in row and k != "policy"})
    row.update(summarize_events(records))
    row.update(recovery.finish())
    trips = simulator.transport_records
    transport_requests = list(simulator.transport_requests.values())
    row.update(actual_transport_trips=len(trips),
               total_actual_transport_min=sum(trip["duration"] for trip in trips),
               agv_wait_min=sum(request["agv_wait"] for request in transport_requests),
               completed_transport_agv_wait_min=sum(trip["agv_wait"] for trip in trips),
               unshipped_transport_agv_wait_min=sum(request["agv_wait"] for request in transport_requests
                                                   if not request["departed"]))
    row.update(_transport_distance_summary(trips))
    assert round(row["total_actual_transport_min"], 2) == row["transport_time"]
    completed_orders=collect_completion_records(recovery.items.values())
    baseline_due=evaluate_due_factor(completed_orders,config.due_slack_factor,config.due_slack_factor)
    assert round(baseline_due["mean_tardiness"],2)==row["mean_tardiness"]
    assert round(baseline_due["on_time_delivery_rate"],4)==row["on_time_delivery_rate"]
    if candidate.get("due_sensitivity"):
        if "batch_due_factor" in candidate or "event_window_minutes_per_order" not in candidate:
            raise ValueError("offline due sensitivity requires a fixed event window and no batch_due_factor")
        for factor in candidate["due_sensitivity"]:
            sensitivity=evaluate_due_factor(completed_orders,config.due_slack_factor,factor)
            tag=format(factor,"g").replace(".","p")
            row["due_"+tag+"_mean_tardiness"]=round(sensitivity["mean_tardiness"],2)
            row["due_"+tag+"_on_time_delivery_rate"]=round(sensitivity["on_time_delivery_rate"],4)
    row["recompiled_versions_per_call"]=(row["rebound_versions"]/row["recompilations"]
                                         if row["recompilations"] else None)
    times=[b["elapsed_s"] for b in batches]
    row.update(full_response_batch_count=len(times), full_response_time_total_s=sum(times),
               full_response_batch_mean_s=mean(times) if times else None,
               full_response_batch_p50_s=percentile(times,.5) if times else None,
               full_response_batch_p95_s=percentile(times,.95) if times else None,
               full_response_batch_max_s=max(times) if times else None)
    row.update(occupancy_peak=peak, capacity_violations=0, ordinary_reassignments=0,
               all_source_allocation_revision_count=len(all_changes),
               all_source_revised_distinct_versions=len({c["version"] for c in all_changes}),
               deferred_rehandled_versions=simulator.deferred_rehandled_versions,
               dispatch_reassignments=len(simulator.reassignment_records),
               reassignment_busy_time=sum(r[3] for r in simulator.reassignment_records),
               generated_external_events=len(calendar),
               startup_failure_opportunities=len(opportunities),
               startup_failure_selected=sum(o["selected"] for o in opportunities.values()),
               startup_failures_delivered=sum(o["delivered"] for o in opportunities.values()),
               suppressed_machine_opportunities=len(suppressed),
               unhandled_external_events=len(calendar)-row["external_event_count"])
    outages = simulator.resource_outage_records
    idle_outages = [r for r in outages if not r["has_owner"]]
    row.update(exogenous_machine_outages=len(outages),
               exogenous_machine_outages_without_owner=len(idle_outages),
               resource_only_state_apply_time_total_s=sum(r["state_apply_time_s"] for r in idle_outages),
               delivered_external_roots=row["external_event_count"]+len(idle_outages),
               delivered_external_partial_share=row["external_event_partial_count"] /
                   max(1, row["external_event_count"]+len(idle_outages)))
    row.update(unprocessed_external_opportunities=len(calendar)-row["delivered_external_roots"],
               exogenous_stage_opportunities=len(isolated_calendar_opportunities),
               exogenous_stage_suppressed=sum(not r["selected"] for r in isolated_calendar_opportunities))
    # 输入机会日历中标明未投递原因，保留逐机会审计。
    by_id={e["event_id"]:e for e in calendar}
    if repair_profile is not None:
        for event_id, actual in repair_profile.root_records.items():
            if event_id in by_id:
                record = by_id[event_id]
                record.update(opportunity_target=record["target"],
                              opportunity_resolves_at=record["resolves_at"])
                record.update(actual)
        calendar.append(dict(record_type="machine_repair_opportunities",
            opportunities=list(repair_profile.clock_opportunities.values()),
            interpretation="common workstation-type duration rule and seed/event draws; live-bound targets can differ by method; fixed exogenous targets require exogenous_machine_workstations"))
    for item in suppressed:
        by_id[item["event_id"]]["suppressed_reason"]=item["reason"]
    if prestart_event_types:
        row.update(prestart_event_opportunities=len(prestart_opportunities),
                   prestart_events_delivered=sum(o["delivered"] for o in prestart_opportunities),
                   suppressed_prestart_opportunities=sum(not o["delivered"] for o in prestart_opportunities))
        for item in prestart_opportunities:
            original = by_id[item["event_id"]]
            original["prestart_delivered"] = item["delivered"]
            original["prestart_ready_orders"] = item["ready_orders"]
            if not item["delivered"]:
                original["suppressed_reason"] = item["suppressed_reason"]
        calendar.append(dict(record_type="prestart_event_opportunities",
                             opportunities=prestart_opportunities))
    if opportunities:
        calendar.append(dict(record_type="startup_opportunities",opportunities=list(opportunities.values())))
    if buffer_risk is not None:
        chances = list(buffer_risk.opportunities.values())
        row.update(dispatch_buffer_failure_opportunities=len(chances),
                   dispatch_buffer_failure_selected=sum(o["selected"] for o in chances),
                   dispatch_buffer_failure_attempted=sum(o["attempted"] for o in chances),
                   dispatch_buffer_eligible_transfers=sum(o["eligible_transfers"] for o in chances),
                   dispatch_buffer_failures_delivered=sum(o["delivered"] for o in chances))
        calendar.append(dict(record_type="dispatch_buffer_opportunities", opportunities=chances,
            interpretation="startup_failure is the shared pre-dispatch binding marker; count machine roots with startup_machine_failure and buffer roots with dispatch_buffer_failure, excluding source_event repeats"))
    if candidate.get("retain_calendar_buffer_failures", True) is False:
        row["suppressed_calendar_buffer_opportunities"] = len(calendar_buffer_opportunities)
        calendar.append(dict(record_type="calendar_buffer_opportunities", opportunities=calendar_buffer_opportunities))
    if quality_risk is not None:
        chances = list(quality_risk.opportunities.values())
        row.update(dispatch_quality_opportunities=len(chances),
                   dispatch_quality_selected=sum(o["selected"] for o in chances),
                   dispatch_quality_attempted=sum(o["attempted"] for o in chances),
                   dispatch_quality_delivered=sum(o["delivered"] for o in chances),
                   suppressed_calendar_quality_opportunities=len(calendar_quality_opportunities))
        calendar.append(dict(record_type="dispatch_quality_opportunities", opportunities=chances,
            interpretation="one latent draw per normal QualityFault-triggering Inspect, fixed order/op/material; resource checks precede discovery; retries and recovery Inspect do not redraw; original recovery actions and costs are unchanged"))
        calendar.append(dict(record_type="calendar_quality_opportunities", opportunities=calendar_quality_opportunities))
    if local_risk is not None:
        chances = list(local_risk.opportunities.values())
        for kind in local_risk.probabilities:
            selected = [chance for chance in chances if chance["kind"] == kind]
            prefix = "dispatch_local_" + kind
            row.update({prefix + "_opportunities": len(selected),
                        prefix + "_selected": sum(c["selected"] for c in selected),
                        prefix + "_attempted": sum(c["attempted"] for c in selected),
                        prefix + "_delivered": sum(c["delivered"] for c in selected),
                        prefix + "_replaced_clock_opportunities": sum(
                            c["kind"] == kind for c in calendar_local_opportunities)})
        calendar.append(dict(record_type="dispatch_local_event_opportunities", opportunities=chances,
            interpretation="one seed/kind/order/op draw for each original normal-operation trigger; resource checks first; fixed owner; no handler/coverage/method filtering or redraw; only enabled kinds replace clocks; original recovery actions, costs and Reactive latent-defect semantics unchanged"))
        if local_risk.triggers:
            calendar.append(dict(record_type="dispatch_local_event_trigger_filter",
                triggers={kind: [list(pair) for pair in sorted(operations)]
                          for kind, operations in local_risk.triggers.items()},
                interpretation="optional discovery-point subset of original target triggers; omitted kinds keep all original trigger operations; no handler or method filtering"))
        calendar.append(dict(record_type="calendar_local_event_opportunities",
                             opportunities=calendar_local_opportunities))
    if requires_backup:
        for prefix, collection in (("startup", opportunities.values()),
                                   ("buffer", buffer_risk.opportunities.values() if buffer_risk else ())):
            checked = [o for o in collection if o.get("backup_channel_checked")]
            row[prefix + "_backup_channel_checked"] = len(checked)
            row[prefix + "_backup_channel_eligible"] = sum(o["backup_channel_eligible"] for o in checked)
            row[prefix + "_backup_channel_suppressed"] = sum(o["backup_channel_suppressed"] for o in checked)
            row[prefix + "_backup_channel_selected_suppressed"] = sum(
                o["selected"] and o["backup_channel_suppressed"] for o in checked)
        calendar.append(dict(record_type="dispatch_backup_channel_policy", enabled=True,
            interpretation="optional shutdown checks require another capable workstation with an available qualified holding site; queue/occupancy is not an outage; rejected checks consume their once-per-operation opportunity without changing latent draws; conditional workload, not iid hardware failures"))
    if clock_opportunities:
        calendar.append(dict(record_type="calendar_machine_opportunities",opportunities=clock_opportunities))
    if overlap_opportunities:
        calendar.append(dict(record_type="stage_overlap_opportunities",opportunities=overlap_opportunities))
    calendar.append(dict(record_type="allocation_changes",changes=all_changes))
    if outages:
        calendar.append(dict(record_type="exogenous_resource_outages", records=outages))
    if isolated_calendar_opportunities:
        calendar.append(dict(record_type="exogenous_stage_opportunities", records=isolated_calendar_opportunities))
    calendar.append(dict(record_type="physical_recovery",records=recovery.records))
    calendar.append(dict(record_type="actual_transport", records=trips,
        interpretation="actual cross-workstation dispatch only; duration reconciles original transport_time; distance uses model coordinate steps, not metres; excludes recovery Load busy-time without a spatial trajectory"))
    calendar.append(dict(record_type="transport_requests", records=transport_requests,
        interpretation="agv_wait integrates only simulation intervals passing other dispatch checks but lacking a free AGV; request-to-departure may include other waits; unshipped requests contribute waiting but no fictitious trip"))
    calendar.append(dict(record_type="order_completions",base_due_factor=config.due_slack_factor,
                         records=completed_orders))
    return row, records, calendar


def run_candidate(job, model, candidate):
    if candidate.get("due_sensitivity") and (
            "batch_due_factor" in candidate or "event_window_minutes_per_order" not in candidate):
        raise ValueError("offline due sensitivity requires a fixed event window and no batch_due_factor")
    # 原驱动已使用作用域内的替换来声明日历/订单组合；这里仅替换结果采集函数。
    with patch.object(grid, "simulate", partial(observed_simulate, candidate=candidate)):
        return grid.simulate_candidate(job, model, candidate)
