#!/usr/bin/env python3
import os
import sys
import re
import json
import argparse
import httpx  # [新增依赖] 用于高效处理 API 请求
from dataclasses import dataclass, asdict
from typing import List, Optional

# 在 import 区域下方加入这些 ANSI 颜色常量
class TerminalColor:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

# ==========================================
# 1. 领域模型 (AST / 中间表示)
# ==========================================

class Timecode:
    """时间戳对象，内部统一使用总毫秒数存储，彻底杜绝字符串计算错误"""
    def __init__(self, total_ms: int):
        self.total_ms = max(0, total_ms)

    @classmethod
    def from_string(cls, ts: str) -> 'Timecode':
        """将各种乱七八糟的字符串（包括纯秒数）解析为标准的时间戳对象"""
        ts_clean = (str(ts).lower()
                    .replace("h", ":").replace("m", ":")
                    .replace("时", ":").replace("分", ":")
                    .replace(",", ".")
                    .replace("s", "").replace("秒", "")
                    .strip())
        
        # 如果为空字符串，直接返回 0
        if not ts_clean or ts_clean == 'none':
            return cls(0)

        parts = ts_clean.split(":")
        
        try:
            # 🎯 修复关键点：支持没有冒号的纯秒数格式 (例如 "12.5" 或 "150")
            if len(parts) == 1:   
                h, m, s_str = 0, 0, parts[0]
            elif len(parts) == 2: # mm:ss.ms
                h, m, s_str = 0, int(parts[0]), parts[1]
            elif len(parts) == 3: # hh:mm:ss.ms
                h, m, s_str = int(parts[0]), int(parts[1]), parts[2]
            else:
                return cls(0)
            
            if '.' in s_str:
                s_val, ms_str = s_str.split('.')
                s = int(s_val)
                # 自动补齐毫秒：比如 .5 会变成 .500，.23 会变成 .230
                ms = int(ms_str.ljust(3, "0")[:3])
            else:
                s, ms = int(s_str), 0
                
            total_ms = (h * 3600 + m * 60 + s) * 1000 + ms
            return cls(total_ms)
        except ValueError:
            return cls(0)

    def add_seconds(self, seconds: float) -> 'Timecode':
        return Timecode(self.total_ms + int(seconds * 1000))

    def to_standard(self, separator='.') -> str:
        """输出标准格式: HH:MM:SS.mmm (或根据separator变更为 HH:MM:SS,mmm)"""
        total_s = self.total_ms // 1000
        ms = self.total_ms % 1000
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}{separator}{ms:03d}"

    def to_lrc_format(self) -> str:
        """输出 LRC 常用格式: mm:ss.xx"""
        total_s = self.total_ms // 1000
        ms = self.total_ms % 1000
        m = total_s // 60
        s = total_s % 60
        xx = ms // 10  # LRC 通常只保留两位小数
        return f"{m:02d}:{s:02d}.{xx:02d}"


@dataclass
class SubtitleCue:
    """单一字幕节点 (AST Node)"""
    start: Timecode
    end: Timecode
    text: str

    def clean_text(self):
        self.text = self.text.strip()


class SubtitleDocument:
    """字幕文档 (AST Root)"""
    def __init__(self):
        self.cues: List[SubtitleCue] = []

    def add_cue(self, start: Timecode, end: Timecode, text: str):
        cue = SubtitleCue(start, end, text)
        cue.clean_text()
        if cue.text:  # 只有包含文本才加入
            self.cues.append(cue)


# ==========================================
# 2. 解析器 (Parser: Raw File -> AST)
# ==========================================

# 修复1：允许匹配末尾的 s 或 秒，以及中文字符“时、分”
TIMESTAMP_PATTERN = r'(\d{1,2}[:h时]\d{1,2}(?:[:m分]\d{1,2})?(?:[\.,]\d{1,3})?[s秒]?)'

# 修复2：极度宽容的连接符，允许时间戳本身被括号独立包裹，中间允许出现任意的 - ~ > ] [ 空格
RANGE_PATTERN = re.compile(
    r'[\[\(\{]?\s*' + TIMESTAMP_PATTERN + 
    r'\s*[\]\)\}]?[\s\-\–\—\~\>\]\[]+[\[\(\{]?\s*' + 
    TIMESTAMP_PATTERN + r'\s*[\]\)\}]?'
)
SINGLE_PATTERN = re.compile(TIMESTAMP_PATTERN)

def safe_read(file_path: str) -> str:
    """健壮的文件读取：自动处理不同编码"""
    for encoding in ['utf-8-sig', 'utf-8', 'gbk']:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码: {file_path}")

