"""只读保存订单完工事实，并计算事先声明的交期敏感性。

仅适用于交期不影响派工和外生事件日历的实验；本模块不验证仿真配置。
记录中的 due_time 已包含 base_factor，重新评价不修改原始记录。
"""
from __future__ import annotations

from math import isfinite
from statistics import mean


def _finite(value, label):
    """未完成及非有限时间不能作为完整样本参与交期评价。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} 必须是有限数值") from error
    if not isfinite(number):
        raise ValueError(f"{label} 必须是有限数值")
    return number


def _validated_records(records):
    checked = []
    seen = set()
    for record in records:
        order_id = record.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("order_id 必须是非空字符串")
        if order_id in seen:
            raise ValueError(f"订单重复：{order_id}")
        seen.add(order_id)
        values = {key: _finite(record.get(key), f"{order_id}/{key}")
                  for key in ("arrival_time", "due_time", "finish_time")}
        checked.append(dict(order_id=order_id, **values))
    return checked


def collect_completion_records(items):
    """导出每张运行时订单的原始到达、承诺交期及实际完工时刻。

    不读取策略或重新推算完工；任何订单尚未完成时直接报错，避免删去难例。
    due_time 取仿真最终使用的运行时字段，保持与原输出指标一致。
    """
    records = [dict(order_id=item.compiled.program.order_id,
                    arrival_time=item.order.arrival_time,
                    due_time=item.due_time,
                    finish_time=item.finish_time) for item in items]
    return _validated_records(records)


def evaluate_due_factor(records, base_factor, new_factor):
    """按原到达时刻缩放交期松弛量，返回未舍入的迟交均值及准交率。"""
    base = _finite(base_factor, "base_factor")
    target = _finite(new_factor, "new_factor")
    if base <= 0 or target <= 0:
        raise ValueError("交期因子必须大于零")
    checked = _validated_records(records)
    if not checked:
        raise ValueError("没有完工订单，无法计算交期指标")
    tardiness = []
    on_time = 0
    for record in checked:
        arrival = record["arrival_time"]
        # 原因子下直接读取原交期，避免往返运算改变恰好准时的浮点边界。
        due = _finite(record["due_time"] if target == base else
                      arrival + (record["due_time"] - arrival) * target / base,
                      f'{record["order_id"]}/scaled_due_time')
        delay = _finite(record["finish_time"] - due,
                        f'{record["order_id"]}/delay')
        tardiness.append(max(0.0, delay))
        on_time += record["finish_time"] <= due
    return dict(mean_tardiness=mean(tardiness),
                on_time_delivery_rate=on_time / len(checked))
