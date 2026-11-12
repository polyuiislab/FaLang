# Pre-allocation scheduling

The full IDE compilation pipeline schedules manufacturing operations before PBQP register allocation. `RegionScheduler` coordinates CFG blocks, and `ListScheduler` schedules operations within each block. The RGC case-study entry point is described separately in [Architecture](../../docs/architecture.md).

## Inputs and resource state

The scheduler uses the CFG, manufacturing dependency graph (MDG), and target model. `ResourceState` tracks:

- availability of individual PALU instances;
- material ownership, location type, and ready time;
- occupancy and capacity of memory and register storage;
- device setup history.

A material version has one physical holder. An operation can issue when its dependencies, compatible PALU availability, input-material readiness, and destination capacity permit it.

## Scheduling within a block

`ListScheduler.schedule_block()` maintains ready and active operations. Completed operations release resources and make dependent operations ready. Feasible ready operations are considered in descending priority, with resource availability checked after each selection.

The implemented priority combines:

```text
critical-path weight
+ alpha * setup pressure
+ beta * resource scarcity
+ structural-hazard priority
```

Critical-path weights follow MDG edges. Setup pressure reflects outgoing setup costs; scarcity measures demand relative to compatible PALU quantities. Structural-hazard priority favors lower setup costs when ready operations compete for those units.

Time normally advances to the next completion or resource release. A positive `scheduling_tick` limits the time increment. Each issued operation receives `scheduled_start` and `scheduled_finish` annotations.

## Propagation across blocks

`RegionScheduler.schedule()` visits CFG blocks in topological order and passes their exit states to successors. At a join, PALU availability uses the latest predecessor time. Material state is retained when predecessors agree; otherwise, its location is unknown and its ready time is the latest predecessor value. Storage occupancy is the union of incoming occupancy sets.

The current implementation schedules individual blocks and reports each block as one entry in `mfr_boundaries`. Catch and compensation operations remain in their CFG blocks and use the same physical resource constraints. Runtime state and device setup history can be supplied through `initial_state_override` and `device_last_model_override`.

## Outputs

`ScheduleResult` contains block schedules, global makespan, device and material timelines, cache occupancy summaries, region boundaries, and block exit states. These results support register allocation and subsequent scheduling in the full compilation pipeline.

Relevant files:

- `scheduler.py`: resource state, readiness, priorities, and block scheduling.
- `scheduler_region.py`: CFG traversal and predecessor-state merging.
- `mdg_builder.py`: manufacturing dependencies.
- `post_ra_scheduler.py`: scheduling after physical register allocation.
