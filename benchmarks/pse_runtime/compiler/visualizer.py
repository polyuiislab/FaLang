# F_IDE_Project/compiler/visualizer.py
import subprocess
import xml.etree.ElementTree as ET
import re

from compiler.ir import BranchInstr, JumpInstr, OpInstr, PhiInstr


class CFGVisualizer:
    @staticmethod
    def _escape_dot(value: str) -> str:
        return (value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _escape_dot_label(value: str) -> str:
        """
        Escape label text for Graphviz without destroying Graphviz escapes like \\n / \\l.
        We only escape double quotes and normalize real newlines.
        """
        value = value or ""
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = value.replace("\n", "\\n")
        return value.replace('"', '\\"')

    @staticmethod
    def _truncate_middle(text: str, max_len: int) -> str:
        text = text or ""
        if len(text) <= max_len:
            return text
        if max_len <= 3:
            return text[:max_len]
        head = (max_len - 3) // 2
        tail = max_len - 3 - head
        return f"{text[:head]}...{text[-tail:]}"

    @staticmethod
    def to_dot_cfg_only(cfg, graph_name="Factory_IR_CFG"):
        dot_lines = []
        dot_lines.append(f"digraph {graph_name} {{")
        dot_lines.append("    rankdir=TB;")
        dot_lines.append("    graph [splines=ortho, nodesep=0.45, ranksep=0.55, overlap=false];")
        # Use a broadly available monospace font to avoid browser fallback width mismatches that can
        # make text appear outside of the rendered node box.
        dot_lines.append('    node [shape=box, fontname="Courier New", fontsize=10, margin="0.22,0.12", style=filled, fillcolor="#f4f4f4"];')
        dot_lines.append('    edge [color="#333333"];')

        for block in cfg.blocks:
            # Use \\l to force left-justified line breaks in Graphviz SVG output
            label_lines = [f"Block_{block.id} [{block.name}]", "-" * 30]
            for instr in block.instructions:
                s = str(instr)
                if len(s) <= 88:
                    label_lines.append(s)
                    continue
                # Soft-wrap long instruction lines to prevent text overflow in SVG boxes.
                # Prefer breaking at ", " and keep subsequent lines indented.
                parts = s.replace(", ", ",\u0000").split("\u0000")
                cur = ""
                for p in parts:
                    if len(p) > 88:
                        # Break very long tokens (e.g., long identifiers) at underscores when possible.
                        segs = []
                        rest = p
                        while len(rest) > 88:
                            cut = rest.rfind("_", 0, 88)
                            if cut <= 10:
                                cut = 88
                            segs.append(rest[:cut])
                            rest = "  " + rest[cut:].lstrip("_ ")
                        if rest:
                            segs.append(rest)
                        for si, sp in enumerate(segs):
                            if si == 0:
                                p = sp
                            else:
                                label_lines.append("  " + sp.lstrip())
                        # Continue wrapping the first segment via normal flow.
                    if not cur:
                        cur = p
                        continue
                    if len(cur) + len(p) <= 88:
                        cur += p
                    else:
                        label_lines.append(cur)
                        cur = "  " + p.lstrip()
                if cur:
                    label_lines.append(cur)
            label_text = "\\l".join(label_lines) + "\\l"

            tooltip = ""
            if hasattr(block, "get_source_range_summary"):
                tooltip = CFGVisualizer._escape_dot(block.get_source_range_summary())

            dot_lines.append(
                f'    B{block.id} [label="{CFGVisualizer._escape_dot_label(label_text)}", tooltip="{tooltip}", id="B{block.id}", class="cfg-node"];'
            )

            for succ in block.successors:
                dot_lines.append(f"    B{block.id} -> B{succ.id};")

        dot_lines.append("}")
        return "\n".join(dot_lines)

    @staticmethod
    def export_dot_file(cfg, filename="ssa_output.dot"):
        dot_content = CFGVisualizer.to_dot_cfg_only(cfg)
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(dot_content)
            return True, filename
        except Exception as e:
            return False, str(e)

    @staticmethod
    def export_svg(cfg, svg_filename="factory_ssa_cfg.svg", dot_filename="factory_ssa_cfg.dot"):
        dot_content = CFGVisualizer.to_dot_cfg_only(cfg)
        try:
            with open(dot_filename, "w", encoding="utf-8") as f:
                f.write(dot_content)

            dot_proc = subprocess.run(
                ["dot", "-Tsvg", dot_filename],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            cfg_svg = dot_proc.stdout

            combined_svg = CFGVisualizer._compose_svg_with_nav(cfg, cfg_svg)
            with open(svg_filename, "w", encoding="utf-8") as f:
                f.write(combined_svg)
            return True, svg_filename
        except Exception as e:
            return False, str(e)

    @staticmethod
    def export_interactive_html(cfg, html_filename="factory_ssa_cfg.html", dot_filename="factory_ssa_cfg.dot"):
        try:
            ok, dot_path_or_err = CFGVisualizer.export_dot_file(cfg, dot_filename)
            if not ok:
                return False, dot_path_or_err

            ok, svg_path_or_err = CFGVisualizer.export_svg(cfg, "factory_ssa_cfg.svg", dot_filename)
            if not ok:
                return False, svg_path_or_err

            html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>FaLang CFG Visualization</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 12px; }}
    .note {{ padding: 8px; background: #f6faff; border: 1px solid #c7d9f4; margin: 6px 0; }}
    code {{ background: #f3f3f3; padding: 2px 4px; border-radius: 3px; }}
  </style>
</head>
<body>
  <h2>FaLang CFG Visualization</h2>
  <p>DOT file: <code>{dot_filename}</code></p>
  <p>SVG file: <code>factory_ssa_cfg.svg</code></p>
  <p class="note">Hover a navigation item or a CFG block to show the dashed mapping line.</p>
</body>
</html>
"""
            with open(html_filename, "w", encoding="utf-8") as f:
                f.write(html_content)
            return True, html_filename
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _parse_graphviz_svg(cfg_svg: str):
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

        root = ET.fromstring(cfg_svg)
        svg_ns = "http://www.w3.org/2000/svg"

        def q(tag: str) -> str:
            return f"{{{svg_ns}}}{tag}"

        width_attr = root.attrib.get("width", "0pt")
        height_attr = root.attrib.get("height", "0pt")
        try:
            cfg_width = float(width_attr.replace("pt", ""))
            cfg_height = float(height_attr.replace("pt", ""))
        except Exception:
            cfg_width, cfg_height = 0.0, 0.0

        graph_group = None
        for child in root.findall(q("g")):
            if child.attrib.get("id") == "graph0":
                graph_group = child
                break

        node_centers = {}
        if graph_group is not None:
            for g in graph_group.findall(q("g")):
                class_attr = g.attrib.get("class", "") or ""
                if "node" not in class_attr.split():
                    continue
                title_el = g.find(q("title"))
                if title_el is None or not title_el.text:
                    continue
                title_text = title_el.text.strip()
                if not title_text.startswith("B"):
                    continue
                block_id_str = title_text[1:]
                if not block_id_str.isdigit():
                    continue
                block_id = int(block_id_str)

                min_x = min_y = None
                max_x = max_y = None
                for shape_tag in ("polygon", "rect"):
                    for shape in g.findall(f".//{q(shape_tag)}"):
                        if shape_tag == "polygon":
                            pts = shape.attrib.get("points", "")
                            for pair in pts.split():
                                if "," not in pair:
                                    continue
                                x_str, y_str = pair.split(",", 1)
                                try:
                                    x = float(x_str)
                                    y = float(y_str)
                                except Exception:
                                    continue
                                min_x = x if min_x is None else min(min_x, x)
                                min_y = y if min_y is None else min(min_y, y)
                                max_x = x if max_x is None else max(max_x, x)
                                max_y = y if max_y is None else max(max_y, y)
                        else:
                            try:
                                x = float(shape.attrib.get("x", "0"))
                                y = float(shape.attrib.get("y", "0"))
                                w = float(shape.attrib.get("width", "0"))
                                h = float(shape.attrib.get("height", "0"))
                            except Exception:
                                continue
                            min_x = x if min_x is None else min(min_x, x)
                            min_y = y if min_y is None else min(min_y, y)
                            max_x = (x + w) if max_x is None else max(max_x, x + w)
                            max_y = (y + h) if max_y is None else max(max_y, y + h)

                if min_x is None or min_y is None or max_x is None or max_y is None:
                    continue

                node_centers[block_id] = {
                    "cx": (min_x + max_x) / 2.0,
                    "cy": (min_y + max_y) / 2.0,
                    "left": min_x,
                    "right": max_x,
                }

                g.attrib["data-block-id"] = str(block_id)

        return root, graph_group, cfg_width, cfg_height, node_centers

    @staticmethod
    def _compose_svg_with_nav(cfg, cfg_svg: str) -> str:
        _cfg_root, cfg_graph_group, cfg_width, cfg_height, node_centers = CFGVisualizer._parse_graphviz_svg(cfg_svg)

        def _wrap_text(text: str, max_chars: int) -> list[str]:
            text = (text or "").strip()
            if not text:
                return [""]
            if max_chars <= 10:
                return [text]

            def _split_long_token(tok: str) -> list[str]:
                tok = tok or ""
                if len(tok) <= max_chars:
                    return [tok]
                # Prefer breaking identifiers at underscores.
                parts = tok.split("_") if "_" in tok else [tok]
                out = []
                cur = ""
                for p in parts:
                    piece = p if not out and not cur else ("_" + p if cur or out else p)
                    if not cur:
                        cur = piece
                        continue
                    if len(cur) + len(piece) <= max_chars:
                        cur += piece
                    else:
                        out.append(cur)
                        cur = piece.lstrip("_")
                if cur:
                    out.append(cur)
                # If still too long (no underscores / huge chunks), hard-slice.
                final = []
                for s in out:
                    if len(s) <= max_chars:
                        final.append(s)
                        continue
                    for i in range(0, len(s), max_chars):
                        final.append(s[i : i + max_chars])
                return final

            words = text.replace(",", ", ").split()
            expanded = []
            for w in words:
                expanded.extend(_split_long_token(w))
            words = expanded
            lines = []
            cur = ""
            for w in words:
                if not cur:
                    cur = w
                    continue
                if len(cur) + 1 + len(w) <= max_chars:
                    cur = f"{cur} {w}"
                else:
                    lines.append(cur)
                    cur = w
            if cur:
                lines.append(cur)
            return lines

        def _op_summaries(block, max_ops: int = 6) -> list[dict]:
            ops = []
            for instr in getattr(block, "instructions", []):
                if not isinstance(instr, OpInstr):
                    continue
                ops.append(
                    {
                        "op": instr.op_type,
                        "args": instr.args or [],
                        "params": instr.params,
                    }
                )
                if len(ops) >= max_ops:
                    break
            return ops

        def summarize_block(block) -> str:
            """
            Manufacturing-semantics summary (对象/地点/事件) for the navigation bar.
            Prefer describing *what* happens to *which* object, *where*, and *which* job/event.
            """
            name = (getattr(block, "name", "") or "").strip()
            ops = _op_summaries(block, max_ops=10)

            material_names = set(getattr(cfg, "material_names", set()) or set())
            resource_names = set(getattr(cfg, "resource_names", set()) or set())

            def _base(sym: str) -> str:
                s = (sym or "").strip()
                if not s:
                    return ""
                return re.sub(r"_\\d+$", "", s)

            def _is_resource(sym: str) -> bool:
                b = _base(sym)
                return bool(b) and b in resource_names

            def _is_material(sym: str) -> bool:
                b = _base(sym)
                if b and b in material_names:
                    return True
                # Heuristic: internal names commonly used for workpieces/WIP in SSA lowering.
                return b.startswith("item") or b.startswith("part") or b.startswith("out")

            def _parse_event(params: str) -> str:
                p = (params or "").strip().strip('"')
                if not p:
                    return ""
                head = p.split(";", 1)[0].strip()
                if ";" not in p:
                    return CFGVisualizer._truncate_middle(head, 52) if head else ""
                tail = p.split(";", 1)[1].strip()
                if not tail:
                    return CFGVisualizer._truncate_middle(head, 52) if head else ""
                tail = CFGVisualizer._truncate_middle(tail, 32)
                if head:
                    return f"{head} ({tail})"
                return tail

            def _add_unique(dst: list[str], v: str):
                v = (v or "").strip()
                if not v or v in dst:
                    return
                dst.append(v)

            important_ops = {"Load", "Move", "Store", "Process", "Inspect", "Adjust", "IsAvailable"}
            filtered = [o for o in ops if o.get("op") in important_ops]

            clauses: list[str] = []
            for o in filtered[:8]:
                op = o["op"]
                args = o.get("args") or []
                params = o.get("params")

                if op == "IsAvailable" and args:
                    target = args[0]
                    _add_unique(clauses, f"Check availability of {target}")
                    continue

                if op in ("Load", "Move", "Store") and args:
                    workpiece = args[0]
                    verb = {"Load": "Load", "Move": "Move", "Store": "Store"}[op]
                    if len(args) >= 2:
                        _add_unique(clauses, f"{verb} {workpiece} to {args[1]}")
                    else:
                        _add_unique(clauses, f"{verb} {workpiece}")
                    continue

                if op in ("Process", "Inspect") and len(args) >= 2:
                    device = args[0]
                    workpiece = " and ".join(args[1:])
                    ev = _parse_event(params)
                    if ev:
                        if op == "Process":
                            _add_unique(clauses, f"At {device}, process {workpiece} with {ev}")
                        else:
                            _add_unique(clauses, f"At {device}, inspect {workpiece} with {ev}")
                    else:
                        if op == "Process":
                            _add_unique(clauses, f"At {device}, process {workpiece}")
                        else:
                            _add_unique(clauses, f"At {device}, inspect {workpiece}")
                    continue

                if op == "Adjust" and args:
                    device = args[0]
                    ev = _parse_event(params)
                    if ev:
                        _add_unique(clauses, f"Adjust {device}: {ev}")
                    else:
                        _add_unique(clauses, f"Adjust {device}")
                    continue

            prefix = ""
            if name.startswith("If_"):
                if name == "If_True":
                    prefix = "If branch (true): "
                elif name == "If_False":
                    prefix = "If branch (false): "
                else:
                    prefix = "Branch merge: "
            elif name.startswith("Catch_"):
                exc = name.replace("Catch_", "") or "Exception"
                prefix = f"Exception handler ({exc}): "
            elif name == "Compensate":
                prefix = "Compensation: "
            elif name == "Resume":
                prefix = "Resume: "
            elif name == "Entry":
                prefix = "Entry: "

            if not clauses:
                if any(isinstance(i, (BranchInstr, JumpInstr)) for i in getattr(block, "instructions", [])):
                    return f"{prefix}Control transfer / merge."
                return f"{prefix}No-op / SSA-only."

            # Keep compact and sentence-like.
            chosen = clauses[:2]
            sentence = "; then ".join(chosen)
            if not sentence.endswith("."):
                sentence += "."
            return f"{prefix}{sentence}"

        nav_items = []
        for block in sorted(cfg.blocks, key=lambda b: b.id):
            start_line = None
            end_line = None
            if hasattr(block, "source_ranges") and block.source_ranges:
                starts = [r[0] for r in block.source_ranges if r and r[0] is not None]
                ends = [r[1] for r in block.source_ranges if r and r[1] is not None]
                start_line = min(starts) if starts else None
                end_line = max(ends) if ends else None
            summary = summarize_block(block)
            nav_items.append((block.id, start_line, end_line, summary))

        nav_width = 520.0
        nav_pad_x = 12.0
        nav_pad_y = 14.0
        row_gap = 8.0
        header_h = 26.0
        # Estimated; actual height computed while laying out items
        nav_height = nav_pad_y * 2 + header_h + (len(nav_items) * (28.0 + row_gap) - (row_gap if nav_items else 0))

        gap = 40.0
        out_width = nav_width + gap + cfg_width
        out_height = max(nav_height, cfg_height)

        svg_ns = "http://www.w3.org/2000/svg"

        def q(tag: str) -> str:
            return f"{{{svg_ns}}}{tag}"

        out_root = ET.Element(
            q("svg"),
            {
                "width": f"{out_width}pt",
                "height": f"{out_height}pt",
                "viewBox": f"0 0 {out_width} {out_height}",
            },
        )

        defs = ET.SubElement(out_root, q("defs"))
        style = ET.SubElement(defs, q("style"), {"type": "text/css"})
        style.text = """
        .nav-bg { fill: #d0e6ff; stroke: #aac8f2; }
        .nav-item { fill: #e8f2ff; stroke: #333333; cursor: pointer; }
        .nav-text { font-family: Consolas, monospace; font-size: 10px; fill: #111111; }
        .nav-title { font-family: Arial, sans-serif; font-size: 14px; font-weight: bold; fill: #0b2a4a; }
        .cfg-highlight polygon, .cfg-highlight rect { stroke: #1a73e8 !important; stroke-width: 2.2 !important; }
        .nav-highlight rect { stroke: #1a73e8 !important; stroke-width: 2.2 !important; }
        .pinned polygon, .pinned rect { stroke: #1a73e8 !important; stroke-width: 2.2 !important; }
        .pinned rect { stroke: #1a73e8 !important; stroke-width: 2.2 !important; }
        .cfg-node { cursor: pointer; }
        """

        script = ET.SubElement(defs, q("script"), {"type": "application/javascript"})
        script.text = """
        (function() {
          var pinned = null;
          function showFor(id) {
            var nav = document.getElementById('nav-B' + id);
            if (nav) nav.classList.add('nav-highlight');
            var cfg = document.querySelector('[data-block-id=\"' + id + '\"]');
            if (cfg) cfg.classList.add('cfg-highlight');
          }
          function hideFor(id) {
            var nav = document.getElementById('nav-B' + id);
            if (nav) nav.classList.remove('nav-highlight');
            var cfg = document.querySelector('[data-block-id=\"' + id + '\"]');
            if (cfg) cfg.classList.remove('cfg-highlight');
          }
          function setPinned(id) {
            if (pinned) {
              var oldNav = document.getElementById('nav-B' + pinned);
              if (oldNav) oldNav.classList.remove('pinned');
              var oldCfg = document.querySelector('[data-block-id=\"' + pinned + '\"]');
              if (oldCfg) oldCfg.classList.remove('pinned');
            }
            pinned = id;
            if (pinned) {
              var nav = document.getElementById('nav-B' + pinned);
              if (nav) nav.classList.add('pinned');
              var cfg = document.querySelector('[data-block-id=\"' + pinned + '\"]');
              if (cfg) cfg.classList.add('pinned');
            }
          }
          function findAttrUp(node, attr) {
            var cur = node;
            while (cur && cur.getAttribute) {
              var v = cur.getAttribute(attr);
              if (v) return v;
              cur = cur.parentNode;
            }
            return null;
          }
          var active = null;
          function onOver(evt) {
            var id = findAttrUp(evt.target, 'data-nav-block-id') || findAttrUp(evt.target, 'data-block-id');
            if (!id) return;
            if (active === id) return;
            if (active) hideFor(active);
            active = id;
            showFor(id);
          }
          function onOut(evt) {
            // If moving within the same block group, ignore.
            var fromId = findAttrUp(evt.target, 'data-nav-block-id') || findAttrUp(evt.target, 'data-block-id');
            var toId = (evt.relatedTarget && (findAttrUp(evt.relatedTarget, 'data-nav-block-id') || findAttrUp(evt.relatedTarget, 'data-block-id'))) || null;
            if (fromId && toId && fromId === toId) return;
            if (fromId && active === fromId) {
              hideFor(fromId);
              active = null;
            }
          }
          function onClick(evt) {
            var id = findAttrUp(evt.target, 'data-nav-block-id') || findAttrUp(evt.target, 'data-block-id');
            if (!id) return;
            if (pinned === id) {
              setPinned(null);
            } else {
              setPinned(id);
            }
          }
          window.addEventListener('load', function() {
            document.documentElement.addEventListener('mouseover', onOver, true);
            document.documentElement.addEventListener('mouseout', onOut, true);
            document.documentElement.addEventListener('click', onClick, true);
          });
        })();
        """

        nav_group = ET.SubElement(out_root, q("g"), {"id": "nav"})
        ET.SubElement(
            nav_group,
            q("rect"),
            {"x": "0", "y": "0", "width": str(nav_width), "height": str(out_height), "class": "nav-bg"},
        )
        ET.SubElement(nav_group, q("text"), {"x": str(nav_pad_x), "y": str(nav_pad_y + 16), "class": "nav-title"}).text = (
            "Source Navigation"
        )

        y = nav_pad_y + header_h
        for (block_id, start_line, end_line, summary) in nav_items:
            if start_line is not None and end_line is not None:
                label = f"Block {block_id}. Lines {start_line}-{end_line}: {summary}"
            else:
                label = f"Block {block_id}. {summary}"
            lines = _wrap_text(label, max_chars=68)
            line_h = 12.5
            pad_y = 6.0
            item_h = pad_y * 2 + line_h * len(lines)

            item_group = ET.SubElement(
                nav_group,
                q("g"),
                {"id": f"nav-B{block_id}", "data-nav-block-id": str(block_id)},
            )
            ET.SubElement(item_group, q("title")).text = label
            ET.SubElement(
                item_group,
                q("rect"),
                {
                    "x": str(nav_pad_x),
                    "y": str(y),
                    "width": str(nav_width - nav_pad_x * 2),
                    "height": str(item_h),
                    "rx": "4",
                    "ry": "4",
                    "class": "nav-item",
                },
            )
            text_el = ET.SubElement(
                item_group,
                q("text"),
                {"x": str(nav_pad_x + 10), "y": str(y + pad_y + line_h), "class": "nav-text"},
            )
            first = True
            for ln in lines:
                if first:
                    tspan = ET.SubElement(text_el, q("tspan"), {"x": str(nav_pad_x + 10)})
                    tspan.text = ln
                    first = False
                else:
                    tspan = ET.SubElement(text_el, q("tspan"), {"x": str(nav_pad_x + 10), "dy": str(line_h)})
                    tspan.text = ln

            y += item_h + row_gap

        cfg_offset_x = nav_width + gap
        cfg_container = ET.SubElement(out_root, q("g"), {"id": "cfg", "transform": f"translate({cfg_offset_x},0)"})
        if cfg_graph_group is not None:
            cfg_container.append(cfg_graph_group)

        # Update output height to fit the nav if needed
        if y + nav_pad_y > out_height:
            out_height = y + nav_pad_y
            out_root.attrib["height"] = f"{out_height}pt"
            out_root.attrib["viewBox"] = f"0 0 {out_width} {out_height}"
            nav_bg = nav_group.find(q("rect"))
            if nav_bg is not None:
                nav_bg.attrib["height"] = str(out_height)

        return ET.tostring(out_root, encoding="utf-8", xml_declaration=True).decode("utf-8")
