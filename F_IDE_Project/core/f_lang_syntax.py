import re

class FLangSyntax:
    """Define FaLang token patterns and Tkinter highlighting styles."""

    def __init__(self):
        self.colors = {
            "KEYWORD": "#FF7F50",
            "TYPE": "#25BAE7",
            "FUNCTION": "#A6E22E",
            "STRING": "#FF8C00",
            "COMMENT": "#75715E",
            "NUMBER": "#B0E0E6",
            "OPERATOR": "#F8F8F2",
            "NORMAL": "#F8F8F2",
            "BACKGROUND": "#272822",
            "CURSOR": "#F8F8F0",
            "SELECTION": "#49483E",
            "BRACKET_L1": "#FFD700",
            "BRACKET_L2": "#FF69B4",
            "BRACKET_L3": "#00FFFF",
            "BRACKET_L4": "#90EE90",
            "BRACKET_L5": "#DDA0DD",
            "BRACKET_L6": "#F0E68C"
        }

        # Match comments and strings before general token rules.
        self.patterns = [
            (r'(?:#|//).*', "COMMENT"),

            (r'(?:(?:"(?:[^"\\]|\\.)*")|(?:\'(?:[^\'\\]|\\.)*\'))', "STRING"),

            (r'\b(?:Task|Parallel|Constraint|after|if|else|foreach|return|try|catch|compensate)\b', "KEYWORD"),

            (r'\b(?:Resource|Storage|Buffer|Device|Material|Register|Memory|Struct|Cluster|Time|ResourceFault|MaterialMissing|Timeout)\b', "TYPE"),

            (r'\b(?:Load|Store|Process|Move|Inspect|Rework|Adjust|IsAvailable)\b', "FUNCTION"),

            (r'\b\d+(?:\.\d+)?(?:s|ms|kg|g|m|mm)\b', "NUMBER"),

            (r'\b\d+(?:\.\d+)?\b', "NUMBER"),

            (r'(?<!/)[=+\-*/<>,:;{}()\[\]](?!/)', "OPERATOR")
        ]

    def get_background_color(self):
        return self.colors.get("BACKGROUND", "#FFFFFF")

    def get_foreground_color(self):
        return self.colors.get("NORMAL", "#000000")

    def get_insert_color(self):
        return self.colors.get("CURSOR", "#000000")

    def get_selection_color(self):
        return self.colors.get("SELECTION", "#BBDDFF")

    def apply_tags(self, text_widget):
        for tag, color in self.colors.items():
            text_widget.tag_configure(tag, foreground=color)

    def _get_bracket_depth_at_position(self, content, pos):
        depth = 0
        bracket_pairs = {'(': ')', '[': ']', '{': '}'}
        opening_brackets = set(bracket_pairs.keys())
        closing_brackets = set(bracket_pairs.values())

        for i in range(min(pos, len(content))):
            char = content[i]
            if char in opening_brackets:
                depth += 1
            elif char in closing_brackets:
                depth = max(0, depth - 1)

        return depth

    def highlight(self, text_widget):
        for tag in self.colors.keys():
            text_widget.tag_remove(tag, "1.0", "end")

        content = text_widget.get("1.0", "end")

        marked_ranges = set()

        for pattern, tag in self.patterns:
            for match in re.finditer(pattern, content, re.MULTILINE):
                # Tk indexes start at 1.0; character offsets handle multiline matches.
                start_idx = f"1.0+{match.start()}c"
                end_idx = f"1.0+{match.end()}c"

                is_in_comment = any(
                    marked_start <= match.start() < marked_end or
                    marked_start < match.end() <= marked_end
                    for marked_start, marked_end, marked_tag in marked_ranges
                    if marked_tag == "COMMENT"
                )

                if not is_in_comment:
                    if tag == "OPERATOR" and match.group() in '()[]{}':
                        depth = self._get_bracket_depth_at_position(content, match.start())
                        # Opening brackets use the next depth; closing brackets use the current depth.
                        if match.group() in '([{' :
                            bracket_level = depth + 1
                        else:
                            bracket_level = depth
                        bracket_level = (bracket_level % 6) if bracket_level > 0 else 1
                        bracket_tag = f"BRACKET_L{bracket_level}"
                        if bracket_tag in self.colors:
                            text_widget.tag_add(bracket_tag, start_idx, end_idx)
                        else:
                            text_widget.tag_add(tag, start_idx, end_idx)
                    else:
                        text_widget.tag_add(tag, start_idx, end_idx)

                    marked_ranges.add((match.start(), match.end(), tag))