class ParserFactory:
    @staticmethod
    def parse(file_path: str) -> Optional[SubtitleDocument]:
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        content = safe_read(file_path)
        doc = SubtitleDocument()

        try:
            if ext == '.json':
                ParserFactory._parse_json(content, doc)
            elif ext == '.sbv':
                ParserFactory._parse_sbv(content, doc)
            elif ext == '.lrc':
                ParserFactory._parse_lrc(content, doc)
            else:
                ParserFactory._parse_text(content, doc)
            
            return doc if doc.cues else None
        except Exception as e:
            print(f"解析 {file_path} 时发生错误: {e}")
            return None

    @staticmethod
    def _parse_json(content: str, doc: SubtitleDocument):
        import json
        data = json.loads(content)
        for item in data:
            # 嗅探模式 1：包含 "timestamp" 数组 (如 Whisper: {"timestamp": [1.5, 3.2]})
            if "timestamp" in item and isinstance(item["timestamp"], list) and len(item["timestamp"]) >= 2:
                start_str = str(item["timestamp"][0])
                end_str = str(item["timestamp"][1])
            # 嗅探模式 2：包含 "start" 和 "end" 或者 "start_time" 和 "end_time"
            else:
                # 兼容不同的命名习惯 (start_time 或 start)
                start_str = str(item.get("start_time", item.get("start", 0)))
                end_str = str(item.get("end_time", item.get("end", 0)))
            
            # 兼容不同的文本键名 (text 或 content)
            text_str = item.get("text", item.get("content", ""))
            
            doc.add_cue(
                start=Timecode.from_string(start_str),
                end=Timecode.from_string(end_str),
                text=text_str
            )

    @staticmethod
    def _parse_sbv(content: str, doc: SubtitleDocument):
        blocks = content.split('\n\n')
        for block in blocks:
            lines = [l.strip() for l in block.split('\n') if l.strip()]
            if len(lines) >= 2:
                times = lines[0].split(',')
                if len(times) == 2:
                    doc.add_cue(
                        start=Timecode.from_string(times[0]),
                        end=Timecode.from_string(times[1]),
                        text="\n".join(lines[1:])
                    )

    @staticmethod
    def _parse_lrc(content: str, doc: SubtitleDocument):
        temp_cues = []
        for line in content.splitlines():
            line = line.strip()
            if not line: continue
            
            ts_matches = list(SINGLE_PATTERN.finditer(line))
            if not ts_matches: continue
            
            last_match = ts_matches[-1]
            text = line[last_match.end():].strip()
            if not text: continue

            for m in ts_matches:
                temp_cues.append({
                    "start": Timecode.from_string(m.group(1)),
                    "text": text
                })
        
        # LRC 缺失结束时间，需要根据上下文补全
        temp_cues.sort(key=lambda x: x["start"].total_ms)
        for i, cue_data in enumerate(temp_cues):
            start_tc = cue_data["start"]
            if i + 1 < len(temp_cues):
                end_tc = temp_cues[i+1]["start"]
            else:
                end_tc = start_tc.add_seconds(3.0) # 最后一句默认持续3秒
            doc.add_cue(start_tc, end_tc, cue_data["text"])

    @staticmethod
    def _parse_text(content: str, doc: SubtitleDocument):
        """状态机解析：支持标准范围(A->B) 和 连续单时间戳(A text B text) 两种极端情况"""
        
        # 1. 如果文本中能找到任何类似于范围时间轴 (A --> B) 的痕迹，走标准状态机
        if RANGE_PATTERN.search(content):
            current_start = None
            current_end = None
            current_text = []

            def flush_cue():
                nonlocal current_start, current_end, current_text
                if current_start and current_end and current_text:
                    doc.add_cue(current_start, current_end, "\n".join(current_text))
                current_start = current_end = None
                current_text = []

            for line in content.splitlines():
                line = line.strip()
                if not line or line.isdigit(): 
                    continue

                range_match = RANGE_PATTERN.search(line)
                if range_match:
                    flush_cue()
                    current_start = Timecode.from_string(range_match.group(1))
                    current_end = Timecode.from_string(range_match.group(2))
                    inline_text = line[range_match.end():].strip()
                    if inline_text:
                        current_text.append(inline_text)
                elif current_start:
                    current_text.append(line)
            flush_cue()
            
        # 2. 如果没有范围时间轴，则启动“连续剧本切割”模式！(专门对付你发的那种格式)
        else:
            # 匹配可能被括号包裹的单时间戳，比如 (0:00), [0:10.50] 或者直接是 0:15
            inline_pattern = re.compile(r'[\[\(\{]?\s*' + TIMESTAMP_PATTERN + r'\s*[\]\)\}]?')
            matches = list(inline_pattern.finditer(content))
            
            if not matches:
                return # 彻底解析失败，既没有范围时间，也没有单时间戳
                
            temp_cues = []
            # 像切香肠一样，把两刀（时间戳）之间的肉（文本）切下来
            for i, match in enumerate(matches):
                # match.group(1) 是纯净的时间戳部分，去掉了外面的括号
                start_tc = Timecode.from_string(match.group(1))
                
                # 当前文本的起点是当前时间戳的末尾
                text_start = match.end()
                # 当前文本的终点是下一个时间戳的开头（如果是最后一个，终点就是文件末尾）
                text_end = matches[i+1].start() if i + 1 < len(matches) else len(content)
                
                text = content[text_start:text_end].strip()
                if text:
                    temp_cues.append({"start": start_tc, "text": text})
            
            # 自动推算结束时间：当前句的结束 = 下一句的开始
            for i, cue_data in enumerate(temp_cues):
                start_tc = cue_data["start"]
                if i + 1 < len(temp_cues):
                    end_tc = temp_cues[i+1]["start"]
                else:
                    # 最后一句哲人的结语，默认给它停留 5 秒钟
                    end_tc = start_tc.add_seconds(5.0)
                doc.add_cue(start_tc, end_tc, cue_data["text"])
        
        


