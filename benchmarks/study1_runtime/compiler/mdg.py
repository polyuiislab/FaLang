from typing import List, Dict, Optional, Set
from compiler.ir import IRInstr, BasicBlock, ControlFlowGraph


class MDGNode:
    _id_counter = 0

    def __init__(self, instr: Optional[IRInstr], block: BasicBlock, node_type: str = "op"):
        self.id = MDGNode._id_counter
        MDGNode._id_counter += 1
        self.instr = instr          # Entry and exit nodes have no instruction.
        self.block = block
        self.node_type = node_type  
        self.out_edges: List["MDGEdge"] = []
        self.in_edges: List["MDGEdge"] = []

    def __repr__(self):
        return f"MDGNode({self.id}, {self.node_type})"


class MDGEdge:
    def __init__(self, src: MDGNode, dst: MDGNode, weight: float = 0.0, edge_type: str = "data"):
        self.src = src
        self.dst = dst
        self.weight = weight
        self.edge_type = edge_type  # Dependency kind: data, control, exc, or struct.

    def __repr__(self):
        return f"MDGEdge({self.src.id} -> {self.dst.id}, w={self.weight}, t={self.edge_type})"


class ManufacturingDependencyGraph:
    def __init__(self):
        self.nodes: List[MDGNode] = []
        self.edges: List[MDGEdge] = []
        self.instr_to_node: Dict[int, MDGNode] = {}      
        self.block_entry: Dict[int, MDGNode] = {}        
        self.block_exit: Dict[int, MDGNode] = {}         
        self.unreachable_blocks: Set[int] = set()        

    def add_node(self, node: MDGNode) -> MDGNode:
        self.nodes.append(node)
        return node

    def add_edge(self, edge: MDGEdge) -> MDGEdge:
        self.edges.append(edge)
        edge.src.out_edges.append(edge)
        edge.dst.in_edges.append(edge)
        return edge

    def get_node_for_instr(self, instr: IRInstr) -> Optional[MDGNode]:
        return self.instr_to_node.get(id(instr))

    def to_dot(self, graph_name: str = "Manufacturing_Dependency_Graph") -> str:

        from compiler.visualizer import CFGVisualizer

        lines = []
        lines.append(f"digraph {graph_name} {{")
        lines.append("    rankdir=TB;")
        lines.append('    node [shape=box, fontname="Courier New", fontsize=10];')
        lines.append('    edge [fontname="Courier New", fontsize=9];')

        for node in self.nodes:
            label = f"[{node.node_type}] B{node.block.id}_{node.id}"
            if node.instr is not None:
                label += f"\n{str(node.instr)}"
            label = CFGVisualizer._escape_dot_label(label)


            if node.block.id in self.unreachable_blocks:
                lines.append(
                    f'    N{node.id} [label="{label}", style="filled,dashed", fillcolor="#e0e0e0", color="#999999"];'
                )
            else:
                lines.append(f'    N{node.id} [label="{label}"];')

        for edge in self.edges:
            edge_label = f"w={edge.weight:.1f}\n{edge.edge_type}"
            edge_label = CFGVisualizer._escape_dot_label(edge_label)
            lines.append(
                f'    N{edge.src.id} -> N{edge.dst.id} '
                f'[label="{edge_label}"];'
            )

        lines.append("}")
        return "\n".join(lines)
