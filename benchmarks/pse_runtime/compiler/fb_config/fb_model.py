"""
Function Block 配置数据模型。

基于 IEC 61499 概念设计，支持 Basic FB (BFB) 与 Composite FB (CFB)。
与代码生成模块独立，仅供 FB 配置模块内部使用。
"""
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class Algorithm:
    """BFB 内部算法（对应文献中的 ALG_INI / ALG_RUN / ALG_SWITCH 等）。"""
    name: str
    description: str = ""


@dataclass
class ECCState:
    """执行控制图（ECC）状态节点。"""
    name: str
    algorithm: Optional[str] = None  # 关联的 Algorithm 名称
    outgoing_events: List[str] = field(default_factory=list)


@dataclass
class ECCTransition:
    """ECC 状态转换：由事件触发。"""
    source: str
    target: str
    event: str


@dataclass
class FBPort:
    """FB 端口（事件或数据）。"""
    name: str
    port_type: str  # "EVENT" | "DATA"
    direction: str  # "IN" | "OUT"
    data_type: Optional[str] = None  # 仅 DATA 端口使用，如 "INT", "STRING", "BOOL"
    comment: str = ""


@dataclass
class FBInterface:
    """FB 外部接口定义。"""
    event_inputs: List[FBPort] = field(default_factory=list)
    event_outputs: List[FBPort] = field(default_factory=list)
    data_inputs: List[FBPort] = field(default_factory=list)
    data_outputs: List[FBPort] = field(default_factory=list)


@dataclass
class FBInstance:
    """FB 网络中的实例（可为 BFB 或 CFB）。"""
    instance_id: str
    fb_type: str  # 类型名称，如 "Process_FB", "BlockCFB_1"
    x: int = 0
    y: int = 0
    comment: str = ""
    # 元数据：用于追溯来源
    block_id: Optional[int] = None
    instr_index: Optional[int] = None


@dataclass
class Connection:
    """事件或数据连接。"""
    source: str  # "InstanceName.PortName"
    destination: str
    conn_type: str  # "EVENT" | "DATA"
    comment: str = ""


@dataclass
class BasicFBDef:
    """Basic FB 类型定义。"""
    type_name: str
    interface: FBInterface = field(default_factory=FBInterface)
    algorithms: List[Algorithm] = field(default_factory=list)
    ecc_states: List[ECCState] = field(default_factory=list)
    ecc_transitions: List[ECCTransition] = field(default_factory=list)


@dataclass
class FBNetwork:
    """CFB 或 Application 内部的 FB 网络。"""
    instances: List[FBInstance] = field(default_factory=list)
    connections: List[Connection] = field(default_factory=list)


@dataclass
class CompositeFBDef:
    """Composite FB 类型定义。"""
    type_name: str
    interface: FBInterface = field(default_factory=FBInterface)
    internal_network: FBNetwork = field(default_factory=FBNetwork)


@dataclass
class FBConfig:
    """完整的 FB 配置结果，可被导出为 JSON 或 IEC 61499 XML。"""
    network_name: str = "FaLang_FB_Network"
    # 顶层网络（Application 级别）
    top_level: FBNetwork = field(default_factory=FBNetwork)
    # 类型定义库
    basic_fb_types: Dict[str, BasicFBDef] = field(default_factory=dict)
    composite_fb_types: Dict[str, CompositeFBDef] = field(default_factory=dict)
    # 追溯映射
    block_to_cfb: Dict[int, str] = field(default_factory=dict)
    instr_to_bfb: Dict[str, str] = field(default_factory=dict)