# ==========================================
# 3. 生成器 (Writer: AST -> Target Format)
# ==========================================

class WriterFactory:
    @staticmethod
    def write(doc: SubtitleDocument, format_type: str, out_path: str):
        with open(out_path, "w", encoding="utf-8") as f:
            if format_type == "srt":
                WriterFactory._write_srt(doc, f)
            elif format_type == "sbv":
                WriterFactory._write_sbv(doc, f)
            elif format_type == "json":
                WriterFactory._write_json(doc, f)
            elif format_type == "lrc":
                WriterFactory._write_lrc(doc, f)
            elif format_type == "txt":
                for cue in doc.cues:
                    # 纯文本模式：保留换行符，用以人类阅读
                    f.write(f"[{cue.start.to_standard()} --> {cue.end.to_standard()}] {cue.text.strip()}\n\n")

    @staticmethod
    def _write_srt(doc: SubtitleDocument, file_obj):
        """严格遵守 SubRip (.srt) 规范的输出逻辑"""
        for i, cue in enumerate(doc.cues, 1):
            start_time = cue.start.to_standard(separator=',') 
            end_time = cue.end.to_standard(separator=',')
            clean_text = cue.text.strip()
            file_obj.write(f"{i}\n{start_time} --> {end_time}\n{clean_text}\n\n")

    @staticmethod
    def _write_sbv(doc: SubtitleDocument, file_obj):
        """严格遵守 YouTube SubViewer (.sbv) 规范的输出逻辑"""
        for cue in doc.cues:
            start_time = cue.start.to_standard(separator='.')
            end_time = cue.end.to_standard(separator='.')
            clean_text = cue.text.strip()
            file_obj.write(f"{start_time},{end_time}\n{clean_text}\n\n")

    @staticmethod
    def _write_json(doc: SubtitleDocument, file_obj):
        """输出高可读性、结构化的 JSON 格式"""
        import json
        out_data = [{
            "timestamp": [cue.start.to_standard(separator='.'), cue.end.to_standard(separator='.')],
            "text": cue.text.strip()
        } for cue in doc.cues]
        json.dump(out_data, file_obj, ensure_ascii=False, indent=2)

    @staticmethod
    def _write_lrc(doc: SubtitleDocument, file_obj):
        """严格遵守 LRC 歌词规范的输出逻辑"""
        # (可选) 写入标准的 LRC ID Tags 元数据，显得更专业
        file_obj.write("[ti:Subtitle Converted by AST]\n")
        file_obj.write("[by:Super Programmer]\n")
        
        for cue in doc.cues:
            # 1. 强制将多行字幕合并为单行，用空格代替换行符，防止破坏 LRC 结构！
            # 比如 "你好\n世界" 会变成 "你好 世界"
            single_line_text = cue.text.strip().replace('\n', '  ')
            
            # 2. 标准规范：标签 [mm:ss.xx] 和文本之间通常不留多余的空格
            timestamp = cue.start.to_lrc_format()
            file_obj.write(f"[{timestamp}]{single_line_text}\n")


