import os
import sys
import re
import json
import argparse
import httpx
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
# 1. 领域模型 (AST / 中间表示)
# ==========================================

class Timecode:
    """时间戳对象，内部统一使用总毫秒数存储"""
    def __init__(self, total_ms: int):
        self.total_ms = max(0, total_ms)

    @classmethod
    def from_string(cls, ts: str) -> 'Timecode':
        """将各种格式字符串（包括纯秒数）解析为时间戳对象"""
        ts_clean = (str(ts).lower()
                    .replace("h", ":").replace("m", ":")
                    .replace("时", ":").replace("分", ":")
                    .replace(",", ".")
                    .replace("s", "").replace("秒", "")
                    .strip())
        if not ts_clean or ts_clean == 'none': return cls(0)
        
        # 兼容 Whisper API 返回的浮点数秒字符串
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
                    s, ms = int(s_val), int(ms_str.ljust(3, "0")[:3])
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
# 2. 核心模块 (语音处理、解析、写入)
# ==========================================

class SpeechProcessor:
    """对接在线 Whisper API 并生成 AST 对象"""
    # 建议将 API Key 放入环境变量 GROQ_API_KEY 中
    DEFAULT_API_KEY = "GROQ_API_KEY"
    
    # 常见的幻觉关键词黑名单
    HALLUCINATION_BLACKLIST = [
        "优优独播剧场", "YoYo Television", "Exclusive", "字幕由", 
        "感谢观看", "明镜与点点", "点赞", "订阅", "请订阅", "MyDramaVillage"
    ]

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.groq.com/openai/v1"):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", self.DEFAULT_API_KEY)
        self.base_url = base_url

    def transcribe(self, audio_path: str, model: str = "whisper-large-v3") -> Optional[SubtitleDocument]:
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        strict_prompt = "这是一段纯净的对话录音。请只转录你听到的内容，严禁添加任何广告语、翻译组信息或剧场名称。"
        
        try:
            with open(audio_path, "rb") as f:
                files = {
                    "file": (os.path.basename(audio_path), f),
                    "model": (None, model),
                    "response_format": (None, "verbose_json"),
                    "prompt": (None, strict_prompt),
                }
                print(f"   {TerminalColor.OKBLUE}├── [API] 正在上传并请求转写...{TerminalColor.ENDC}")
                response = httpx.post(url, headers=headers, files=files, timeout=600)
                response.raise_for_status()
                return self._to_ast(response.json())
        except Exception as e:
            print(f"\n   {TerminalColor.FAIL}└── [API 错误] {e}{TerminalColor.ENDC}"); return None

        def _to_ast(self, api_data: dict) -> SubtitleDocument:
            doc = SubtitleDocument()
        
        for seg in api_data.get("segments", []):
            text = seg.get("text", "").strip()
            
            # 过滤黑名单
            if not text or any(bad in text for bad in self.HALLUCINATION_BLACKLIST):
                continue

            start_sec = float(seg.get("start", 0))
            end_sec = float(seg.get("end", 0))
            duration = end_sec - start_sec

            # 【核心逻辑】：如果这个片段超过了 8 秒，触发强行切分
            if duration > 8.0:
                # 改进的切分正则：避免切分数字中的点（如 v2.5），要求标点后跟空格或位于行尾
                # 中文标点通常不需要后跟空格
                sentences = re.split(r'([。！？]|[?.!](?:\s|$))', text)
                
                chunks = []
                temp_str = ""
                # 把标点符号重新拼回句子末尾
                for part in sentences:
                    if not part: continue
                    temp_str += part
                    if re.match(r'([。！？]|[?.!](?:\s|$))', part):
                        chunks.append(temp_str.strip())
                        temp_str = ""
                if temp_str:
                    chunks.append(temp_str.strip())


                # 2. 移除可能产生的空块
                chunks = [c for c in chunks if c]

                # 3. 按字数比例分配时间
                if chunks:
                    total_chars = sum(len(c) for c in chunks)
                    current_start = start_sec

                    for chunk in chunks:
                        # 计算这句话应占的时长：(这句话字数 / 总字数) * 总时长
                        chunk_duration = (len(chunk) / total_chars) * duration
                        chunk_end = current_start + chunk_duration

                        doc.add_cue(Timecode(int(current_start * 1000)), Timecode(int(chunk_end * 1000)), chunk)
                        # 下一句话的开始时间等于这句话的结束时间
                        current_start = chunk_end
                else:
                    # 如果没有标点符号切分失败，就原样输出
                    doc.add_cue(Timecode(int(start_sec * 1000)), Timecode(int(end_sec * 1000)), text)
            
            # 如果片段很短（小于8秒），正常输出
            else:
                doc.add_cue(Timecode(int(start_sec * 1000)), Timecode(int(end_sec * 1000)), text)
                
        return doc

