"""Factory resources, order generation, and uncertainty inputs for the shared AHU experiment."""
from dataclasses import replace
import random
from case_study.domains import spec_of, DOMAIN_SPECS
from case_study.domains.spec import WorkstationSpec
from case_study.domains.orders import DomainOrderGenerator
from case_study.study2 import LineSimulator, _rng
from case_study.records import Study2Row
from compiler.uncertainty import GROUP_A, GROUP_B, UncertaintyEvent

def simulate(job, model_path):
    group, name, seed, config = job
    simulator = LineSimulator(config, model_path=model_path)
    sync_inventory = simulator._sync_inventory
    recolor = simulator._recolor
    peak = 0

    def checked_inventory(items, t):
        nonlocal peak
        sync_inventory(items, t)
        for site, count in simulator.f_t.state.occupied.items():
            assert count <= simulator.f_t.capacity_of(site), (group, name, seed, t, site, count)
            peak = max(peak, count)

    def checked_coloring(items, t):
        planned = dict(simulator._planned)
        workset, lowerings, allocation, waiting = recolor(items, t)
        for vreg in workset.pending_vregs & planned.keys() & allocation.keys():
            assert allocation[vreg] == planned[vreg], (group, name, seed, t, vreg)
        return workset, lowerings, allocation, waiting

    simulator._sync_inventory = checked_inventory
    simulator._recolor = checked_coloring
    result = simulator.run(seed)
    audit = {"method": name, "seed": seed, "occupancy_peak": peak, "capacity_violations": 0,
             "ordinary_reassignments": 0,
             "deferred_rehandled_versions": simulator.deferred_rehandled_versions,
             "dispatch_reassignments": len(simulator.reassignment_records),
             "reassignment_busy_time": sum(record[3] for record in simulator.reassignment_records)}
    return group, Study2Row.of(result, method=name, region_rule=config.region_rule), audit

def register_physical_scenario(candidate):
    """同一 DomainSpec 同时生成工艺时长、物流坐标和目标资源模型。"""
    spec = spec_of(candidate['config']['domain'])
    physical = candidate['physical']
    key = spec.key + '__' + candidate['name']
    actions = tuple(replace(a, processing_time=physical['preparation_minutes'])
                    if a.description == 'Operator_Reassign' and 'preparation_minutes' in physical else a
                    for a in spec.actions)
    actions = tuple(replace(a, processing_time=physical['action_times'][a.description])
                    if a.description in physical.get('action_times', {}) else a for a in actions)
    modules = tuple(replace(m, quantity=physical.get('agvs', m.quantity)) if m.palu_id == 3 else
                    replace(m, quantity=1) if physical.get('recovery_bay') and m.palu_id == 5 else m
                    for m in spec.resource_modules)
    workstations = tuple(w for w in spec.workstations
                         if 'workstation_ids' not in physical or w.workstation_id in physical['workstation_ids'])
    if 'palus_by_workstation' in physical:
        workstations = tuple(replace(w, palus=tuple(physical['palus_by_workstation'][str(w.workstation_id)]))
                             for w in workstations)
    if 'coordinates' in physical:
        workstations = tuple(replace(w, row=physical['coordinates'][str(w.workstation_id)][0],
                                    col=physical['coordinates'][str(w.workstation_id)][1]) for w in workstations)
    if physical.get('recovery_bay'):
        workstations = tuple(replace(w, palus=tuple(p for p in w.palus if p != 5)) for w in workstations)
        bay_row, bay_col = physical.get('recovery_bay_coordinates', (1, 4))
        bay_registers = tuple(physical.get('recovery_bay_registers', (18, 19)))
        workstations += (WorkstationSpec(9, 'Recovery_Bay', bay_row, bay_col, bay_registers, (5, 3)),)
    if 'extra_workstations' in physical:
        workstations += tuple(WorkstationSpec(
            w['id'], w['type'], w['row'], w['col'], tuple(w['registers']), tuple(w['palus']))
            for w in physical['extra_workstations'])
    if any(k in physical for k in ('workstation_ids', 'palus_by_workstation', 'extra_workstations')):
        modules = tuple(m if m.palu_id == 3 else replace(m, quantity=sum(m.palu_id in w.palus for w in workstations))
                        for m in modules)
    # 场景声明哪些设备工序可产生硬件报警；订单已有的保护规则不变。
    catch_triggers = dict(spec.catch_triggers)
    for exception, pairs in physical.get('extra_catch_triggers', {}).items():
        catch_triggers[exception] = catch_triggers.get(exception, ()) + tuple(tuple(p) for p in pairs)
    declared = replace(spec, key=key, actions=actions, resource_modules=modules,
                       option_families=tuple(replace(f, setup_step=f.setup_step * physical.get('setup_factor', 1))
                                             for f in spec.option_families),
                       workstations=workstations, catch_triggers=catch_triggers,
                       holding_capacity=physical.get('holding_capacity', spec.holding_capacity),
                       rows=physical.get('rows', 2 if physical.get('recovery_bay') else spec.rows),
                       cols=physical.get('cols', 9 if physical.get('recovery_bay') else spec.cols))
    DOMAIN_SPECS[key] = declared
    return declared


