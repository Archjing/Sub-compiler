#!/usr/bin/env python3
import os
import sys
import re
import json
import argparse
import httpx  # [新增依赖] 用于高效处理 API 请求
from dataclasses import dataclass
from typing import List, Optional

# ==========================================
# 0. 终端色彩与 Banner
# ==========================================

class TerminalColor:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def print_banner():
    """打印实心、高亮、充满力量感的 SUBCOMPILER 横幅"""
    banner = f"""
{TerminalColor.OKCYAN}{TerminalColor.BOLD}
 ██████  ██    ██ ██████   ██████  ██████  ███    ███ ██████  ██ ██      ███████ ██████  
██       ██    ██ ██   ██ ██      ██    ██ ████  ████ ██   ██ ██ ██      ██      ██   ██ 
 █████   ██    ██ ██████  ██      ██    ██ ██ ████ ██ ██████  ██ ██      █████   ██████  
     ██  ██    ██ ██   ██ ██      ██    ██ ██  ██  ██ ██      ██ ██      ██      ██   ██ 
 ██████   ██████  ██████   ██████  ██████  ██      ██ ██      ██ ███████ ███████ ██   ██ 
{TerminalColor.ENDC}
{TerminalColor.HEADER}>>> 多格式字幕/AST通用转换引擎 v2.5 (Speech Enabled) <<<{TerminalColor.ENDC}
    """
    print(banner)

# ==========================================
# 1. 领域模型 (AST / 中间表示) - 保持不变
# ==========================================

class Timecode:
    def __init__(self, total_ms: int):
        self.total_ms = max(0, total_ms)

    @classmethod
    def from_string(cls, ts: str) -> 'Timecode':
        ts_clean = (str(ts).lower()
                    .replace("h", ":").replace("m", ":")
                    .replace("时", ":").replace("分", ":")
                    .replace(",", ".")
                    .replace("s", "").replace("秒", "")
                    .strip())
        if not ts_clean or ts_clean == 'none': return cls(0)
        
        # 兼容浮点数秒 (Whisper API 常用格式)
        try:
            return cls(int(float(ts_clean) * 1000))
        except ValueError:
            parts = ts_clean.split(":")
            try:
                if len(parts) == 1: h, m, s_str = 0, 0, parts[0]
                elif len(parts) == 2: h, m, s_str = 0, int(parts[0]), parts[1]
                elif len(parts) == 3: h, m, s_str = int(parts[0]), int(parts[1]), parts[2]
                else: return cls(0)
                
                if '.' in s_str:
                    s_val, ms_str = s_str.split('.')
                    s = int(s_val)
                    ms = int(ms_str.ljust(3, "0")[:3])
                else: s, ms = int(s_str), 0
                return cls((h * 3600 + m * 60 + s) * 1000 + ms)
            except: return cls(0)

    def add_seconds(self, seconds: float) -> 'Timecode':
        return Timecode(self.total_ms + int(seconds * 1000))

    def to_standard(self, separator='.') -> str:
        total_s = self.total_ms // 1000
        ms = self.total_ms % 1000
        h, m, s = total_s // 3600, (total_s % 3600) // 60, total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}{separator}{ms:03d}"

    def to_lrc_format(self) -> str:
        total_s, ms = self.total_ms // 1000, self.total_ms % 1000
        return f"{total_s // 60:02d}:{total_s % 60:02d}.{ms // 10:02d}"

@dataclass
class SubtitleCue:
    start: Timecode; end: Timecode; text: str

class SubtitleDocument:
    def __init__(self): self.cues: List[SubtitleCue] = []
    def add_cue(self, start: Timecode, end: Timecode, text: str):
        if text.strip(): self.cues.append(SubtitleCue(start, end, text.strip()))

# ==========================================
# 2. [新增模块] SpeechProcessor (在线 Whisper 转写)
# ==========================================