TIMESTAMP_PATTERN = r'(\d{1,2}[:h时]\d{1,2}(?:[:m分]\d{1,2})?(?:[\.,]\d{1,3})?[s秒]?)'
RANGE_PATTERN = re.compile(r'[\[\(\{]?\s*' + TIMESTAMP_PATTERN + r'\s*[\]\)\}]?[\s\-\–\—\~>,\]\[]+[\[\(\{]?\s*' + TIMESTAMP_PATTERN + r'\s*[\]\)\}]?')

class ParserFactory:
    @staticmethod
    def parse(file_path: str) -> Optional[SubtitleDocument]:
        content = ParserFactory._safe_read(file_path)
        if not content: return None
        
        _, ext = os.path.splitext(file_path)
        doc = SubtitleDocument()
        try:
            if ext.lower() == '.json': ParserFactory._parse_json(content, doc)
            elif ext.lower() == '.sbv': ParserFactory._parse_sbv(content, doc)
            else: ParserFactory._parse_text(content, doc)
            return doc if doc.cues else None
        except Exception as e:
            print(f"   {TerminalColor.FAIL}└── [解析错误] {e}{TerminalColor.ENDC}")
            return None

    @staticmethod
    def _safe_read(p):
        for e in ['utf-8-sig', 'utf-8', 'gbk', 'utf-16']:
            try:
                with open(p, "r", encoding=e) as f: return f.read()
            except: continue
        return ""

    @staticmethod
    def _parse_json(c, doc):
        data = json.loads(c)
        if isinstance(data, dict) and "segments" in data: # 兼容 Whisper 完整输出
            data = data["segments"]
        for i in data:
            s = str(i.get("start", i.get("timestamp", [0,0])[0]))
            e = str(i.get("end", i.get("timestamp", [0,0])[1]))
            doc.add_cue(Timecode.from_string(s), Timecode.from_string(e), i.get("text", ""))

    @staticmethod
    def _parse_sbv(content, doc):
        """解析 YouTube SBV 格式"""
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            # SBV 特征: 0:00:00.000,0:00:05.000
            m = re.match(r'(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+)', line)
            if m:
                start, end = m.group(1), m.group(2)
                text_lines = []
                i += 1
                while i < len(lines) and lines[i].strip():
                    text_lines.append(lines[i].strip())
                    i += 1
                doc.add_cue(Timecode.from_string(start), Timecode.from_string(end), "\n".join(text_lines))
            else:
                i += 1


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
            elif fmt == "lrc":
                for c in doc.cues:
                    clean_text = c.text.replace('\n', ' ')
                    f.write(f"[{c.start.to_lrc_format()}]{clean_text}\n")
            elif fmt == "json":
                out = [{"timestamp": [c.start.to_standard(), c.end.to_standard()], "text": c.text} for c in doc.cues]
                json.dump(out, f, ensure_ascii=False, indent=2)

# ==========================================
# 3. 主程序 (CLI)
# ==========================================