def declared_event_mix(count, share, weights_a, weights_b, seed):
    """受控场景的种类配额：按比例取整总数，保留每seed独立随机发生顺序。"""
    count_a = round(count * share)
    kinds = []
    for size, pool, weights in ((count_a, GROUP_A, weights_a), (count-count_a, GROUP_B, weights_b)):
        fractions = [size * weight / sum(weights) for weight in weights]
        quotas = [int(value) for value in fractions]
        remainder = size - sum(quotas)
        priority = sorted(range(len(pool)), key=lambda i: fractions[i]-quotas[i], reverse=True)
        for index in priority[:remainder]:
            quotas[index] += 1
        kinds.extend(kind for kind, number in zip(pool, quotas) for _ in range(number))
    random.Random(f'event-mix:{seed}').shuffle(kinds)
    return kinds


def simulate_candidate(job, model, candidate):
    if 'physical' in candidate:
        register_physical_scenario(candidate)
    execute = LineSimulator._simulate
    generate = DomainOrderGenerator.generate
    uncertainty_events = LineSimulator._uncertainty_events

    def base_calendar(simulator, orders, seed):
        # 外生故障日历覆盖声明的生产班次，窗口不再随客户交期改变。
        events = uncertainty_events(simulator, orders, seed)
        start = candidate.get('calendar_start', min(item.order.arrival_time for item in orders))
        # 给定事件数，排序均匀样本对应齐次Poisson日历；幂变换声明非均匀发生强度。
        calendar_rng = random.Random(f'event-calendar:{seed}')
        random_times = sorted(calendar_rng.random() for _ in events) if candidate.get('random_event_times') else []
        balanced_kinds = declared_event_mix(
            len(events), simulator.config.group_a_share, candidate.get('group_a_weights', (1, 1, 1)),
            simulator.config.resource_event_weights, seed) if candidate.get('balanced_event_mix') else None
        shifted = []
        for index, event in enumerate(events):
            if balanced_kinds is not None:
                kind = balanced_kinds[index]
                event = replace(event, type=kind, event_id=f'{kind.key}-{index:04d}')
                lo, hi = simulator.config.repair_delay
                duration = (lo+(hi-lo)*_rng(seed,index,7) if kind.invalidates_resource else
                            simulator._recovery_duration(event))
                event = replace(event, resolves_at=event.time+duration)
            elif not event.type.invalidates_resource and 'group_a_weights' in candidate:
                # 仅声明外生物料/质量/工装异常的构成；恢复动作和执行器保持原实现。
                pool = tuple(kind for kind, weight in zip(GROUP_A, candidate['group_a_weights'])
                             for _ in range(weight))
                kind = pool[int(_rng(seed, index, 9) * len(pool))]
                event = replace(event, type=kind, event_id=f'{kind.key}-{index:04d}')
                event = replace(event, resolves_at=event.time+simulator._recovery_duration(event))
            when = event.time
            if 'event_window_minutes_per_order' in candidate:
                new_span = len(orders) * candidate['event_window_minutes_per_order']
                fraction = (random_times[index] if candidate.get('random_event_times') else
                            (index+1) / (len(events)+1))
                when = start + new_span * fraction ** candidate.get('event_time_power', 1.0)
            duration = event.resolves_at-event.time
            if event.type.key in candidate.get('event_duration_ranges', {}):
                lo, hi = candidate['event_duration_ranges'][event.type.key]
                duration = lo + (hi-lo) * _rng(seed, index, 7)
            if event.type.key in candidate.get('event_duration_modes', {}):
                pick = _rng(seed, index, 8)
                cumulative = 0.0
                for probability, lo, hi in candidate['event_duration_modes'][event.type.key]:
                    cumulative += probability
                    if pick < cumulative:
                        duration = lo + (hi-lo) * _rng(seed, index, 7)
                        break
            shifted.append(replace(event, time=when, resolves_at=when+duration))
        return shifted

    def production_calendar(simulator, orders, seed):
        if not candidate.get('additive_nonresource_events'):
            return base_calendar(simulator, orders, seed)
        # C的输入由B资源日历与独立非资源日历叠加；混合比例不重抽B的故障。
        config = simulator.config
        try:
            simulator.config = replace(config, group_a_share=0.0)
            events = base_calendar(simulator, orders, seed)
        finally:
            simulator.config = config
        count = round(len(events) * config.group_a_share / (1.0-config.group_a_share))
        kinds = declared_event_mix(count, 1.0, candidate.get('group_a_weights', (1, 1, 1)),
                                   config.resource_event_weights, f'nonresource:{seed}')
        rng = random.Random(f'nonresource-calendar:{seed}')
        fractions = (sorted(rng.random() for _ in kinds) if candidate.get('random_event_times')
                     else [(i+1)/(count+1) for i in range(count)])
        start = candidate.get('calendar_start', min(item.order.arrival_time for item in orders))
        span = len(orders) * candidate['event_window_minutes_per_order']
        for index, (kind, fraction) in enumerate(zip(kinds, fractions)):
            when = start + span * fraction ** candidate.get('event_time_power', 1.0)
            event = UncertaintyEvent(f'{kind.key}-additional-{index:04d}', kind, when)
            events.append(replace(event, resolves_at=when+simulator._recovery_duration(event)))
        return sorted(events, key=lambda event: event.time)

    def campaign_workload(generator, count, program_generator=None):
        orders = generate(generator, count, program_generator)
        if 'order_mix' in candidate:
            # 共用产品族与检验等级输入；交期保留原始随机松弛倍率，按新工艺负荷换算。
            mix = candidate['order_mix']
            mix_rng = random.Random(f'order-mix:{generator.seed}')
            selected = []
            for order in orders:
                options = dict(order.options)
                for family, models in mix.get('options', {}).items():
                    options[family] = mix_rng.choice(models)
                grade = mix_rng.choice(mix['grades']) if 'grades' in mix else order.quality_grade
                revised = replace(order, options=options, quality_grade=grade)
                before = generator._nominal_lead_time(order, program_generator)
                after = generator._nominal_lead_time(revised, program_generator)
                selected.append(replace(revised, due_time=order.arrival_time +
                    (order.due_time-order.arrival_time) * after/before))
            orders = selected
        if 'campaign_size' not in candidate:
            return orders
        size = candidate['campaign_size']
        models = next(f.models for f in generator.spec.option_families if f.family == 'Fabric')
        grouped = []
        for start in range(0, len(orders), size):
            grouped.extend(sorted(orders[start:start+size], key=lambda o: models.index(o.options['Fabric']),
                                  reverse=bool(start // size % 2)))
        return grouped

    def batch_workload(simulator, orders, mtba, seed):
        if candidate.get('regular_release'):
            # 节拍投料保留每张订单的承诺提前期；所有方法使用相同到达序列。
            for index, item in enumerate(orders):
                slack = item.order.due_time - item.order.arrival_time
                arrival = index * mtba
                item.order = replace(item.order, arrival_time=arrival, due_time=arrival+slack)
                item.due_time = item.order.due_time
            return execute(simulator, orders, mtba, seed)
        # 同一批订单在班次开始下达；交期只依赖声明工艺负荷，不依赖方法运行结果。
        loads = simulator._station_loads(orders)
        nominal_span = max(loads.get(ws, 0.0) / slots for ws, slots in simulator.workstation_slots.items())
        slacks = [item.order.due_time - item.order.arrival_time for item in orders]
        mean_slack = sum(slacks) / len(slacks)
        for item, slack in zip(orders, slacks):
            due = candidate['batch_due_factor'] * nominal_span * slack / mean_slack
            item.order = replace(item.order, arrival_time=0.0, due_time=due)
            item.due_time = due
        return execute(simulator, orders, mtba, seed)

    if 'batch_due_factor' in candidate or candidate.get('regular_release'):
        LineSimulator._simulate = batch_workload
    if 'campaign_size' in candidate or 'order_mix' in candidate:
        DomainOrderGenerator.generate = campaign_workload
    if any(key in candidate for key in ('event_window_minutes_per_order', 'event_duration_ranges',
                                        'event_duration_modes', 'group_a_weights', 'balanced_event_mix',
                                        'additive_nonresource_events')):
        LineSimulator._uncertainty_events = production_calendar
    try:
        return simulate(job, model)
    finally:
        LineSimulator._simulate = execute
        DomainOrderGenerator.generate = generate
        LineSimulator._uncertainty_events = uncertainty_events
