"""Export Function Block configurations as JSON and IEC 61499 System XML."""
import json
import os
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from compiler.fb_config.fb_model import FBConfig, FBPort, BasicFBDef, CompositeFBDef


class FBExporter:


    @staticmethod
    def export_json(config: FBConfig, filepath: str) -> str:

        data = FBExporter._config_to_dict(config)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return filepath

    @staticmethod
    def export_iec61499_xml(config: FBConfig, filepath: str) -> str:

        xml_bytes = FBExporter._config_to_xml(config)
        with open(filepath, "wb") as f:
            f.write(xml_bytes)
        return filepath


    @staticmethod
    def _config_to_dict(config: FBConfig) -> dict:
        def port_to_dict(p: FBPort) -> dict:
            return {
                "name": p.name,
                "port_type": p.port_type,
                "direction": p.direction,
                "data_type": p.data_type,
                "comment": p.comment,
            }

        def interface_to_dict(iface) -> dict:
            return {
                "event_inputs": [port_to_dict(p) for p in iface.event_inputs],
                "event_outputs": [port_to_dict(p) for p in iface.event_outputs],
                "data_inputs": [port_to_dict(p) for p in iface.data_inputs],
                "data_outputs": [port_to_dict(p) for p in iface.data_outputs],
            }

        def bfb_to_dict(b: BasicFBDef) -> dict:
            return {
                "type_name": b.type_name,
                "interface": interface_to_dict(b.interface),
                "algorithms": [{"name": a.name, "description": a.description} for a in b.algorithms],
                "ecc_states": [
                    {"name": s.name, "algorithm": s.algorithm, "outgoing_events": s.outgoing_events}
                    for s in b.ecc_states
                ],
                "ecc_transitions": [
                    {"source": t.source, "target": t.target, "event": t.event}
                    for t in b.ecc_transitions
                ],
            }

        def cfb_to_dict(c: CompositeFBDef) -> dict:
            return {
                "type_name": c.type_name,
                "interface": interface_to_dict(c.interface),
                "internal_network": {
                    "instances": [
                        {
                            "instance_id": i.instance_id,
                            "fb_type": i.fb_type,
                            "block_id": i.block_id,
                            "instr_index": i.instr_index,
                            "x": i.x,
                            "y": i.y,
                            "comment": i.comment,
                        }
                        for i in c.internal_network.instances
                    ],
                    "connections": [
                        {
                            "source": conn.source,
                            "destination": conn.destination,
                            "conn_type": conn.conn_type,
                            "comment": conn.comment,
                        }
                        for conn in c.internal_network.connections
                    ],
                },
            }

        return {
            "network_name": config.network_name,
            "top_level": {
                "instances": [
                    {
                        "instance_id": i.instance_id,
                        "fb_type": i.fb_type,
                        "block_id": i.block_id,
                        "x": i.x,
                        "y": i.y,
                        "comment": i.comment,
                    }
                    for i in config.top_level.instances
                ],
                "connections": [
                    {
                        "source": conn.source,
                        "destination": conn.destination,
                        "conn_type": conn.conn_type,
                        "comment": conn.comment,
                    }
                    for conn in config.top_level.connections
                ],
            },
            "basic_fb_types": {k: bfb_to_dict(v) for k, v in config.basic_fb_types.items()},
            "composite_fb_types": {k: cfb_to_dict(v) for k, v in config.composite_fb_types.items()},
            "mappings": {
                "block_to_cfb": config.block_to_cfb,
                "instr_to_bfb": config.instr_to_bfb,
            },
        }


    @staticmethod
    def _config_to_xml(config: FBConfig) -> bytes:
        system = Element("System", {"Name": config.network_name})


        for bfb in config.basic_fb_types.values():
            FBExporter._append_bfb_type(system, bfb)


        for cfb in config.composite_fb_types.values():
            FBExporter._append_cfb_type(system, cfb)


        app = SubElement(system, "Application", {"Name": "FaLangApp"})
        tln = SubElement(app, "FBNetwork")

        for inst in config.top_level.instances:
            SubElement(tln, "FB", {
                "Name": inst.instance_id,
                "Type": inst.fb_type,
                "x": str(inst.x),
                "y": str(inst.y),
            })

        ev_conns = SubElement(tln, "EventConnections")
        dt_conns = SubElement(tln, "DataConnections")
        for conn in config.top_level.connections:
            if conn.conn_type == "EVENT":
                SubElement(ev_conns, "Connection", {
                    "Source": conn.source,
                    "Destination": conn.destination,
                    "Comment": conn.comment,
                })
            else:
                SubElement(dt_conns, "Connection", {
                    "Source": conn.source,
                    "Destination": conn.destination,
                    "Comment": conn.comment,
                })


        header = b'<?xml version="1.0" encoding="UTF-8"?>\n'
        body = tostring(system, encoding="utf-8")
        return header + body

    @staticmethod
    def _append_bfb_type(parent: Element, bfb: BasicFBDef):
        fb_type = SubElement(parent, "FBType", {"Name": bfb.type_name})
        iface = SubElement(fb_type, "InterfaceList")

        if bfb.interface.event_inputs:
            ei = SubElement(iface, "EventInputs")
            for p in bfb.interface.event_inputs:
                SubElement(ei, "Event", {"Name": p.name})
        if bfb.interface.event_outputs:
            eo = SubElement(iface, "EventOutputs")
            for p in bfb.interface.event_outputs:
                SubElement(eo, "Event", {"Name": p.name})
        if bfb.interface.data_inputs:
            di = SubElement(iface, "InputVars")
            for p in bfb.interface.data_inputs:
                var = SubElement(di, "VarDeclaration", {
                    "Name": p.name,
                    "Type": p.data_type or "STRING",
                })
                if p.comment:
                    var.set("Comment", p.comment)
        if bfb.interface.data_outputs:
            do = SubElement(iface, "OutputVars")
            for p in bfb.interface.data_outputs:
                SubElement(do, "VarDeclaration", {
                    "Name": p.name,
                    "Type": p.data_type or "STRING",
                })


        basic_fb = SubElement(fb_type, "BasicFB")
        ecc = SubElement(basic_fb, "ECC")
        for s in bfb.ecc_states:
            state_el = SubElement(ecc, "ECState", {"Name": s.name})
            if s.algorithm:
                SubElement(state_el, "ECAction", {"Algorithm": s.algorithm, "Output": ""})
        for t in bfb.ecc_transitions:
            SubElement(ecc, "ECTransition", {
                "Source": t.source,
                "Destination": t.target,
                "Condition": t.event,
            })
        for a in bfb.algorithms:
            SubElement(basic_fb, "Algorithm", {"Name": a.name})

    @staticmethod
    def _append_cfb_type(parent: Element, cfb: CompositeFBDef):
        fb_type = SubElement(parent, "FBType", {"Name": cfb.type_name})
        iface = SubElement(fb_type, "InterfaceList")

        if cfb.interface.event_inputs:
            ei = SubElement(iface, "EventInputs")
            for p in cfb.interface.event_inputs:
                SubElement(ei, "Event", {"Name": p.name})
        if cfb.interface.event_outputs:
            eo = SubElement(iface, "EventOutputs")
            for p in cfb.interface.event_outputs:
                SubElement(eo, "Event", {"Name": p.name})
        if cfb.interface.data_inputs:
            di = SubElement(iface, "InputVars")
            for p in cfb.interface.data_inputs:
                SubElement(di, "VarDeclaration", {
                    "Name": p.name,
                    "Type": p.data_type or "STRING",
                })
        if cfb.interface.data_outputs:
            do = SubElement(iface, "OutputVars")
            for p in cfb.interface.data_outputs:
                SubElement(do, "VarDeclaration", {
                    "Name": p.name,
                    "Type": p.data_type or "STRING",
                })


        fb_net = SubElement(fb_type, "FBNetwork")
        for inst in cfb.internal_network.instances:
            SubElement(fb_net, "FB", {
                "Name": inst.instance_id,
                "Type": inst.fb_type,
                "x": str(inst.x),
                "y": str(inst.y),
            })

        ev_conns = SubElement(fb_net, "EventConnections")
        dt_conns = SubElement(fb_net, "DataConnections")
        for conn in cfb.internal_network.connections:
            src = conn.source
            dst = conn.destination
            # An empty instance name denotes the enclosing CFB interface.
            if src.startswith("."):
                src = ""
            if dst.startswith("."):
                dst = ""
            if conn.conn_type == "EVENT":
                SubElement(ev_conns, "Connection", {
                    "Source": src,
                    "Destination": dst,
                })
            else:
                SubElement(dt_conns, "Connection", {
                    "Source": src,
                    "Destination": dst,
                })
