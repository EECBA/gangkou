# -*- coding: utf-8 -*-
"""
tone_grid.py —— 语调网格（她说话的温度层，2026.9.3 实装）
数据在 data/tone_grid.json，由用户亲自填写（人格文案不代笔，铁规矩）；
没填的档位回退 tier3，没填的簇回退 neutral，文件缺失/全空则整层不生效。

网格结构（原版 jiwen tone-grid 的设计）：
- 9 情绪簇：由 valence(心情) × arousal(心绪) 二维划分
  excited 开心兴奋 / content 舒服慵懒 / agitated 烦躁带刺 / depressed 低落话少 /
  sullen 闷闷的 / restless 坐不住 / pleased 心情不错 / calm 平静 / neutral 中性
- 每簇 5 档 pride（1=完全不端着 … 5=全副武装）；她 pride 常年在 0~0.35，
  实际只会用到 1/2/3 档，4/5 留空即可
- urgency 叠加层（想念浓度）：aware(惦记) / urgent(想开口) / desperate(忍不住了)，
  各分 proactive（她主动开口时）/ reactive（回复时）两套
"""

import json
import re
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
TONE_GRID_PATH = DATA_DIR / "tone_grid.json"
TONE_MD_PATH = DATA_DIR / "tone_grid填写.md"

_cache = {"mtime": 0, "data": None}

# 填写页（md）标题 → 网格键：簇名与档位都用中文写，解析只需切分「·」
_CLUSTERS = {"中性": "neutral", "兴奋": "excited", "舒服": "content", "低落": "depressed",
             "烦躁": "agitated", "开心": "pleased", "发闷": "sullen", "躁动": "restless",
             "松弛": "calm"}
_TIERS = {"放软": "1", "平常": "2", "端着": "3"}
_URG = {"惦记": "aware", "想开口了": "urgent", "忍不住了": "desperate"}
_URG_MODE = {"她主动开口时": "proactive", "她回消息时": "reactive"}


def _parse_md(text):
    """解析填写页：每个 ### 标题是一个格子，正文 = 标题之后到下一个标题之前的非空行。
    标题格式：簇·档（如 中性·平常）或 想念·档位·模式（如 想念·忍不住了·她主动开口时）。
    括号里的说明文字不算正文（只出现在空格子里当提示）。"""
    profiles, urgency = {}, {}
    cur_key = None   # (kind, a, b)
    buf = []
    for line in text.split("\n"):
        if line.startswith("### "):
            if cur_key:
                _put(profiles, urgency, cur_key, buf)
            parts = [p.strip() for p in line[4:].strip().split("·")]
            cur_key, buf = None, []
            if len(parts) == 2 and parts[0] in _CLUSTERS and parts[1] in _TIERS:
                cur_key = ("p", _CLUSTERS[parts[0]], _TIERS[parts[1]])
            elif len(parts) == 3 and parts[0] == "想念" and parts[1] in _URG and parts[2] in _URG_MODE:
                cur_key = ("u", _URG[parts[1]], _URG_MODE[parts[2]])
        elif cur_key is not None and not line.startswith("#"):
            s = line.strip()
            # 提示行（全角括号包裹的示例说明）不算填写内容
            if s.startswith("（") and s.endswith("）"):
                continue
            if s:
                buf.append(s)
    if cur_key:
        _put(profiles, urgency, cur_key, buf)
    return {"profiles": profiles, "urgency": urgency}


def _put(profiles, urgency, key, buf):
    kind, a, b = key
    if not buf:
        return
    text = " ".join(buf)
    # 引号协议：写在 “” 中间才算填写；没引号但有正文也收（防呆）
    m = re.search(r"“(.*)”", text, re.S)
    content = m.group(1).strip() if m else text.strip()
    if not content:
        return
    if kind == "p":
        profiles.setdefault(a, {})[b] = content
    else:
        urgency.setdefault(a, {})[b] = content


def _load():
    """热加载：填写页 md 优先（用户主入口），没有填写内容再看 json；文件改动即生效。"""
    try:
        src, m = None, 0
        if TONE_MD_PATH.exists():
            src = TONE_MD_PATH.read_text("utf-8")
            m = TONE_MD_PATH.stat().st_mtime
        if _cache["data"] is not None and m == _cache["mtime"]:
            return _cache["data"]
        data = _parse_md(src) if src is not None else None
        if data is None or (not data.get("profiles") and not data.get("urgency")):
            data = json.loads(TONE_GRID_PATH.read_text("utf-8")) if TONE_GRID_PATH.exists() else None
        _cache["data"], _cache["mtime"] = data, m
        return data
    except Exception:
        return None


def classify(state):
    """五轴 → (情绪簇, pride 档位)。"""
    v, p = state.get("valence", 0), state.get("pride", 0)
    a = state.get("arousal", 0)
    if v > 0.3 and a > 0.3:
        cluster = "excited"
    elif v > 0.3 and a < -0.3:
        cluster = "content"
    elif v < -0.3 and a > 0.3:
        cluster = "agitated"
    elif v < -0.3 and a < -0.3:
        cluster = "depressed"
    elif v > 0.3:
        cluster = "pleased"
    elif v < -0.3:
        cluster = "sullen" if a > 0 else "depressed"
    elif a > 0.3:
        cluster = "restless"
    elif a < -0.3:
        cluster = "calm"
    else:
        cluster = "neutral"
    tier = 5 if p > 0.8 else 4 if p > 0.5 else 3 if p > 0.3 else 2 if p > 0.1 else 1
    return cluster, tier


def _urgency_level(c):
    return "desperate" if c >= 0.45 else "urgent" if c >= 0.35 else "aware" if c >= 0.20 else None


def get_style_guidance(state, mode="reactive"):
    """查表：五轴 → 语调指引文本（注入 system prompt 用）。
    mode: reactive=回复他 / proactive=她主动开口。空格子回退，全空返回 ""。"""
    data = _load()
    if not data:
        return ""
    cluster, tier = classify(state)
    profiles = data.get("profiles") or {}
    # 回退链（兼容 md 版 1~3 档与 json 版 1~5 档）：
    # 本簇该档 → 本簇"平常"(2) → 本簇(3) → neutral 同序 → 放弃（整层不出）
    prof = profiles.get(cluster) or {}
    nprof = profiles.get("neutral") or {}
    text = ""
    for source in (prof, nprof):
        for key in (str(tier), "2", "3"):
            if source.get(key):
                text = source[key]
                break
        if text:
            break
    if not text:
        return ""
    lines = [text]
    # 想念叠加层
    urg = _urgency_level(state.get("connection", 0))
    if urg:
        u = (data.get("urgency") or {}).get(urg) or {}
        extra = u.get(mode) or ""
        if extra:
            lines.append(extra)
    return "\n".join(l for l in lines if l)