class SpeechProcessor:
    """对接在线 Whisper API 并生成 AST 对象"""
    # [修改] API Key 设为默认值 usercustom
    def __init__(self, api_key: str = "GROQ_API_KEY", base_url: str = "https://api.groq.com/openai/v1"):
        self.api_key = api_key
        self.base_url = base_url

    def transcribe(self, audio_path: str, model: str = "whisper-large-v3") -> Optional[SubtitleDocument]:
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # [修改] 定义一个强效的防幻觉 Prompt
        # 告诉模型这是严谨的对话，不要联想 YouTube 标签
        strict_prompt = "这是一段纯净的对话录音，这是一段正式的会议记录或学术讲座录音，内容严谨，请只转录你听到的内容，严禁添加任何频道名称、广告语、翻译组信息或剧场名称。"
        
        try:
            with open(audio_path, "rb") as f:
                files = {
                    "file": (os.path.basename(audio_path), f),
                    "model": (None, model),
                    "response_format": (None, "verbose_json"),
                    "prompt": (None, strict_prompt), # [新增] 将 Prompt 发送给 API
                }
                print(f"   {TerminalColor.OKBLUE}├── [API] 正在上传音频至 Whisper 接口并应用防幻觉 Prompt...{TerminalColor.ENDC}")
                response = httpx.post(url, headers=headers, files=files, timeout=600)
                response.raise_for_status()
                return self._to_ast(response.json())
        except Exception as e:
            print(f"\n   {TerminalColor.FAIL}└── [API 错误] {e}{TerminalColor.ENDC}")
            return None

    def _to_ast(self, api_data: dict) -> SubtitleDocument:
        doc = SubtitleDocument()
        segments = api_data.get("segments", [])
        for seg in segments:
            start = Timecode.from_string(str(seg.get("start", 0)))
            end = Timecode.from_string(str(seg.get("end", 0)))
            doc.add_cue(start, end, seg.get("text", ""))
        return doc

# ==========================================
# 3. 解析器与生成器 - 保持不变
# ==========================================

TIMESTAMP_PATTERN = r'(\d{1,2}[:h时]\d{1,2}(?:[:m分]\d{1,2})?(?:[\.,]\d{1,3})?[s秒]?)'
RANGE_PATTERN = re.compile(r'[\[\(\{]?\s*' + TIMESTAMP_PATTERN + r'\s*[\]\)\}]?[\s\-\–\—\~>\]\[]+[\[\(\{]?\s*' + TIMESTAMP_PATTERN + r'\s*[\]\)\}]?')

class ParserFactory:
    @staticmethod
    def parse(file_path: str) -> Optional[SubtitleDocument]:
        content = ParserFactory._safe_read(file_path)
        _, ext = os.path.splitext(file_path)
        doc = SubtitleDocument()
        try:
            if ext.lower() == '.json': ParserFactory._parse_json(content, doc)
            elif ext.lower() == '.sbv': ParserFactory._parse_sbv(content, doc)
            else: ParserFactory._parse_text(content, doc)
            return doc if doc.cues else None
        except: return None

    @staticmethod
    def _safe_read(p):
        for e in ['utf-8-sig', 'utf-8', 'gbk']:
            try:
                with open(p, "r", encoding=e) as f: return f.read()
            except: continue
        return ""

    @staticmethod
    def _parse_json(c, doc):
        data = json.loads(c)
        for i in data:
            if "timestamp" in i and isinstance(i["timestamp"], list):
                s, e = str(i["timestamp"][0]), str(i["timestamp"][1])
            else:
                s, e = str(i.get("start_time", i.get("start", 0))), str(i.get("end_time", i.get("end", 0)))
            doc.add_cue(Timecode.from_string(s), Timecode.from_string(e), i.get("text", i.get("content", "")))

    @staticmethod
    def _parse_sbv(c, doc):
        for b in c.split('\n\n'):
            ls = [l.strip() for l in b.split('\n') if l.strip()]
            if len(ls) >= 2:
                ts = ls[0].split(',')
                if len(ts) == 2: doc.add_cue(Timecode.from_string(ts[0]), Timecode.from_string(ts[1]), "\n".join(ls[1:]))

    @staticmethod
    def _parse_text(content, doc):
        if RANGE_PATTERN.search(content):
            cur_s, cur_e, cur_t = None, None, []
            for line in content.splitlines():
                line = line.strip()
                if not line or line.isdigit(): continue
                m = RANGE_PATTERN.search(line)
                if m:
                    if cur_s and cur_e and cur_t: doc.add_cue(cur_s, cur_e, "\n".join(cur_t))
                    cur_s, cur_e, cur_t = Timecode.from_string(m.group(1)), Timecode.from_string(m.group(2)), [line[m.end():].strip()]
                elif cur_s: cur_t.append(line)
            if cur_s and cur_e and cur_t: doc.add_cue(cur_s, cur_e, "\n".join(cur_t))
        else:
            inline_re = re.compile(r'[\[\(\{]?\s*' + TIMESTAMP_PATTERN + r'\s*[\]\)\}]?')
            ms = list(inline_re.finditer(content))
            temp = []
            for i, m in enumerate(ms):
                t_e = ms[i+1].start() if i+1 < len(ms) else len(content)
                txt = content[m.end():t_e].strip()
                if txt: temp.append({"s": Timecode.from_string(m.group(1)), "t": txt})
            for i, c in enumerate(temp):
                end = temp[i+1]["s"] if i+1 < len(temp) else c["s"].add_seconds(5.0)
                doc.add_cue(c["s"], end, c["t"])

