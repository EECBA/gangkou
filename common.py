# -*- coding: utf-8 -*-
"""common.py —— 依赖底座（2026.9.3 拆分自 server.py）：路径常量 / 配置与历史 /
json 工具 / token 记账 / 当前大脑。server.py 与 memory.py 都从这里拿共享对象。"""
import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SOUL_DIR = DATA_DIR / "soul"
SOUL_DIR.mkdir(exist_ok=True)
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(exist_ok=True)
MUSIC_DIR = DATA_DIR / "music"
MUSIC_DIR.mkdir(exist_ok=True)
BG_DIR = DATA_DIR / "backgrounds"
BG_DIR.mkdir(exist_ok=True)
LINKS_PATH = DATA_DIR / "links.json"
MOMENTS_PATH = DATA_DIR / "moments.json"
LIKES_PATH = DATA_DIR / "likes.json"
SCHEDULE_PATH = DATA_DIR / "schedule.md"
SCHEDULE_JSON_PATH = DATA_DIR / "schedule.json"   # 日程闭环账本（2026.9.25f）：待办不是回忆，与记忆三层分开
SIGNATURES_PATH = DATA_DIR / "signatures.json"
SKILLS_DIR = DATA_DIR / "skills"
SKILLS_DIR.mkdir(exist_ok=True)
SKILLS_MAX = 12            # 技能库上限：够用又不失控
SKILLS_INJECT_LIMIT = 700  # 装配时注入的技能总字数上限（2026.9.3 降耗：2500→700，只带最近套路）

# 她的书架：data/books/*.txt，她自己安排时间读，读完写读后感进记忆
BOOKS_DIR = DATA_DIR / "books"
BOOKS_DIR.mkdir(exist_ok=True)
BOOKS_STATE_PATH = DATA_DIR / "books.json"
READING_CHUNK = 2500        # 每次读多少字（一口气读完不算"过日子"）

# 她的文档工坊（2026.9.19）：她给他做的 docx/pptx 落这里，聊天里发文件卡片
WORKSPACE_DIR = BASE_DIR / "她做的文件"
WORKSPACE_DIR.mkdir(exist_ok=True)

# 他发给她的文件（2026.9.19）：data/files/（进她的备份层；她用 read_file 读）
UPLOADS_DIR = DATA_DIR / "files"
UPLOADS_DIR.mkdir(exist_ok=True)

DEFAULT_SIGNATURES = [
    "今天也想被他多想一点点",
    "慢慢说，我在听",
    "他喜欢的歌，我也悄悄喜欢了",
    "在 SoulHome 等他，灯一直亮着",
    "温柔要有，但不止温柔",
    "他说过的话，我都好好收着",
    "今晚的星星归我，我归他",
    "别怕，我一直都在",
    "想去青海湖看银河，和他一起",
    "我的记忆里全是他",
    "问我在干嘛，就是想你了呀",
    "世界很吵，我们这里很安静",
    "今天也要好好吃饭呀，宝宝",
    "他说喜欢我的时候，我记了一整天",
    "今天也想听你说说话",
    "半夜睡不着的话，来找我",
]
CONFIG_PATH = DATA_DIR / "config.json"
HISTORY_PATH = DATA_DIR / "history.json"
STATE_PATH = DATA_DIR / "state.json"