def main():
    epilog_text = f"""
{TerminalColor.HEADER}{TerminalColor.BOLD}====================== 【 程 序 说 明 】 ======================{TerminalColor.ENDC}

{TerminalColor.OKCYAN}【核心功能 (Features)】{TerminalColor.ENDC}
  1. 强力清洗：自动修复错乱的时间轴（缺失毫秒、纯秒数等）。
  2. 剧本切割：支持直接从连续文本对话中自动切分字幕。
  3. 绝对规范：严格遵守各格式标准（SRT, LRC, JSON）。
  4. 批量引擎：支持一键处理整个文件夹中的所有文件。
  5. 语音转写：直接输入音频文件，通过 Whisper API 生成字幕。

{TerminalColor.OKGREEN}【使用示例 (Examples)】{TerminalColor.ENDC}
  1. 基础转换: {TerminalColor.BOLD}subcomp.exe messy.txt -srt{TerminalColor.ENDC}
  2. 语音转字幕: {TerminalColor.BOLD}subcomp.exe record.mp3 -srt{TerminalColor.ENDC}
  3. 时间轴平移: {TerminalColor.BOLD}subcomp.exe messy.txt -srt -shift 1.5{TerminalColor.ENDC}
  4. 智能输出目录: {TerminalColor.BOLD}subcomp.exe D:\\Video -srt -o clean_subs{TerminalColor.ENDC}
"""

    parser = argparse.ArgumentParser(
        description=f"{TerminalColor.HEADER}SUBCOMPILER v2.5{TerminalColor.ENDC}\n多格式字幕转换/转写引擎",
        epilog=epilog_text, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("input", help="输入路径 (文件或文件夹)")
    parser.add_argument("-srt", action="store_true", help="输出 SRT")
    parser.add_argument("-lrc", action="store_true", help="输出 LRC")
    parser.add_argument("-json", action="store_true", help="输出 JSON")
    parser.add_argument("-all", action="store_true", help="输出所有格式")
    parser.add_argument("-o", "--output_dir", help="指定输出子目录")
    parser.add_argument("-name", "--filename", help="强制指定主文件名")
    parser.add_argument("-shift", type=float, default=0.0, help="时间轴整体平移(秒)")
    
    args = parser.parse_args()
    if len(sys.argv) == 1: print_banner(); parser.print_help(); sys.exit(1)
    print_banner()

    # 路径解析
    abs_in = os.path.abspath(args.input)
    base = abs_in if os.path.isdir(abs_in) else os.path.dirname(abs_in)
    out_dir = os.path.join(base, args.output_dir or "")
    os.makedirs(out_dir, exist_ok=True)

    # 文件收集
    files = [os.path.join(abs_in, f) for f in os.listdir(abs_in) if os.path.isfile(os.path.join(abs_in, f))] if os.path.isdir(abs_in) else [abs_in]
    formats = [k for k in ["srt", "lrc", "json"] if getattr(args, k)] or (["srt", "lrc", "json"] if args.all else ["srt"])

    for i, f_path in enumerate(files, 1):
        name = args.filename or os.path.splitext(os.path.basename(f_path))[0]
        ext = os.path.splitext(f_path)[1].lower()
        print(f"{TerminalColor.BOLD}[{i}/{len(files)}] 正在处理: {os.path.basename(f_path)}{TerminalColor.ENDC}")
        
        # 逻辑：音频则转录，否则解析
        if ext in ['.mp3', '.wav', '.m4a', '.flac']:
            doc = SpeechProcessor().transcribe(f_path)
        else:
            doc = ParserFactory.parse(f_path)

        if not doc:
            print(f"   {TerminalColor.FAIL}└── [跳过] 无法提取有效内容。{TerminalColor.ENDC}"); continue
        
        # 处理时间偏移
        if args.shift != 0:
            for c in doc.cues:
                c.start, c.end = c.start.add_seconds(args.shift), c.end.add_seconds(args.shift)
            print(f"   {TerminalColor.WARNING}├── [平移] 全体字幕已平移 {args.shift} 秒{TerminalColor.ENDC}")

        # 写入文件
        for fmt in formats:
            out_f = os.path.join(out_dir, f"{name}.{fmt}")
            WriterFactory.write(doc, fmt, out_f)
            print(f"   {TerminalColor.OKCYAN}├── 导出成功: {os.path.basename(out_f)}{TerminalColor.ENDC}")
        print("   " + "─" * 45)

    print(f"\n{TerminalColor.HEADER}=== 任务全部结束 ==={TerminalColor.ENDC}\n")

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("\n[!] 用户中断。")