# ==========================================
# 4. 主程序 (CLI)
# ==========================================

def print_banner():
    """打印实心、高亮、充满力量感的 SUBCOMPILER 横幅"""
    # 采用 ANSI 块状字符，确保实心效果
    banner = f"""
{TerminalColor.OKCYAN}{TerminalColor.BOLD}
 ██████  ██    ██ ██████   ██████  ██████  ███    ███ ██████  ██ ██      ███████ ██████  
██       ██    ██ ██   ██ ██      ██    ██ ████  ████ ██   ██ ██ ██      ██      ██   ██ 
 █████   ██    ██ ██████  ██      ██    ██ ██ ████ ██ ██████  ██ ██      █████   ██████  
     ██  ██    ██ ██   ██ ██      ██    ██ ██  ██  ██ ██      ██ ██      ██      ██   ██ 
 ██████   ██████  ██████   ██████  ██████  ██      ██ ██      ██ ███████ ███████ ██   ██ 
{TerminalColor.ENDC}
{TerminalColor.HEADER}>>> 多格式字幕/AST通用转换引擎 v2.0 <<<{TerminalColor.ENDC}
    """
    print(banner)

def main():
    epilog_text = f"""
{TerminalColor.OKCYAN}【核心功能 (Features)】{TerminalColor.ENDC}
  1. 强力清洗：自动修复错乱的时间轴（如缺失毫秒、纯秒数、奇葩分隔符、全角空格等）。
  2. 剧本切割：支持直接从连续的带有时间标记的纯文本对话中（如: "(0:15)青年：..."）自动切分字幕。
  3. 绝对规范：严格遵守各格式标准（SRT的逗号与空行、LRC的单行防断崖、JSON的规范缩进）。
  4. 批量引擎：支持一键处理整个文件夹中的所有乱码文件，自动防覆盖重命名。
  5. 时间魔法：支持整体时间轴精准平移（正数延后，负数提前），拯救音画不同步。

{TerminalColor.OKGREEN}【使用示例 (Examples)】{TerminalColor.ENDC}
  1. 基础转换 (将不规范文本转为标准 SRT):
     {TerminalColor.BOLD}subcomp.exe messy_test.txt -srt{TerminalColor.ENDC}

  2. 多格式同出 (将 JSON 转为 SRT、LRC 和 SBV):
     {TerminalColor.BOLD}subcomp.exe whisper_out.json -srt -lrc -sbv{TerminalColor.ENDC}

  3. 剧本提取与全格式生成:
     {TerminalColor.BOLD}subcomp.exe adler_dialogue.txt -all{TerminalColor.ENDC}

  4. 批量处理目录，并指定输出文件夹:
     {TerminalColor.BOLD}subcomp.exe C:\\raw_subs -srt -json -o C:\\clean_subs{TerminalColor.ENDC}

  5. 单文件转换并重命名输出文件:
     {TerminalColor.BOLD}subcomp.exe video1_draft.txt -srt -name "final_subtitle"{TerminalColor.ENDC}

  6. 🚀 时间轴平移 (全体字幕延后 1.5 秒):
     {TerminalColor.BOLD}subcomp.exe messy.txt -srt -shift 1.5{TerminalColor.ENDC}

  7. 👑 终极组合技 (转写多格式 + 全体延后0.5秒 + 指定输出目录):
     {TerminalColor.BOLD}subcomp.exe whisper.json -srt -lrc -shift 0.5 -o final_subs{TerminalColor.ENDC}

"""

    parser = argparse.ArgumentParser(
        description=f"{TerminalColor.HEADER}多格式字幕/AST通用转换引擎 v2.0{TerminalColor.ENDC}\n将混乱的文本/字幕文件精准解析并转换为标准格式。",
        epilog=epilog_text,
        formatter_class=argparse.RawTextHelpFormatter # 允许我们在帮助文档里使用真实的换行符
    )
    parser.add_argument("input", help="输入文件或文件夹的路径")
    parser.add_argument("-srt", action="store_true", help="输出标准的 .srt 字幕")
    parser.add_argument("-lrc", action="store_true", help="输出标准的 .lrc 歌词")
    parser.add_argument("-json", action="store_true", help="输出结构化的 .json 数据")
    parser.add_argument("-sbv", action="store_true", help="输出 YouTube .sbv 格式")
    parser.add_argument("-txt", action="store_true", help="输出纯文本阅读格式")
    parser.add_argument("-all", action="store_true", help="一键输出上述所有格式")
    parser.add_argument("-o", "--output_dir", help="指定输出目录 (默认: 当前目录)")
    parser.add_argument("-name", "--filename", help="强制指定输出的主文件名")
    # 👇 新增这一行：接收平移参数，默认为 0.0（不平移）
    parser.add_argument("-shift", type=float, default=0.0, help="时间轴整体平移(秒)，正数延后，负数提前")
    
    args = parser.parse_args()

    # 如果没有带任何参数直接运行，大概率是新手，打印横幅并显示帮助
    if len(sys.argv) == 1:
        print_banner()
        parser.print_help()
        sys.exit(1)
        
    print_banner()

    # 1. 确定输出格式
    target_opts = {"srt": args.srt, "lrc": args.lrc, "json": args.json, "sbv": args.sbv, "txt": args.txt}
    formats = [k for k, v in target_opts.items() if v]
    if args.all or not formats:
        formats = list(target_opts.keys())
        print(f"{TerminalColor.WARNING}[*] 未指定格式或使用 -all，将默认输出所有格式: {', '.join(formats)}{TerminalColor.ENDC}")

    # 2. 收集文件列表
    input_path = args.input
    file_list = []
    if os.path.isdir(input_path):
        for file in os.listdir(input_path):
            file_path = os.path.join(input_path, file)
            if os.path.isfile(file_path) and not file.startswith('.'):
                file_list.append(file_path)
        print(f"{TerminalColor.OKBLUE}[*] 检测到目录输入，共找到 {len(file_list)} 个文件待处理。{TerminalColor.ENDC}\n")
    else:
        if os.path.isfile(input_path):
            file_list.append(input_path)
        else:
            print(f"{TerminalColor.FAIL}[X] 致命错误：找不到输入路径 '{input_path}'{TerminalColor.ENDC}")
            sys.exit(1)

    out_dir = args.output_dir if args.output_dir else os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    success_count = 0
    fail_count = 0

    # 3. 核心执行循环与进度反馈
    for i, file_path in enumerate(file_list, 1):
        base_name = args.filename if args.filename else os.path.splitext(os.path.basename(file_path))[0]
        print(f"{TerminalColor.BOLD}[{i}/{len(file_list)}] 正在解析: {os.path.basename(file_path)}...{TerminalColor.ENDC}", end=" ")
        
        doc = ParserFactory.parse(file_path)
        if not doc:
            print(f"\n   {TerminalColor.FAIL}└── [跳过] 解析结果为空或格式严重损坏。{TerminalColor.ENDC}")
            fail_count += 1
            continue

        print(f"{TerminalColor.OKGREEN}成功提取 {len(doc.cues)} 条字幕。{TerminalColor.ENDC}")
        # 👇 新增这一段：应用时间轴平移魔法
        if args.shift != 0.0:
            for cue in doc.cues:
                cue.start = cue.start.add_seconds(args.shift)
                cue.end = cue.end.add_seconds(args.shift)
            
            direction = "延后" if args.shift > 0 else "提前"
            print(f"   {TerminalColor.WARNING}├── [平移] 整体{direction}: {abs(args.shift)} 秒{TerminalColor.ENDC}")
        # 👆 新增结束
        
        for f in formats:
            out_file = os.path.join(out_dir, f"{base_name}.{f}")
            count = 1
            # 自动重命名，防止覆盖已有的珍贵数据
            while os.path.exists(out_file):
                out_file = os.path.join(out_dir, f"{base_name}_{count}.{f}")
                count += 1
            
            try:
                WriterFactory.write(doc, f, out_file)
                print(f"   {TerminalColor.OKCYAN}├── 写入: {os.path.basename(out_file)}{TerminalColor.ENDC}")
            except Exception as e:
                print(f"   {TerminalColor.FAIL}├── [错误] 生成 {f} 失败: {e}{TerminalColor.ENDC}")
        
        success_count += 1
        print("   " + "─" * 40)

    # 4. 任务总结报告
    print(f"\n{TerminalColor.HEADER}=== 任务总结 ==={TerminalColor.ENDC}")
    print(f"总计处理: {len(file_list)} 个文件")
    print(f"成功转换: {TerminalColor.OKGREEN}{success_count}{TerminalColor.ENDC} 个")
    if fail_count > 0:
        print(f"失败跳过: {TerminalColor.FAIL}{fail_count}{TerminalColor.ENDC} 个")
    print(f"输出目录: {os.path.abspath(out_dir)}")
    print(f"{TerminalColor.HEADER}================{TerminalColor.ENDC}\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{TerminalColor.WARNING}[!] 任务被用户手动中断。{TerminalColor.ENDC}")
        sys.exit(0)