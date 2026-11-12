"""只读生产恢复计时：异常发生至直接受影响工序恢复执行（仿真分钟）。"""
from statistics import mean
from case_study.statistics import percentile


class RecoveryTracker:
    """每个外部事件单独跟踪；派工重报不重复建立事件。

    界限取事件前分配中直接失效的待执行工序，每张订单只取最早的一道。
    此界限不使用策略选择的重编译区域，避免边界小的方法少计恢复责任。
    """
    def __init__(self):
        self.items = {}
        self.starts = {}
        self.records = []

    def register(self, items):
        self.items={item.compiled.program.order_id:item for item in items}

    def sync(self, items):
        for item in items:
            if item.executing is not None:
                self.starts.setdefault((item.compiled.program.order_id,item.executing),item.op_start)

    def add(self,event,affected_versions,path=None):
        if event.detail.get("source_event"):
            return
        targets={}
        for order_id,item in self.items.items():
            if item.finish_time is not None:
                continue
            for index in range(item.completed_seq+1,len(item.ops)):
                # 已在加工/在途的工序物理位置锁定，不作为新启动恢复对象。
                if index==item.executing or (index==item.completed_seq+1 and item.in_transit):
                    continue
                op=item.ops[index]
                definitions=set(op.implicit_targets or ()) | ({op.target} if op.target else set())
                if definitions & affected_versions:
                    targets[order_id]=index
                    break
        owner=self.items.get(event.order_id)
        if owner is not None and owner.finish_time is None:
            index=owner.completed_seq+1
            if not event.type.invalidates_resource:
                if index==owner.executing:
                    index+=1
                # 未即时处理的质量/工装缺陷在检验或末工序前执行实体恢复。
                if event.type.key in {"quality_fault","tooling_deviation"}:
                    index=next((i for i in range(index,len(owner.ops))
                                if owner.ops[i].op_type=="Inspect"),len(owner.ops)-1)
                if index<len(owner.ops):
                    targets[event.order_id]=index
            elif event.detail.get("startup_failure"):
                targets[event.order_id]=index
        self.records.append(dict(event_id=event.event_id,kind=event.type.key,
                                 occurrence_time=event.time,targets=targets,path=path))

    def finish(self):
        for record in self.records:
            times=[self.starts.get((order,index)) for order,index in record["targets"].items()]
            known=all(t is not None and t+1e-9>=record["occurrence_time"] for t in times)
            record["censored"]=not known
            record["affected_orders"]=len(times)
            record["recovered_at"]=max(times,default=record["occurrence_time"]) if known else None
            record["recovery_time_min"]=(record["recovered_at"]-record["occurrence_time"]) if known else None
        result=self.summarize(self.records,"event_recovery")
        # 各类事件分别列出，防止把完全覆盖处理器的收益归给部分覆盖机制。
        for path in ("partial","contained","inexecutable","unanticipated","no_active_owner"):
            selected=[r for r in self.records if r["path"]==path]
            result.update(self.summarize(selected,path+"_recovery"))
        return result

    @staticmethod
    def summarize(records,prefix):
        active=[r for r in records if r["affected_orders"]]
        closed=[r["recovery_time_min"] for r in active if not r["censored"]]
        censored=sum(r["censored"] for r in active)
        values=dict(events=len(active),censored=censored,unaffected=len(records)-len(active),
                    affected_order_occurrences=sum(r["affected_orders"] for r in active),
                    total_min=sum(closed) if not censored else None,
                    mean_min=mean(closed) if closed else None,
                    p50_min=percentile(closed,.5) if closed else None,
                    p95_min=percentile(closed,.95) if closed else None,
                    max_min=max(closed) if closed else None)
        return {prefix+"_"+key:value for key,value in values.items()}
