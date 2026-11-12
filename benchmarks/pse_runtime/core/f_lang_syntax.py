import re

class FLangSyntax:
    """
    F-Lang (Factory Language) 语法高亮规则定义类。
    该类负责维护 F-Lang 的词法规则，并将正则表达式映射到 Tkinter Text 组件的 tag 样式。
    
    设计依据：
    根据 F-Lang 提案 ，语言包含资源声明、任务定义、并发控制等核心概念。
    配色方案采用了深色背景下的高对比度色调，以适应工厂控制室的弱光环境。
    """

    def __init__(self):
        # 定义语法高亮的配色方案 (Monokai 风格变体)
        self.colors = {
            "KEYWORD": "#FF7F50",      # 珊瑚色：用于结构性关键字 (Task, Parallel)
            "TYPE": "#25BAE7",         # 深蓝色：用于类型定义 (Resource, Storage)
            "FUNCTION": "#A6E22E",     # 绿色：用于内置工艺函数 (Load, Process)
            # "STRING": "#E6DB74",       # 黄色：用于字符串字面量
            "STRING": "#FF8C00",       # 橙色：用于字符串字面量
            "COMMENT": "#75715E",      # 灰色：用于注释
            # "NUMBER": "#AE81FF",       # 紫色：用于数值和寄存器索引
            # "OPERATOR": "#F92672",     # 粉红：用于操作符
            # "OPERATOR": "#F8F8F2",     # 粉红：用于操作符
            "NUMBER": "#B0E0E6",       # 淡青色：用于数值和寄存器索引
            "OPERATOR": "#F8F8F2",     # 白色：用于操作符
            "NORMAL": "#F8F8F2",       # 白色：普通文本
            "BACKGROUND": "#272822",   # 深灰：编辑器背景
            "CURSOR": "#F8F8F0",       # 光标颜色
            # "SELECTION": "#49483E"     # 选中区域背景
            "SELECTION": "#49483E",    # 选中区域背景
            # 括号嵌套颜色（彩色括号）
            "BRACKET_L1": "#FFD700",   # 金黄色：第1层括号
            "BRACKET_L2": "#FF69B4",   # 粉红色：第2层括号
            "BRACKET_L3": "#00FFFF",   # 青色：第3层括号
            "BRACKET_L4": "#90EE90",   # 浅绿色：第4层括号
            "BRACKET_L5": "#DDA0DD",   # 梅紫色：第5层括号
            "BRACKET_L6": "#F0E68C"    # 卡其色：第6层括号
        }

        # 定义正则表达式模式列表
        # 列表中的顺序至关重要：Python 会按顺序匹配，先匹配到的规则生效。
        # 因此，注释和字符串等特定规则必须放在通用规则（如操作符）之前。
        self.patterns = [
            # 1. 注释（行注释）
            #(r'#.*', "COMMENT"),
            # 1. 注释（行注释：支持 # 和 //）
            (r'(?:#|//).*', "COMMENT"),

            # 2. 字符串（支持单/双引号与转义）
            (r'(?:(?:"(?:[^"\\]|\\.)*")|(?:\'(?:[^\'\\]|\\.)*\'))', "STRING"),

            # 3. 结构关键字 (Keywords): 任务、并发、流程控制
            (r'\b(?:Task|Parallel|Constraint|after|if|else|foreach|return|try|catch|compensate)\b', "KEYWORD"),

            # 4. 类型与资源关键字 (Types): 物理实体定义
            (r'\b(?:Resource|Storage|Buffer|Device|Material|Register|Memory|Struct|Cluster|Time|ResourceFault|MaterialMissing|Timeout)\b', "TYPE"),

            # 5. 动作指令/函数 (Functions): 对应 FISA 指令的操作码
            (r'\b(?:Load|Store|Process|Move|Inspect|Rework|Adjust|IsAvailable)\b', "FUNCTION"),

            # 6. 物理单位 (Units): 如 10s, 5kg (放在纯数字前匹配)
            (r'\b\d+(?:\.\d+)?(?:s|ms|kg|g|m|mm)\b', "NUMBER"),

            # 7. 纯数值 (Numbers): 整数或浮点数
            (r'\b\d+(?:\.\d+)?\b', "NUMBER"),

            # 8. 操作符与标点 (Operators): 各种符号，包括冒号 ':'
            # 注意：- 在字符类中需要放在末尾或转义
            #(r'[=+\-*/<>,:;{}()\[\]]', "OPERATOR")
            # 注意：排除 // 不匹配为操作符（已在注释规则中处理）
            (r'(?<!/)[=+\-*/<>,:;{}()\[\]](?!/)', "OPERATOR")
        ]

    def get_background_color(self):
        """返回编辑器背景色字符串（容错默认）。"""
        return self.colors.get("BACKGROUND", "#FFFFFF")

    def get_foreground_color(self):
        """返回正文前景色字符串（容错默认）。"""
        return self.colors.get("NORMAL", "#000000")

    def get_insert_color(self):
        """返回光标颜色字符串（容错默认）。"""
        return self.colors.get("CURSOR", "#000000")
        
    def get_selection_color(self):
        """返回选中区域背景色字符串（容错默认）。"""
        return self.colors.get("SELECTION", "#BBDDFF")

    def apply_tags(self, text_widget):
        """
        将定义的颜色样式配置到 Tkinter Text 组件的 tag 系统中。
        """
        for tag, color in self.colors.items():
            # 为每种类型的 token 创建对应的 tag
            text_widget.tag_configure(tag, foreground=color)

    def _get_bracket_depth_at_position(self, content, pos):
        """
        计算给定位置处的括号嵌套深度。
        支持的括号对：() [] {}
        """
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
        """
        核心高亮逻辑：扫描整个文档内容，应用正则表达式，并为匹配的文本打上 tag。
        
        注意：对于超大文件，全量扫描可能会有性能问题。
        但在工厂配方（Recipe）场景下，脚本通常在几百行以内，全量扫描的响应速度（<10ms）完全满足需求。
        """
        # 1. 清除旧的 tags (保留 selection 和光标)
        for tag in self.colors.keys():
            text_widget.tag_remove(tag, "1.0", "end")

        # 2. 获取全部文本内容
        content = text_widget.get("1.0", "end")
        
        # 3. 用于记录已标记的区间，避免在注释内重复标记
        marked_ranges = set()
        
        # 4. 逐个模式匹配并应用 tag
        for pattern, tag in self.patterns:
            for match in re.finditer(pattern, content, re.MULTILINE):
                # 计算匹配文本在 Tkinter 中的索引位置 (line.column)
                # 注意：Tkinter 索引从 1.0 开始，而 python 字符串索引从 0 开始
                # 这种转换在处理多行文本时比较复杂，这里使用简化的字符计数法
                start_idx = f"1.0+{match.start()}c"
                end_idx = f"1.0+{match.end()}c"

                # 检查当前位置是否已被标记为 COMMENT，如果是则跳过
                is_in_comment = any(
                    marked_start <= match.start() < marked_end or
                    marked_start < match.end() <= marked_end
                    for marked_start, marked_end, marked_tag in marked_ranges
                    if marked_tag == "COMMENT"
                )
                
                if not is_in_comment:
                    # 对于括号操作符，使用嵌套颜色标签
                    if tag == "OPERATOR" and match.group() in '()[]{}':
                        depth = self._get_bracket_depth_at_position(content, match.start())
                        # 根据括号类型调整深度：左括号使用 depth+1，右括号使用 depth
                        if match.group() in '([{' :
                            bracket_level = depth + 1
                        else:
                            bracket_level = depth
                        # 约束深度在1-6之间循环
                        bracket_level = (bracket_level % 6) if bracket_level > 0 else 1
                        bracket_tag = f"BRACKET_L{bracket_level}"
                        if bracket_tag in self.colors:
                            text_widget.tag_add(bracket_tag, start_idx, end_idx)
                        else:
                            text_widget.tag_add(tag, start_idx, end_idx)
                    else:
                        text_widget.tag_add(tag, start_idx, end_idx)
                    
                    # 记录已标记的范围
                    marked_ranges.add((match.start(), match.end(), tag))