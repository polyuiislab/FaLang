"""Data model for Basic FBs, Composite FBs, and their event and data connections."""
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class Algorithm:

    name: str
    description: str = ""


@dataclass
class ECCState:

    name: str
    algorithm: Optional[str] = None  
    outgoing_events: List[str] = field(default_factory=list)


@dataclass
class ECCTransition:

    source: str
    target: str
    event: str


@dataclass
class FBPort:

    name: str
    port_type: str  # EVENT or DATA.
    direction: str  # IN or OUT.
    data_type: Optional[str] = None  # Data-port type, such as INT, STRING, or BOOL.
    comment: str = ""


@dataclass
class FBInterface:

    event_inputs: List[FBPort] = field(default_factory=list)
    event_outputs: List[FBPort] = field(default_factory=list)
    data_inputs: List[FBPort] = field(default_factory=list)
    data_outputs: List[FBPort] = field(default_factory=list)


@dataclass
class FBInstance:

    instance_id: str
    fb_type: str  
    x: int = 0
    y: int = 0
    comment: str = ""

    block_id: Optional[int] = None
    instr_index: Optional[int] = None


@dataclass
class Connection:

    source: str  # Endpoint syntax: InstanceName.PortName.
    destination: str
    conn_type: str  
    comment: str = ""


@dataclass
class BasicFBDef:

    type_name: str
    interface: FBInterface = field(default_factory=FBInterface)
    algorithms: List[Algorithm] = field(default_factory=list)
    ecc_states: List[ECCState] = field(default_factory=list)
    ecc_transitions: List[ECCTransition] = field(default_factory=list)


@dataclass
class FBNetwork:

    instances: List[FBInstance] = field(default_factory=list)
    connections: List[Connection] = field(default_factory=list)


@dataclass
class CompositeFBDef:

    type_name: str
    interface: FBInterface = field(default_factory=FBInterface)
    internal_network: FBNetwork = field(default_factory=FBNetwork)


@dataclass
class FBConfig:

    network_name: str = "FaLang_FB_Network"

    top_level: FBNetwork = field(default_factory=FBNetwork)

    basic_fb_types: Dict[str, BasicFBDef] = field(default_factory=dict)
    composite_fb_types: Dict[str, CompositeFBDef] = field(default_factory=dict)

    block_to_cfb: Dict[int, str] = field(default_factory=dict)
    instr_to_bfb: Dict[str, str] = field(default_factory=dict)