class WriterFactory:
    @staticmethod
    def write(doc, fmt, out_p):
        with open(out_p, "w", encoding="utf-8") as f:
            if fmt == "srt":
                for i, c in enumerate(doc.cues, 1): f.write(f"{i}\n{c.start.to_standard(',') } --> {c.end.to_standard(',')}\n{c.text}\n\n")
            elif fmt == "sbv":
                for c in doc.cues: f.write(f"{c.start.to_standard()},{c.end.to_standard()}\n{c.text}\n\n")
            elif fmt == "json":
                out = [{"timestamp": [c.start.to_standard(), c.end.to_standard()], "text": c.text} for c in doc.cues]
                json.dump(out, f, ensure_ascii=False, indent=2)
            elif fmt == "lrc":
                f.write("[ti:Converted by SubCompiler]\n")
                for c in doc.cues: f.write(f"[{c.start.to_lrc_format()}]{c.text.replace('\\n', '  ')}\n")
            elif fmt == "txt":
                for c in doc.cues: f.write(f"[{c.start.to_standard()} --> {c.end.to_standard()}] {c.text}\n\n")

# ==========================================
# 4. 主程序 (CLI)
# ==========================================

def main():
    # [修改] 更新帮助文档示例，移除 -api 参数演示
    epilog_text = f"""
{TerminalColor.HEADER}{TerminalColor.BOLD}====================== 【 程 序 说 明 】 ======================{TerminalColor.ENDC}

{TerminalColor.OKCYAN}【核心功能 (Features)】{TerminalColor.ENDC}
  1. 强力清洗：自动修复错乱的时间轴（缺失毫秒、纯秒数、奇葩分隔符等）。
  2. 剧本切割：支持直接从连续文本对话中自动切分字幕。
  3. 绝对规范：严格遵守各格式标准（SRT, LRC, JSON, SBV）。
  4. 智能寻路：默认与输入同级输出，支持自动创建子文件夹。
  5. 时间魔法：支持整体时间轴精准平移（-shift 参数）。
  6. 🚀 语音转写：直接输入音频文件，通过 Whisper API 生成字幕。

{TerminalColor.OKGREEN}【使用示例 (Examples)】{TerminalColor.ENDC}
  1. 基础转换: {TerminalColor.BOLD}subcomp.exe messy.txt -srt{TerminalColor.ENDC}
  2. 🚀 语音转字幕: {TerminalColor.BOLD}subcomp.exe record.mp3 -srt{TerminalColor.ENDC}
  3. 时间轴平移: {TerminalColor.BOLD}subcomp.exe messy.txt -srt -shift 1.5{TerminalColor.ENDC}
  4. 终极组合: {TerminalColor.BOLD}subcomp.exe record.mp3 -all -shift 0.5 -o final{TerminalColor.ENDC}
"""

    parser = argparse.ArgumentParser(
        description=f"{TerminalColor.HEADER}SUBCOMPILER v2.5{TerminalColor.ENDC}\n多格式字幕/语音转写引擎",
        epilog=epilog_text, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("input", help="输入路径 (支持文件/文件夹/音频)")
    parser.add_argument("-srt", action="store_true")
    parser.add_argument("-lrc", action="store_true")
    parser.add_argument("-json", action="store_true")
    parser.add_argument("-sbv", action="store_true")
    parser.add_argument("-txt", action="store_true")
    parser.add_argument("-all", action="store_true")
    parser.add_argument("-o", "--output_dir")
    parser.add_argument("-name", "--filename")
    parser.add_argument("-shift", type=float, default=0.0)
    # [修改] 删除了 -api 参数
    
    args = parser.parse_args()
    if len(sys.argv) == 1: print_banner(); parser.print_help(); sys.exit(1)
    print_banner()

    # 智能寻路逻辑
    abs_in = os.path.abspath(args.input)
    base = abs_in if os.path.isdir(abs_in) else os.path.dirname(abs_in)
    out_dir = args.output_dir if args.output_dir and os.path.isabs(args.output_dir) else os.path.join(base, args.output_dir or "")
    os.makedirs(out_dir, exist_ok=True)

    # 格式与文件收集
    target_opts = {"srt": args.srt, "lrc": args.lrc, "json": args.json, "sbv": args.sbv, "txt": args.txt}
    formats = [k for k, v in target_opts.items() if v] or (list(target_opts.keys()) if args.all else ["srt"])
    files = [os.path.join(abs_in, f) for f in os.listdir(abs_in) if os.path.isfile(os.path.join(abs_in, f)) and not f.startswith('.')] if os.path.isdir(abs_in) else [abs_in]

    # 核心循环
    for i, f_path in enumerate(files, 1):
        name = args.filename or os.path.splitext(os.path.basename(f_path))[0]
        ext = os.path.splitext(f_path)[1].lower()
        
        print(f"{TerminalColor.BOLD}[{i}/{len(files)}] 正在处理: {os.path.basename(f_path)}...{TerminalColor.ENDC}", end="")
        
        # [修改] 判断逻辑：只要是音频格式，即自动启动转写模式
        if ext in ['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg','.opus', '.mp4', '.webm', '.mkv', '.avi']:
            print(f"\n   {TerminalColor.OKBLUE}├── [模式] 语音转写模式启动{TerminalColor.ENDC}")
            processor = SpeechProcessor() # 使用默认 API Key
            doc = processor.transcribe(f_path)
        else:
            print("") 
            doc = ParserFactory.parse(f_path)

        if not doc:
            print(f"   {TerminalColor.FAIL}└── [跳过] 无法获取有效字幕内容。{TerminalColor.ENDC}"); continue
        
        print(f"   {TerminalColor.OKGREEN}└── 成功获取 {len(doc.cues)} 条字幕。{TerminalColor.ENDC}")

        # 时间轴平移逻辑
        if args.shift != 0:
            for c in doc.cues:
                c.start, c.end = c.start.add_seconds(args.shift), c.end.add_seconds(args.shift)
            print(f"   {TerminalColor.WARNING}├── [平移] {'延后' if args.shift>0 else '提前'}: {abs(args.shift)}秒{TerminalColor.ENDC}")

        for fmt in formats:
            out_f = os.path.join(out_dir, f"{name}.{fmt}")
            cnt = 1
            while os.path.exists(out_f):
                out_f = os.path.join(out_dir, f"{name}_{cnt}.{fmt}"); cnt += 1
            WriterFactory.write(doc, fmt, out_f)
            print(f"   {TerminalColor.OKCYAN}├── 写入: {os.path.basename(out_f)}{TerminalColor.ENDC}")
        print("   " + "─" * 45)

    print(f"\n{TerminalColor.HEADER}=== 任务完成 ==={TerminalColor.ENDC}")
    print(f"输出目录: {out_dir}\n")

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print(f"\n{TerminalColor.WARNING}[!] 中断。{TerminalColor.ENDC}")