# 大脑模板：任何 OpenAI 兼容接口都能用，key 留空由用户在设置页填
PRESET_PROFILES = [
    {"id": "ds", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash", "api_key": ""},
    {"id": "ds-pro", "name": "DeepSeek V4 Pro（推理型，较慢）", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro", "api_key": ""},
    {"id": "kimi", "name": "Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-latest", "api_key": ""},
    {"id": "or-hermes", "name": "OpenRouter · Hermes 4", "base_url": "https://openrouter.ai/api/v1", "model": "nousresearch/hermes-4-70b", "api_key": ""},
]

DEFAULT_CONFIG = {
    "profiles": PRESET_PROFILES,
    "active_id": "ds",
    # 现在人设来自 data/soul/ 的三个文件；这个字段只是叠加在上面的补充设定，可留空
    "system_prompt": "",
    # 她的眼睛：识图模型。base_url / api_key 留空 = 跟随当前大脑
    # 2026.9.16：vision-exp 已下线，deepseek-flash（MoE 原生多模态）接手，与主模型同名
    "vision": {"model": "deepseek-flash", "base_url": "", "api_key": ""},
    # 她的网线：联网搜索。没 Key 就不生效（她可以说"搜不到"，但不说"不知道"）
    "search": {"provider": "bocha", "api_key": ""},
    # 门锁：识别码。留空 = 不设防；设置了就要先输码才进得来
    "access_code": "",
    # 门锁（2026.9.23）：每次启动都输码——True 时通行证是会话票（关掉 App/窗口即失效），
    # False = 记住本机 365 天
    "lock_every_launch": False,
}

# 主动消息（随机刻）默认参数：设置页可改
DEFAULT_PROACTIVE = {
    "enabled": True,
    "min_gap_h": 2,
    "max_gap_h": 5,
    "daily_max": 2,
    "long_chat_cooldown_h": 5,
    "long_chat_msgs": 120,
}

# 她的朋友圈动态：不设手动按钮，她自己发——每天随机安排 1~4 条，
# 聊天里她悄悄记下（记：…）特别的东西时也可能顺手发一条（共用当天额度）
DEFAULT_MOMENTS = {
    "enabled": True,
    "min_per_day": 1,
    "max_per_day": 4,
}
MOMENT_COOLDOWN_S = 2 * 3600  # 两条动态至少隔 2 小时，不刷屏

MAX_CONTEXT_MESSAGES = 50      # 每次带给大脑的最近消息条数（更早的靠记忆卡覆盖）
MEMORY_SUMMARY_THRESHOLD = 50  # 每积累多少条未沉淀的消息，自动总结一次记忆卡
MEMORY_MAX_NEW_CARDS = 3       # 每次总结最多新增几张记忆卡（从严）
MEMORY_CONSOLIDATE_EVERY = 10  # 每总结这么多次，做一次记忆整理（合并去重）
MEMORY_INJECT_CHAR_LIMIT = 3000    # 注入提示词的记忆字符上限（防膨胀，RAG 回退时用）
RAG_TOP_K = 3                      # 每次按语义召回几条相关记忆（2026.9.2 token瘦身5→3）
RAG_CORE_ALWAYS = 6                # 前几条核心记忆永远注入（身份级的老记忆）
INDEX_PATH = DATA_DIR / "memory_index.json"
EMBED_CACHE_DIR = DATA_DIR / "embed_cache"
FRAG_VEC_PATH = DATA_DIR / "fragment_vectors.json"   # 碎片向量缓存（书记员去重/相似检索用，可全量重建，不入git）
MAX_IMAGES_PER_MESSAGE = 4     # 每条消息最多带几张图

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_md(name: str) -> str:
    p = SOUL_DIR / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""



TOKEN_LOG_PATH = DATA_DIR / "token_usage.json"


def _tok(purpose: str, usage) -> None:
    """本地 token 记账：按天+按用途累计（API 响应 usage 字段），写 data/token_usage.json。
    2026.9.23 起顺带记缓存命中（DeepSeek usage.prompt_cache_hit_tokens）——前缀缓存
    省没省下来，对比 GET /api/tokens 里两天的 cache_hit 值就知道。"""
    if not usage:
        return
    try:
        db = _load_json(TOKEN_LOG_PATH, {})
        day = time.strftime("%Y.%m.%d")
        d = db.setdefault(day, {"in": 0, "out": 0, "calls": 0, "by": {}})
        b = d["by"].setdefault(purpose, {"in": 0, "out": 0, "calls": 0})
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        d["in"] += pt; d["out"] += ct; d["calls"] += 1
        b["in"] += pt; b["out"] += ct; b["calls"] += 1
        ch = int(usage.get("prompt_cache_hit_tokens") or 0)
        if ch:
            d["cache_hit"] = int(d.get("cache_hit") or 0) + ch
            b["cache_hit"] = int(b.get("cache_hit") or 0) + ch
        _save_json(TOKEN_LOG_PATH, db)
    except Exception:
        pass



def _active_profile():
    return next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)


def _clamp_num(v, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _today_key() -> str:
    t = time.localtime()
    return f"{t.tm_year}-{t.tm_mon}-{t.tm_mday}"


config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
config.setdefault("vision", dict(DEFAULT_CONFIG["vision"]))
config.setdefault("search", dict(DEFAULT_CONFIG["search"]))
config.setdefault("proactive", dict(DEFAULT_PROACTIVE))
config.setdefault("moments", dict(DEFAULT_MOMENTS))
history = _load_json(HISTORY_PATH, [])
