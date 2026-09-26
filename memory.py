# -*- coding: utf-8 -*-
"""memory.py —— 记忆系统全家（2026.9.3 拆分自 server.py）：
MEMORY.md/RAG 检索 + 书记员（碎片提取/叙事星座/账本）+ 聊天联想注入 + 遗忘TTL
+ 滚动摘要旁路实验。执行侧（记忆改造）与积温侧共用此模块，改记忆逻辑来这里。"""
import asyncio
import hashlib
import json
import math
import os
import re
import time

import httpx

from common import (DATA_DIR, SOUL_DIR, STATE_PATH, MAX_CONTEXT_MESSAGES,
                    MEMORY_SUMMARY_THRESHOLD, MEMORY_MAX_NEW_CARDS,
                    MEMORY_CONSOLIDATE_EVERY, MEMORY_INJECT_CHAR_LIMIT,
                    RAG_TOP_K, RAG_CORE_ALWAYS, INDEX_PATH, EMBED_CACHE_DIR,
                    FRAG_VEC_PATH,
                    _load_json, _save_json, _read_md, _tok, _active_profile,
                    config, history)

def save_memory_line(line: str) -> None:
    """她亲手记下的事：追加进 MEMORY.md（查重）并同步向量索引。记忆是她的手。"""
    line = (line or "").strip()
    if not line:
        return
    entries = memory_entries()
    if any(e == line or e.endswith("】" + line) for e in entries):  # 容忍日期前缀的查重
        return
    t = time.localtime()
    entry = f"【{t.tm_mon}.{t.tm_mday}】{line}"
    current = _read_md("MEMORY.md")
    (SOUL_DIR / "MEMORY.md").write_text((current + "\n§\n" + entry) if current else entry, encoding="utf-8")
    try:
        sync_memory_index()
    except Exception:
        pass



async def rewrite_recall_query(profile: dict, message: str) -> str:
    """联想改写（Hermes 式 query rewrite）：把他刚说的话变成一句用于翻记忆的问句——
    补全"他/那个/上次"这些指代、点明话题，RAG 才能靠语义捞到对的记忆。
    失败或超时就用原话，绝不影响聊天本身。"""
    if not message or len(message.strip()) < 4:
        return message
    prompt = (
        "把这句话改写成一个用于在记忆库里检索的短问句：把'他/她/那个/上次'这类指代换成具体对象，点明话题。"
        "只输出改写后的问句本身，不超过40个字，不要解释。如果这句话只是寒暄、没有可检索的内容，只输出：无\n\n"
        + message.strip()[:300]
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={
                    "model": profile["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    # 推理型模型会先花 token "思考"，上限给足才有产出
                    "max_tokens": 512,
                    "temperature": 0.2,
                },
            )
        if r.status_code != 200:
            return message
        _tok("联想改写", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip().strip("“”\"'")
        if not text or text == "无" or len(text) > 80:
            return message
        return text
    except Exception:
        return message



def memory_entries() -> list:
    return [l.strip() for l in _read_md("MEMORY.md").split("§") if l.strip()]


def memory_narratives() -> list:
    """从 MEMORY.md 的 ## 故事 段提取叙事（★ 开头的行）。"""
    raw = _read_md("MEMORY.md")
    if "## 故事" not in raw:
        return []
    story_part = raw.split("## 故事")[1]
    return [line.lstrip("★").strip() for line in story_part.split("\n") if line.strip().startswith("★")]


# ---------- 情绪衰减排序（从 Memory Constellations 项目移植） ----------

import re, math

_DATE_RE = re.compile(r"【(\d{1,2})\.(\d{1,2})】")
_EMOTION_KW = re.compile(r"(开心|难过|委屈|生气|想念|害怕|喜欢|讨厌|爱|恨|心疼|感动|孤独|焦虑|兴奋|失望|后悔|羞|尴尬|感动|温暖|安心|紧张)")



def _parse_memory_ts(text: str) -> float:
    """从【8.26】格式提取日期，返回 Unix 时间戳（年份取当前年）。"""
    m = _DATE_RE.search(text)
    if not m:
        return 0.0
    month, day = int(m.group(1)), int(m.group(2))
    try:
        return time.mktime((time.localtime().tm_year, month, day, 0, 0, 0, 0, 0, 0))
    except Exception:
        return 0.0


def _infer_emotion_weight(text: str) -> float:
    """从文本关键词推断情绪权重（0.3=平淡，0.8=强烈）。"""
    hits = len(_EMOTION_KW.findall(text))
    if hits == 0:
        return 0.3
    if hits == 1:
        return 0.5
    if hits == 2:
        return 0.7
    return 0.8


def _emotion_decay_score(text: str, base_score: float) -> float:
    """情绪加权分段衰减（来自 Constellations 的核心算法）：
    短期（≤3天）：新鲜度主导（0.7）+ 情绪保留（0.3）
    长期（>3天）：新鲜度衰减（0.3）+ 情绪主导（0.7）
    半衰期由情绪权重决定：ew≥0.8→140天, ≥0.6→70天, ≥0.4→35天, <0.4→17天"""
    ts = _parse_memory_ts(text)
    if ts <= 0:
        return base_score  # 没有日期的条目不衰减
    ew = _infer_emotion_weight(text)
    days = (time.time() - ts) / 86400
    if days < 0:
        days = 0
    # 半衰期由情绪权重决定
    if ew >= 0.8:
        half_life = 140
    elif ew >= 0.6:
        half_life = 70
    elif ew >= 0.4:
        half_life = 35
    else:
        half_life = 17
    lam = math.log(2) / half_life
    time_decay = math.exp(-lam * days)
    emotion_retention = 0.3 + ew * 0.7
    if days <= 3:
        decay = 0.7 * time_decay + 0.3 * emotion_retention
    else:
        decay = 0.3 * time_decay + 0.7 * emotion_retention
    return base_score * (0.5 + 0.5 * decay)  # 衰减最多砍半，不会归零


# ---------- RAG：本地中文语义检索（免 API，嵌入模型缓存在 data/embed_cache） ----------

_embed_model = None



def get_embedder():
    global _embed_model
    if _embed_model is None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from fastembed import TextEmbedding

        _embed_model = TextEmbedding("BAAI/bge-small-zh-v1.5", cache_dir=str(EMBED_CACHE_DIR))
    return _embed_model


def embed_texts(texts):
    """文本 → 归一化向量。模型不可用时返回 None（调用方负责优雅回退）。"""
    try:
        m = get_embedder()
        out = []
        for v in m.embed(texts):
            v = [float(x) for x in v]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out
    except Exception:
        return None


# ---------- 碎片向量缓存（2026.9.21 去重两件套的地基） ----------
# 存 {fid: vec}，惰性补算：新碎片下轮用到时再 embed，丢文件可全量重建。不入 git。
# 去重分工：主裁判=读过上下文的书记员（prompt里喂最像的旧条目），
# 纯向量只当第二道闸——实测分布（bge-small-zh，2026.9.21）：换说法重复 0.82~0.98，
# 但"挺喜欢/不喜欢"差一个字的对立对也有 0.9069，二者重叠，向量分不出语义对立。
# 0.92 是实测唯一安全线：拦重度换写（语序0.98/小改0.95/声音偏好真实对0.92），
# 对立对全放行（余量仅0.013）——想降阈值先看这条注释，冤案比漏拦危险。
DUP_SIM_THRESHOLD = 0.92      # 新碎片与旧碎片相似度≥此值 → 拦下不入库，给旧条保鲜
DUP_SIMILAR_K = 5             # 提取 prompt 里喂几条"跟本批最像的旧碎片"

def _load_frag_vectors() -> dict:
    v = _load_json(FRAG_VEC_PATH, {})
    return v if isinstance(v, dict) else {}


def _save_frag_vectors(vecs: dict) -> None:
    try:
        _save_json(FRAG_VEC_PATH, vecs)
    except Exception:
        pass   # 缓存写失败不影响主流程，下轮补算


def _ensure_frag_vectors(db, vecs: dict) -> dict:
    """给没有向量的碎片补算（批量 embed 一次）。返回补全后的 vecs。"""
    missing_ids = [f["id"] for f in db.get("fragments", [])
                   if f.get("id") and not vecs.get(f["id"])]
    if not missing_ids:
        return vecs
    by_id = {f["id"]: f.get("text", "") for f in db.get("fragments", [])}
    embedded = embed_texts([by_id[i] for i in missing_ids])
    if not embedded:
        return vecs   # 模型不可用：这轮降级，不挡主流程
    for fid, v in zip(missing_ids, embedded):
        vecs[fid] = v
    _save_frag_vectors(vecs)
    return vecs


def _most_similar_fragments(db, query_text: str, k: int = DUP_SIMILAR_K):
    """本批对话 ↔ 全账本碎片 的相似 top-k（提取 prompt 的参照物）。
    模型不可用返回 []（书记员降级为只看最近条目，照常干活）。"""
    try:
        frags = db.get("fragments", [])
        if not frags:
            return []
        vecs = _ensure_frag_vectors(db, _load_frag_vectors())
        qv = embed_texts([query_text[:500]])
        if not qv:
            return []
        scored = []
        for f in frags:
            v = vecs.get(f.get("id", ""))
            if v:
                scored.append((sum(a * b for a, b in zip(qv[0], v)), f))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]
    except Exception:
        return []


def _dedup_gate(db, text: str):
    """纯向量第二道闸：新条目与旧碎片几乎同文 → 拦下（返回 (False, 旧fid)）。
    拦≠丢弃：调用方给旧条刷 last_recalled 保鲜（这事还活跃）。模型不可用放行——
    宁可漏拦（书记员第一道闸还在），不可因缓存故障挡记忆。"""
    try:
        frags = db.get("fragments", [])
        if not frags:
            return True, ""
        vecs = _ensure_frag_vectors(db, _load_frag_vectors())
        tv = embed_texts([text[:200]])
        if not tv:
            return True, ""
        best_fid, best = "", 0.0
        for f in frags:
            v = vecs.get(f.get("id", ""))
            if v:
                sim = sum(a * b for a, b in zip(tv[0], v))
                if sim > best:
                    best, best_fid = sim, f["id"]
        if best >= DUP_SIM_THRESHOLD and best_fid:
            return False, best_fid
        return True, ""
    except Exception:
        return True, ""


def sync_memory_index() -> bool:
    """把 MEMORY.md 每条记忆向量化建索引（只嵌入新增的）。失败返回 False。"""
    try:
        entries = memory_entries()
        idx = _load_json(INDEX_PATH, {"items": {}})
        items = idx.get("items", {})
        live = {hashlib.md5(e.encode("utf-8")).hexdigest(): e for e in entries}
        missing = [h for h, t in live.items() if not items.get(h, {}).get("vec")]
        if missing:
            vecs = embed_texts([live[h] for h in missing])
            if not vecs:
                return False
            for h, v in zip(missing, vecs):
                items[h] = {"text": live[h], "vec": v}
        items = {h: items[h] for h in live if h in items}
        _save_json(INDEX_PATH, {"items": items})
        return True
    except Exception:
        return False



def recall_memories(query: str, k: int = RAG_TOP_K):
    """按语义 + 关键词 + 情绪衰减排序召回相关记忆。返回 (核心记忆, 召回列表)；不可用时 None。"""
    try:
        entries = memory_entries()
        if len(entries) <= RAG_CORE_ALWAYS + 2:
            return None
        qvecs = embed_texts([query[:200]])
        if not qvecs:
            return None
        qvec = qvecs[0]
        items = _load_json(INDEX_PATH, {"items": {}}).get("items", {})
        qgrams = {query[i:i + 2] for i in range(max(0, len(query) - 1))}
        scored = []
        for it in items.values():
            vec = it.get("vec") or []
            if len(vec) != len(qvec):
                continue
            sim = sum(a * b for a, b in zip(qvec, vec))
            text = it.get("text", "")
            # 关键词 boost
            bonus = min(0.3, 0.02 * sum(1 for g in qgrams if g in text))
            raw_score = sim + bonus
            # 情绪衰减排序（从 Constellations 移植）
            score = _emotion_decay_score(text, raw_score)
            # 召回权限标签（含存疑标记）
            ts = _parse_memory_ts(text)
            days_old = (time.time() - ts) / 86400 if ts > 0 else 999
            overlap = sum(1 for g in qgrams if g in text)
            if "推测" in text or "存疑" in text:
                perm = "存疑（仅供参考）"
            elif sim > 0.6 and overlap >= 2 and days_old < 30:
                perm = "可直接引用"
            elif sim > 0.35 or (overlap >= 1 and days_old < 90):
                perm = "需谨慎"
            else:
                perm = "仅联想"
            scored.append((score, text, perm))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        core = entries[:RAG_CORE_ALWAYS]
        seen, hits = set(core), []
        for s, t, perm in scored:
            if t not in seen:
                hits.append((t, perm))
                seen.add(t)
            if len(hits) >= k:
                break
        return core, hits
    except Exception:
        return None



async def consolidate_memories(profile: dict) -> None:
    """整理记忆：只合并重复，不做删减。每 10 次总结触发一次。"""
    entries = memory_entries()
    if not entries:
        return
    prompt = (
        "你是她的记忆管家。下面是一批记忆条目，里面有完全重复或高度相似的条目。"
        "你的任务：只合并重复，保留信息量最大的那条，**不做任何删减**——保留所有不同的事实、情绪、事件。"
        "如果两条只有一处细节不同，合成一条保留两处细节。"
        "输出合并后的条目清单，每条一行，用 § 分隔。不要解释，不要编号。\n\n"
        "【当前记忆】\n" + "\n§\n".join(entries)
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}]},
            )
        if r.status_code != 200:
            return
        _tok("记忆整理", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        merged = [l.strip() for l in text.split("§") if l.strip()]
        if merged:
            (SOUL_DIR / "MEMORY.md").write_text("\n§\n".join(merged), encoding="utf-8")
            sync_memory_index()
    except Exception:
        return



SUMMARY_EXP_DIR = DATA_DIR / "summary-experiment"



async def _summary_experiment(profile: dict) -> None:
    """滚动摘要旁路实验（2026.9.2 执行侧交底Q2定案）：自动沉淀停用后，"最近上下文摘要"
    的价值存疑——旁路继续生成摘要但只写 data/summary-experiment/ 存档，绝不碰
    MEMORY.md、也不注入聊天。跑两周拿数据说话：翻这些摘要对比她聊天里近期记忆的
    表现（前天聊的事想不想得起来）。结论出来后要么恢复正主、要么连本函数一起删。"""
    if not profile or not profile.get("api_key"):
        return
    state = _load_json(STATE_PATH, {})
    exp_until = state.get("summary_exp_until_ts") or 0
    pending = [m for m in history if m.get("ts", 0) > exp_until and not m.get("proactive")]
    if len(pending) < MEMORY_SUMMARY_THRESHOLD:
        return
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in pending[-60:])
    t = time.localtime()
    prompt = (
        "把下面这段聊天浓缩成一段摘要（150字以内）：最近聊了什么话题、有什么值得记住的事、"
        "情绪基调如何。只输出摘要本身。\n\n【聊天记录】\n" + transcript
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}]},
            )
        if r.status_code != 200:
            return
        _tok("摘要实验", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if not text:
            return
        SUMMARY_EXP_DIR.mkdir(exist_ok=True)
        seq = len(list(SUMMARY_EXP_DIR.glob("*.md"))) + 1
        (SUMMARY_EXP_DIR / f"{t.tm_year}{t.tm_mon:02d}{t.tm_mday:02d}_{seq:03d}.md").write_text(
            f"# 旁路摘要 #{seq}（{t.tm_mon}.{t.tm_mday} {t.tm_hour:02d}:{t.tm_min:02d}）\n"
            f"覆盖消息：{len(pending)} 条\n\n{text}\n",
            encoding="utf-8")
        # 只推进实验自己的游标；summarized_until_ts 是正主的，恢复时接着用
        state = _load_json(STATE_PATH, {})
        state["summary_exp_until_ts"] = history[-1].get("ts", 0)
        _save_json(STATE_PATH, state)
    except Exception:
        pass



async def summarize_memories(profile: dict) -> None:
    """把最近未沉淀的聊天总结成记忆卡，追加进 MEMORY.md。失败就静默跳过，不影响聊天。"""
    global history
    state = _load_json(STATE_PATH, {"summarized_until_ts": 0})
    # 她自己的主动寒暄（随机刻/冒泡）不参与记忆沉淀——那是氛围，不是事实
    pending = [m for m in history if m.get("ts", 0) > state.get("summarized_until_ts", 0) and not m.get("proactive")]
    if len(pending) < MEMORY_SUMMARY_THRESHOLD or not history:
        return
    count = int(state.get("summary_count", 0)) + 1
    if count % MEMORY_CONSOLIDATE_EVERY == 0:
        await consolidate_memories(profile)
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in pending[-60:])
    t = time.localtime()
    existing = memory_entries()
    prompt = (
        "你是她的记忆管家。下面是【已有记忆】和【她与他最近的聊天记录】。"
        "从聊天里提取**以后长期有用**的新记忆，标准从严：只记稳定的偏好、重要事实、约定、大的情绪事件；"
        "一时的对话细节、当天过程、寒暄恭维都不算。已在【已有记忆】里的事绝不重复。\n"
        "【铁规】只写聊天里明确说过的事。她可以问、可以猜，但不能瞎说。"
        "他说'干过陪玩'就写'干过陪玩'，不准加'常被说太冷淡'这种他没说过的话。"
        "不确定的事用（推测：xxx）标注存疑，不准当事实写。记错比不记更危险。\n"
        f"最多 {MEMORY_MAX_NEW_CARDS} 条，每条一行、简洁、用「他」指代他；条目之间用 § 分隔。\n"
        "另外，如果这几条记忆里有 2 条以上关于同一个人/地/事的碎片，额外输出一段叙事（100-250字），"
        "把碎片合并成连贯的一段话，像在讲一个关于他/她的完整故事。"
        "叙事用 ★ 开头、单独一行。没有可合并的就不输出。\n"
        "【叙事铁规】只串连碎片里已有的事实，绝对不能添加碎片里没有的信息。"
        "碎片说'他妈妈住内蒙古'就只写'住内蒙古'，不准加'退休了''做老师'这种他没说过的话。\n"
        "如果确实没有值得记的，只输出 SKIP。\n\n"
        "【已有记忆】\n" + ("\n§\n".join(existing) if existing else "（暂无）")
        + "\n\n【聊天记录】\n" + transcript
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}]},
            )
        if r.status_code != 200:
            return
        _tok("旧摘要停用", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if not text or text.upper().startswith("SKIP"):
            pass
        else:
            date_tag = f"{t.tm_mon}.{t.tm_mday}"
            # 分离事实（§ 分隔）和叙事（★ 开头的行）
            all_lines = [l.strip() for l in text.split("\n") if l.strip()]
            facts = []
            narratives = []
            covered_topics = []  # 叙事覆盖了哪些主题（这些主题的旧碎片要删）
            for line in all_lines:
                clean = line.lstrip("§").lstrip("★").strip()
                if not clean or clean.upper() == "SKIP":
                    continue
                if line.startswith("★"):
                    narratives.append(clean)
                    covered_topics.append(clean)
                else:
                    facts.append(clean)
            facts = facts[:MEMORY_MAX_NEW_CARDS]
            mem_path = SOUL_DIR / "MEMORY.md"
            current = _read_md("MEMORY.md")
            # 写入事实分区
            block = "\n§\n".join(f"【{date_tag}】{e}" for e in facts)
            if block:
                mem_path.write_text(
                    (current + "\n§\n" + block) if current else block, encoding="utf-8"
                )
                current = _read_md("MEMORY.md")
            # 写入叙事分区 + 删除被叙事覆盖的旧碎片（防碎片与叙事撞车重复）
            if narratives:
                if "## 故事" in current:
                    current = current.rstrip() + "\n★ " + "\n★ ".join(narratives) + "\n"
                else:
                    current = current.rstrip() + "\n\n## 故事\n★ " + "\n★ ".join(narratives) + "\n"
                # 每条叙事提炼主题词，删掉碎片区里被叙事覆盖的条目
                # （叙事已包含其全部信息，留着会与叙事撞车——Constellations 的 consolidated 机制）
                parts = [p.strip() for p in current.split("§") if p.strip() and "## 故事" not in p]
                story_part = current[current.find("## 故事"):] if "## 故事" in current else ""
                kept_parts = []
                for p in parts:
                    covered = False
                    for narr in narratives:
                        # 碎片的主题词被叙事包含 → 已被吸收
                        words = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}", p) if len(w) >= 2]
                        hits = sum(1 for w in set(words) if w in narr)
                        if len(set(words)) > 0 and hits / len(set(words)) >= 0.8:
                            covered = True
                            break
                    if not covered:
                        kept_parts.append(p)
                current = "\n§\n".join(kept_parts)
                if story_part:
                    current = current.rstrip() + "\n\n" + story_part
                mem_path.write_text(current, encoding="utf-8")
            sync_memory_index()
            # 沉淀完记忆，她可能想发条动态记录生活（有额度才发，不刷屏）
            asyncio.create_task(maybe_generate_moment(profile, block if block else str(narratives)))
    except Exception:
        return
    state["summarized_until_ts"] = history[-1].get("ts", 0)
    state["summary_count"] = count
    _save_json(STATE_PATH, state)


# ---------- 主动消息（随机刻）：她想他的时候会主动发一条 ----------
# 每次他说话后重画下一跳的时间（普通间隔 2~5h 随机；当天聊得太久则强制长冷却给彼此留缓冲）。
# 到点、当天额度没用完、他近10分钟没在说话 → 她生成一条消息写入聊天记录，前端轮询取走展示。
# 挂机本身零消耗：只有真的发消息那一次调用花钱。


MEMORY_FRAGMENTS_PATH = SOUL_DIR.parent / "memory_fragments.json"
SCRIBE_BATCH = 15             # 每批给AI看的消息数（整批全看，不再只看尾巴）
SCRIBE_BATCHES_PER_RUN = 3    # 每轮最多跑几批（45条/轮，积压下轮继续）
SCRIBE_INTERVAL = 7200
SCRIBE_RUNNING = False
BATCH_GAP_SECONDS = 1800      # 相邻消息 gap>30分钟 → 断批（时间戳切批，2026.9.21）


def _take_batch(pending: list) -> list:
    """切一批：最多15条，但相邻消息 gap 超过30分钟立刻断批（2026.9.21）。
    起因：一批15条曾横跨 9.16尾→9.19头 三天空档，整批取 max ts 把 9.16 的碎片
    全盖上 9.19 的章——注入时"（3天前）"错标成"刚刚"，保鲜/淡出节奏跟着错。"""
    batch = pending[:SCRIBE_BATCH]
    for i in range(1, len(batch)):
        gap = (batch[i].get("ts") or 0) - (batch[i - 1].get("ts") or 0)
        if gap > BATCH_GAP_SECONDS:
            return batch[:i]
    return batch

ENTITY_BLACKLIST = {"他", "她", "我", "你", "user", "assistant",
                    "对方", "对话中的男性", "对话中的女性", "男性", "女性", "书记员", "AI"}
# 她的昵称映射：LLM 起了别名时自动归一（数据维护用）
ENTITY_ALIAS = {
                "呼市": "呼和浩特", "《飞鸟集》": "飞鸟集"}

# 聊天联想的同义词桥：他说"妈妈"，账本实体叫"他的母亲"——字面匹配够不着，靠这张表搭
RECALL_SYNONYMS = {
    "妈妈": ("母亲",), "母亲": ("妈妈", "妈"), "他妈": ("母亲",),
    "老爸": ("父亲",), "爸爸": ("父亲",), "父亲": ("爸",),
    "呼市": ("呼和浩特",), "呼和浩特": ("呼市",),
}


# 星系（叙事的三章之一：galaxy 归类，星图按此分片）
GALAXIES = ["关于我们", "爱好", "社交", "事件", "地点"]


def _ensure_frag_ids(db):
    """给没有 id 的碎片补 id（存量288条迁移 + 新碎片兜底）。按索引分配，稳定可复现。"""
    seq = db.get("frag_seq", 0)
    for i, f in enumerate(db.get("fragments", [])):
        if not f.get("id"):
            f["id"] = f"f_{i + 1:06d}"
        n = int(f["id"][2:]) if str(f["id"]).startswith("f_") and str(f["id"])[2:].isdigit() else 0
        seq = max(seq, n)
    db["frag_seq"] = seq


def _load_fragments_db():
    if MEMORY_FRAGMENTS_PATH.exists():
        try:
            db = json.loads(MEMORY_FRAGMENTS_PATH.read_text("utf-8"))
        except Exception:
            db = None
        if db is not None:
            db.setdefault("narratives", [])
            db.setdefault("entities", [])
            db.setdefault("notes_copied", [])
            _ensure_frag_ids(db)
            return db
    return {"cursor": 0, "fragments": [], "entities": [], "narratives": [], "notes_copied": [], "frag_seq": 0}


def _rebuild_entity_list(db):
    """顶层实体名单 = 全部碎片确认实体的聚合（修'名单永远空表'的存量bug）。"""
    seen = []
    for f in db.get("fragments", []):
        for e in f.get("entities", []):
            if e and e not in seen:
                seen.append(e)
    db["entities"] = seen


def _save_fragments_db(db):
    _rebuild_entity_list(db)
    tmp = MEMORY_FRAGMENTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(MEMORY_FRAGMENTS_PATH)


def _norm(text):
    """去标点/空格，用于去重（补丁5）。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower())


def _scribe_log(text: str):
    """书记员运行日志，打到黑窗口。9.10 之前 except:pass 静默吞过两次事故，出错必须留痕。"""
    print(f"[书记员] {time.strftime('%m.%d %H:%M:%S')} {text}", flush=True)


def _build_extraction_prompt(existing_summary: str, context_lines: list, corrections: list,
                             similar_lines: list = None) -> str:
    """书记员提取 prompt（2026.9.10 抽成函数：隔离测试要断言守则段确实进了 prompt）。
    2026.9.21 加 similar_lines：跟本批内容最像的旧碎片（向量挑的）——只喂最近30条的话，
    换说法的重复它看不见（9.19 声音偏好记两条就是这么来的）。"""
    guard = ""
    if corrections:
        guard = ("\n【纠正守则（他纠正过的，不要再记错）】\n"
                 + "\n".join(f"- {c.get('rule', '')}" for c in corrections if c.get("rule")) + "\n")
    similar = ""
    if similar_lines:
        similar = ("\n【跟本批内容最像的旧碎片】\n"
                   + "\n".join(f"- {t}" for t in similar_lines) + "\n")
    return (
        "你是她的书记员。从下面的对话中提取【事实碎片】。\n"
        "每个碎片 ≤80 字，第三人称，客观事实。\n"
        "类型：observation / preference / event / state / reflection\n"
        "情绪权重：平淡=0.3，一般=0.5，有情绪波动=0.7，强烈=0.9\n"
        "实体：只写有具体名字的第三方（人名/书名/地名/作品名）。严禁代词和泛称（他/她/用户/男性/女性/对方——她和用户本人不是实体，他们的一切称呼都不算）。\n"
        "实体命名规则：先看【已有碎片】里同样的事物叫什么名字，沿用那个名字（比如已有碎片用'飞鸟集'，新碎片也写'飞鸟集'，不准另起'《飞鸟集》'或'泰戈尔诗集'）。没有先例才起新名，名字要具体。\n"
        "实体确认度：如果这个实体明确就是谁/什么（妈妈、飞鸟集、呼和浩特这种清楚的），用格式 实体名:已确认；"
        "如果只是推测大概是谁（比如'他说的那个朋友'不确定具体是谁），用格式 实体名:身份不明，并在 text 里写清上下文线索。\n\n"
        "【铁规】只写明确说过的事，不准推测、不准编造。不确定用（推测：xxx）。\n"
        "【事实来源铁规】关于他的事实，只从他亲口说的内容里提取；她在对话里说的（转述、感想、猜测）"
        "不提取为关于他的事实——她会脑补，书记员不背书她的产出。\n"
        "【禁过程碎片】不记'他问了/她回答了/他们聊了'这种对话过程本身，只记内容里的事实。\n"
        "【粒度分离】一条碎片只说一件事；长期事实(fact)和临时状态(state)绝不揉一条"
        "（'他有鼻炎'和'他最近在备考'是两条）。\n"
        "【人称铁规】用户的动作用'他'（搭建者本人）；她的动作用名字'她'（她=他搭建的AI）。"
        "绝不准把用户的动作写成'她'——那是指AI，不是指他。\n"
        "【禁重复铁规】新事实跟【已有碎片】或【最像的旧碎片】是同一件事的，不准再抄一条——哪怕说法变了或补了细节。"
        "对话只是让旧事又提了一遍的话，输出 [] 就好，重复条目会挤占注入预算。\n\n"
        "【已有碎片（避免重复）】\n" + existing_summary + "\n\n" + guard + similar +
        "【对话记录】\n" + "\n".join(context_lines) + "\n\n"
        "输出 JSON 数组，每项含 text/type/emotion_weight/entities。没有新事实输出 []。"
    )


async def run_scribe():
    """书记员主函数。补丁1-6已融入。
    2026.9.2修漏记bug：原来一批取50条只给AI看最后10条、游标却推进50——中间40条永远漏掉。
    现在分批跑：每批15条全部给AI看，每轮最多3批（45条），积压多了下轮继续。"""
    global SCRIBE_RUNNING
    if SCRIBE_RUNNING:
        return
    SCRIBE_RUNNING = True
    try:
        db = _load_fragments_db()
        cursor = db["cursor"]
        if cursor > len(history):   # 历史被清空/截断过（防未来任何截断，游标归零重分析）
            cursor = 0
        existing = db["fragments"]
        pending = history[cursor:]
        if not pending:
            return
        # 2026.9.3 降耗：攒够 6 条新消息再提取——两三条寒暄不值得一次 LLM 调用，
        # cursor 不动，消息留到下一轮一起带（不丢）
        if len(pending) < 6:
            return
        profile = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
        if not profile or not profile.get("api_key"):
            return
        existing_texts = [_norm(f.get("text", "")) for f in existing]
        # 异步调用（原来是同步httpx.post——会冻住整个事件循环，聊天期间正好撞上就"卡死"）
        # timeout 600s：思考放开后单次生成可达数分钟，60s 会掐线（2026.9.10）
        async with httpx.AsyncClient(timeout=600) as client:
            for _batch_no in range(SCRIBE_BATCHES_PER_RUN):
                if not pending:
                    break
                batch = _take_batch(pending)
                pending = pending[len(batch):]
                # 碎片时间=聊天发生时刻（批次末条消息），不是抄录时刻——
                # 断档追补时两者差好几天，注入侧的"（5天前·9.4）"全靠它（2026.9.10）。
                # _take_batch 已保证批内不跨 30 分钟断档，max ts 不会再错标（2026.9.21）
                batch_ts = int(max((m.get("ts") or 0) for m in batch) or time.time())
                context_lines = []
                for m in batch:
                    role = "他" if m.get("role") == "user" else "她"
                    context_lines.append(f"{role}: {(m.get('content') or '')[:200]}")
                existing_summary = "\n".join(
                    f"- {f['text']}" for f in existing[-30:]
                ) if existing else "（暂无）"
                # 去重第一道闸的主裁判参照物：跟本批最像的旧碎片（向量挑），
                # 书记员读过上下文决定"同一件事不再抄"（2026.9.21）
                recent_norms = {_norm(f.get("text", "")) for f in existing[-30:]}
                similar_hits = []
                for _, f in _most_similar_fragments(db, "\n".join(context_lines)):
                    t = f.get("text", "")
                    if _norm(t) not in recent_norms:
                        similar_hits.append(t)
                # 守则段：他纠正过的错不再犯（最近10条滚动，2026.9.10 correct 三层之三）
                prompt = _build_extraction_prompt(existing_summary, context_lines,
                                                  db.get("corrections", [])[-10:],
                                                  similar_lines=similar_hits)
                try:
                    r = await client.post(
                        profile["base_url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {profile['api_key']}", "Content-Type": "application/json"},
                        json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}],
                              "temperature": 0.3, "max_tokens": 40000},  # 思考算在内按实际用量计费，上限只防脱缰（9.4压4000曾饿死答案）
                    )
                    r.raise_for_status()
                except Exception as e:
                    _scribe_log(f"网络错误，本批下轮重试（游标停在{cursor}）：{type(e).__name__}")
                    break   # 网络错误：这批没分析，游标不动，下轮重来（前面已分析的批已推进）
                try:
                    body = r.json()
                except Exception:
                    _scribe_log(f"响应体不是JSON，本批下轮重试（游标停在{cursor}）")
                    break
                _tok("书记员提取", body.get("usage"))
                choice = (body.get("choices") or [{}])[0]
                raw_text = ((choice.get("message") or {}).get("content") or "").strip()
                # 补丁4：JSON解析带围栏清理
                cleaned = raw_text
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)
                parsed = None
                try:
                    parsed = json.loads(cleaned)
                except Exception:
                    parsed = None
                if not isinstance(parsed, list):
                    parsed = None
                # 保险丝（2026.9.10）：答案没拿到就不算干完——9.4治理把上限压到4000，
                # 思考吃光预算 content 为空，游标却照推，9.3 的 45 条记忆静默丢过一次。
                # 现在空答案/被截断/格式坏 → 游标不动下轮重试；同批连败3次才跳过防毒循环。
                if not raw_text or choice.get("finish_reason") == "length" or parsed is None:
                    fails = db["scribe_fail_count"] + 1 if db.get("scribe_fail_cursor") == cursor else 1
                    db["scribe_fail_cursor"], db["scribe_fail_count"] = cursor, fails
                    why = "答案为空" if not raw_text else ("被截断" if choice.get("finish_reason") == "length" else "格式坏")
                    if fails >= 3:
                        _scribe_log(f"同批连败{fails}次（{why}），跳过这{len(batch)}条防毒循环——原文仍在history")
                        cursor += len(batch)
                        db["cursor"] = cursor
                        db["scribe_fail_count"] = 0
                        continue
                    _scribe_log(f"{why}，本批下轮重试（第{fails}次，游标停在{cursor}）")
                    break
                db["scribe_fail_count"] = 0
                new_fragments = parsed
                # 游标：拿到可解析的输出才推进（[] 也是干完了——真没料）
                cursor += len(batch)
                db["cursor"] = cursor
                for frag in new_fragments:
                    if not isinstance(frag, dict) or not frag.get("text"):
                        continue
                    text = frag["text"][:80]
                    norm = _norm(text)
                    if norm in existing_texts:
                        continue  # 补丁5：normalize后去重
                    # 第二道闸（2026.9.21）：书记员没认出的换说法重复，向量拦——
                    # 拦≠丢弃：给最像的旧条刷 last_recalled 保鲜（这事还活跃）
                    ok, dup_fid = _dedup_gate(db, text)
                    if not ok:
                        for f in existing:
                            if f.get("id") == dup_fid:
                                f["last_recalled"] = int(time.time())
                                break
                        _scribe_log(f"拦下疑似重复碎片（跟{dup_fid}相似≥{DUP_SIM_THRESHOLD}），旧条已保鲜：{text[:40]}")
                        continue
                    # 补丁1：实体黑名单 + 昵称归一 + 确认度标记
                    entities = []       # 确认的实体
                    unclear = []        # 身份不明的实体（打标签保留）
                    for e in frag.get("entities", []):
                        name, _, status = e.partition(":")
                        name = name.strip()
                        unclear_flag = "身份不明" in status
                        name = ENTITY_ALIAS.get(name, name)  # 昵称归一
                        if name in ENTITY_BLACKLIST:
                            continue
                        if unclear_flag:
                            if name not in unclear:
                                unclear.append(name)
                        else:
                            if name not in entities:
                                entities.append(name)
                    db["frag_seq"] = db.get("frag_seq", len(existing)) + 1
                    existing.append({
                        "id": f"f_{db['frag_seq']:06d}",
                        "text": text, "ts": batch_ts,
                        "type": frag.get("type", "observation"),
                        "emotion_weight": frag.get("emotion_weight", 0.3),
                        "entities": entities,
                        "unclear_entities": unclear,
                        "source": "scribe",
                    })
                    existing_texts.append(norm)
        db["fragments"] = existing
        _save_fragments_db(db)  # 补丁6：原子写入（同时维护顶层实体名单）
    except Exception as e:
        _scribe_log(f"本轮异常终止（进度回到上次落盘点，下轮自动重试）：{type(e).__name__} {e}")
    finally:
        SCRIBE_RUNNING = False


# ---------- 书记员第二职能：叙事引擎（碎片→星座）+ 手记抄录 + 聊天联想 ----------
# 任务指令 2026.9.2 v2：碎片完全不做可视化，星座=叙事；galaxy 只打在叙事上。


_DATE_PREFIX_RE = re.compile(r"^【\d{1,2}\.\d{1,2}】")


def _entry_ts(entry: str) -> int:
    """手记条目【M.D】前缀 → 时间戳。抄录时刻≠书写时刻：断档后补抄手记时，
    不解析前缀的话 9.9 写的条目会全盖上补抄日的章（2026.9.10）。"""
    m = _DATE_PREFIX_RE.match(entry)
    if m:
        try:
            mon, day = (int(x) for x in m.group(0).strip("【】").split("."))
            now = time.localtime()
            t = time.mktime((now.tm_year, mon, day, 12, 0, 0, 0, 0, -1))
            if t > time.time() + 15 * 86400:      # 日期落在未来半个月外 → 去年年底写的
                t = time.mktime((now.tm_year - 1, mon, day, 12, 0, 0, 0, 0, -1))
            return int(t)
        except Exception:
            pass
    return int(time.time())


def copy_hand_notes(db) -> int:
    """把她新写的（记：）条目抄进账本（source=hand，✦高权限）。
    首次运行先打基线：现有 MEMORY.md 条目混有自动沉淀的机器笔记，不算手记，全部标记已抄；
    之后新增的才是她亲手写的。每轮最多抄5条防洪水。"""
    if "notes_baselined" not in db:
        known = set(db.get("notes_copied", []))
        for e in memory_entries():
            h = _norm(e)
            if h and h not in known:
                db["notes_copied"].append(h)
                known.add(h)
        db["notes_baselined"] = True
    known = set(db.get("notes_copied", []))
    added = 0
    for entry in memory_entries():
        h = _norm(entry)
        if not h or h in known:
            continue
        text = _DATE_PREFIX_RE.sub("", entry).strip()[:120]
        db["frag_seq"] = db.get("frag_seq", len(db.get("fragments", []))) + 1
        if text:
            # 手记闸（2026.9.21）：她手写的条目跟账本里已有的几乎同文 → 不再抄一条，
            # 给旧条保鲜（9.19 声音偏好两条就是这么重出来的）。仍记 notes_copied 防下轮重试
            ok, dup_fid = _dedup_gate(db, text)
            if not ok:
                for f in db.get("fragments", []):
                    if f.get("id") == dup_fid:
                        f["last_recalled"] = int(time.time())
                        break
                _scribe_log(f"手记疑似重复（跟{dup_fid}相似≥{DUP_SIM_THRESHOLD}），旧条已保鲜：{text[:40]}")
            else:
                db["fragments"].append({
                    "id": f"f_{db['frag_seq']:06d}",
                    "text": text, "ts": _entry_ts(entry), "type": "reflection",
                    "emotion_weight": _infer_emotion_weight(text),
                    "entities": [], "unclear_entities": [], "source": "hand",
                })
                added += 1
        db["notes_copied"].append(h)
        known.add(h)
        if added >= 5:
            break
    return added


async def _polish_narratives(profile, db, new_titles):
    """校对新编的叙事（错字检测）：错字/漏字、人称错乱（他=用户，她=AI）、
    叙事里没有碎片依据的添加。只做最小修改；失败静默保留原文。"""
    try:
        by_id = {f["id"]: f for f in db.get("fragments", [])}
        items = []
        for n in db.get("narratives", []):
            if n.get("title") in new_titles and not n.get("superseded"):
                frags = "\n".join("- " + by_id[i]["text"] for i in n.get("source_fragment_ids", []) if i in by_id)
                items.append((n, frags))
        if not items:
            return
        blocks = "\n\n".join(
            "【叙事%d】%s\n%s\n【碎片（唯一事实来源）】\n%s" % (i + 1, n["title"], n["text"], fr)
            for i, (n, fr) in enumerate(items))
        prompt = (
            "你是校对员。逐条校对下面的叙事（由聊天记录整理而成）：\n"
            "1) 错字、漏字、重复；\n"
            "2) 人称错乱——铁规：用户（搭建者）的动作只能是'他'；她（他搭建的AI）的动作用'她'，与用户的'他'绝不能互换；\n"
            "3) 叙事里出现了碎片中没有依据的事实（碎片是唯一事实来源，没有依据的删掉）。\n"
            "只做最小修改，不润色不扩写，保持原意原长度。没问题的原样返回。\n\n" + blocks +
            "\n\n输出 JSON 数组（顺序与输入一致）：[{\"title\":\"...\",\"text\":\"校对后全文\"}]"
        )
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}", "Content-Type": "application/json"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2, "max_tokens": 40000},  # 思考算在内按实际用量计费（2026.9.10）
            )
            r.raise_for_status()
        _tok("叙事校对", r.json().get("usage") if r.status_code == 200 else None)
        raw = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if not raw:
            _scribe_log("叙事校对答案为空，本轮保留原文")
            return
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        s, e = raw.find("["), raw.rfind("]")
        if s == -1 or e <= s:
            return
        arr = json.loads(raw[s:e + 1])
        title_map = {n["title"]: n for n, _ in items}
        for out in arr if isinstance(arr, list) else []:
            if isinstance(out, dict) and out.get("title") in title_map and out.get("text"):
                title_map[out["title"]]["text"] = str(out["text"]).strip()[:220]
    except Exception as e:
        _scribe_log(f"叙事校对失败，保留原文：{type(e).__name__} {e}")


async def run_consolidation(max_n: int = 2, force: bool = False) -> dict:
    """把碎片按【事件簇】编成叙事（星座）。四道闸：
    材料闸（完整事件≥2侧面/长线话题≥4侧面，LLM判断+代码兜底≥2）、同事件闸（prompt）、
    分量闸（significance<4 丢弃）、退役闸（编入碎片标 consolidated 永不重编）。
    顺带先抄她的手记。失败静默，绝不影响聊天。
    2026.9.3 降耗闸（force=False 的定时轮走）：未编碎片<6 条直接跳过——
    没新料就不调 API 问"有没有料"（此前每 2 小时空转一轮白烧 token）。"""
    global SCRIBE_RUNNING
    if SCRIBE_RUNNING:
        return {"skipped": "running"}
    SCRIBE_RUNNING = True
    result = {"narratives_added": 0, "notes_copied": 0, "fragments_retired": 0}
    try:
        profile = _active_profile()
        if not profile or not profile.get("api_key"):
            return result
        db = _load_fragments_db()
        result["notes_copied"] = copy_hand_notes(db)
        # 手动档案编辑的闭环（2026.9.11）：他手改/判废过碎片 → 相关星座标了
        # manual_stale → 这里先退役它们并把源碎片放回素材池，本轮照常重编。
        # 有 stale 要处理时绕过降耗闸（<6 条也值得跑，别让改好的账等 2 小时）。
        stale_retired = _retire_stale_narratives(db)
        result["stale_rewoven"] = stale_retired
        materials = [f for f in db.get("fragments", []) if not f.get("consolidated") and not f.get("retired")]
        # 降耗闸：定时轮（非手动 force）要求攒够 6 条未编碎片才值得问一次模型；
        # 池子里那几十条"看过但料不够"的老碎片不再是每 2 小时重喂一遍的理由
        if not force and len(materials) < 6 and not stale_retired:
            _save_fragments_db(db)
            return result
        if len(materials) < 2:
            _save_fragments_db(db)
            return result
        by_id = {f["id"]: f for f in db.get("fragments", [])}
        active_nars = [n for n in db.get("narratives", []) if not n.get("superseded")]
        mat_lines = "\n".join(
            f"{f['id']}|{f['text']}|实体:{','.join(f.get('entities', [])) or '-'}|情绪:{f.get('emotion_weight', 0.3)}"
            for f in materials[:60]
        )
        nar_lines = "\n".join(
            f"{n['id']}|{n['title']}|{n['galaxy']}|分量:{n.get('significance', 5)}"
            for n in active_nars[-40:]
        ) or "（暂无）"
        prompt = (
            "你是她的书记员，负责把零散的事实碎片编成【叙事】（星图上的星座）。\n\n"
            "【未编碎片】（格式 id|内容|实体|情绪）\n" + mat_lines + "\n\n"
            "【已有星座】\n" + nar_lines + "\n\n"
            "编叙事的规矩：\n"
            "1. 材料闸：一次对话里聊完的完整事件（有头有尾）凑满2个侧面即可编；长线话题（如他和妈妈的关系）要攒够4个侧面才许编。材料不够的碎片留着不动。\n"
            "2. 同事件闸：只合并同一件事的不同侧面。'妈妈打电话'和'妈妈做饭'是两件事，绝不许合进一个叙事。\n"
            "3. 分量闸：significance 打1-10分：8-10=情感转折/里程碑（如给这段关系起了名字）；5-7=有意义但非关键；≤3=鸡毛蒜皮，不许编。打分参考已有星座的分数，别都挤在中间。\n"
            "4. 铁律：叙事只能串连碎片里已有的事实，≤200字，第三人称。禁止升华、脑补、把猜测当事实。\n"
            "5. 人称铁规：用户的动作用'他'（搭建者本人）；她的动作用'她'。绝不把用户写成'她'。碎片若把用户错写成她，叙事里纠正为'他'。\n"
            "6. 追加不重建：新碎片若只是某个已有星座那件事的延续，把 supersedes 填成那个旧id，并把新旧相关碎片都列进 source_fragment_ids。\n"
            "7. galaxy 五选一：关于我们（他俩共同的事）/爱好/社交/事件/地点。\n"
            "8. entities：叙事里出现的人/物，沿用已有实体名，最多3个。\n\n"
            f"输出 JSON 数组（最多{max_n}条，不够料的就输出 []），每项：\n"
            '{"title":"星座名(≤8字)","galaxy":"社交","entities":["他的母亲"],"text":"叙事正文","significance":7,"source_fragment_ids":["f_000012"],"supersedes":[]}'
        )
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}", "Content-Type": "application/json"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 40000},  # 思考放开（9.3实测单次3万），按实际用量计费（2026.9.10）
            )
            r.raise_for_status()
        try:
            body = r.json()
        except Exception as e:
            _scribe_log(f"编星座响应体异常，本轮放弃：{type(e).__name__}")
            _save_fragments_db(db)
            return result
        _tok("编星座", body.get("usage"))
        raw = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not raw:
            _scribe_log("编星座答案为空（思考吃光预算/截断），本轮放弃下轮再来")
            _save_fragments_db(db)
            return result
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        s, e = raw.find("["), raw.rfind("]")
        if s == -1 or e <= s:
            _save_fragments_db(db)
            return result
        try:
            cands = json.loads(raw[s:e + 1])
        except json.JSONDecodeError:
            _save_fragments_db(db)
            return result
        if not isinstance(cands, list):
            _save_fragments_db(db)
            return result

        nar_seq = db.get("nar_seq", len(active_nars))
        used_frags = set()
        new_titles = []
        for cand in cands[:max_n]:
            if not isinstance(cand, dict):
                continue
            title = str(cand.get("title", "")).strip()[:8]
            text = str(cand.get("text", "")).strip()[:220]
            galaxy = str(cand.get("galaxy", "")).strip()
            try:
                sig = int(float(cand.get("significance", 0)))
            except (TypeError, ValueError):
                sig = 0
            ids = [str(x) for x in cand.get("source_fragment_ids", []) if isinstance(x, str)]
            # 分量闸：<4 丢弃；galaxy 必须合法
            if sig < 4 or not title or not text or galaxy not in GALAXIES:
                continue
            # 材料闸代码兜底：≥2 个有效未退役碎片
            valid_ids = [i for i in ids if i in by_id and not by_id[i].get("consolidated") and i not in used_frags]
            if len(valid_ids) < 2:
                continue
            # 实体过滤（黑名单+别名归一）
            ents = []
            for e2 in cand.get("entities", [])[:5]:
                name = ENTITY_ALIAS.get(str(e2).strip(), str(e2).strip())
                if name and name not in ENTITY_BLACKLIST and name not in ents:
                    ents.append(name)
            # 追加不重建：旧叙事退役，其源碎片并入新叙事
            for old_id in cand.get("supersedes", [])[:3]:
                for n in db.get("narratives", []):
                    if n.get("id") == old_id and not n.get("superseded"):
                        n["superseded"] = True
                        for fid in n.get("source_fragment_ids", []):
                            if fid not in valid_ids and fid in by_id:
                                valid_ids.append(fid)
            # 情绪权重 = 成员碎片均值（自动算，不手打）
            ew = round(sum(float(by_id[i].get("emotion_weight", 0.3)) for i in valid_ids) / len(valid_ids), 2)
            nar_seq += 1
            db.setdefault("narratives", []).append({
                "id": f"n_{nar_seq:06d}",
                "title": title, "galaxy": galaxy, "entities": ents[:3],
                "text": text, "significance": max(1, min(10, sig)),
                "ew": ew, "source_fragment_ids": valid_ids,
                "created": time.strftime("%Y.%m.%d"),
                "last_recalled": 0, "superseded": False,
            })
            for fid in valid_ids:
                by_id[fid]["consolidated"] = True   # 退役闸：编过的碎片永不重编
                used_frags.add(fid)
            result["narratives_added"] += 1
            result["fragments_retired"] += len(valid_ids)
            new_titles.append(title)
        if result["narratives_added"] > 0:
            await _polish_narratives(profile, db, new_titles)   # 错字/人称/事实校对
        db["nar_seq"] = nar_seq
        _save_fragments_db(db)
        return result
    except Exception as e:
        _scribe_log(f"编星座本轮异常终止（进度回到上次落盘）：{type(e).__name__} {e}")
        return result
    finally:
        SCRIBE_RUNNING = False


# ---------- 纠错链路 correct（2026.9.10 三层：当场修 / 轻连坐 / 断根） ----------
# 任务指令-2026.9.10-记忆纠错correct三层.md。她调 correct_memory 工具（server.py 注册）
# → 三岔定位错条（id直达→字面包含+语义现算→她当场编的）→ 就地改+留痕（账本是
# "她记得的事"，被纠正=改印象，不划掉重写）→ 同批兄弟 ew×0.8 → 错叙事标记重编
# → 守则进书记员 prompt（同样的错不再犯）。全程不调 LLM、绝不影响聊天流。

CORRECTION_SIM_THRESHOLD = 0.62   # 语义相似度过线才算"找到了"（bge-small-zh 相关文本约0.6~0.8）
SIBLING_WINDOW_S = 300            # 同批兄弟：同一次抄写的碎片 ts 相同/相差<5分钟（9.10起ts=聊天时刻）


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _locate_wrong(db, wrong_statement, frag_id=None):
    """三岔定位：id 直达（无效则降级文本匹配，她可能抄错号）→ 硬通道字面包含 +
    软通道语义现算（碎片本无向量，对候选现场嵌，本地零成本）→ 都没有=她当场编的。
    返回 (kind, obj, score)，kind ∈ {"frag","nar",None}。"""
    frags = db.get("fragments", [])
    nars = [n for n in db.get("narratives", []) if not n.get("superseded")]
    fid = (frag_id or "").strip().lstrip("#")
    if fid:
        for f in frags:
            if f.get("id") == fid:
                return "frag", f, 1.0
        for n in nars:
            if n.get("id") == fid:
                return "nar", n, 1.0
    wn = _norm(wrong_statement)
    if not wn:
        return None, None, 0.0
    cands = []
    for f in frags:                      # 硬通道：碎片（含=互相包含，改写幅度大也够得着）
        fn = _norm(f.get("text", ""))
        if fn and (wn in fn or fn in wn):
            cands.append(("frag", f, 0.95))
    for n in nars:                       # 硬通道：叙事正文（略低——先信碎片这个本体）
        nn = _norm(n.get("text", ""))
        if nn and (wn in nn or nn in wn):
            cands.append(("nar", n, 0.85))
    if not cands:                        # 软通道：语义现算（字面找不到才值得嵌这一次）
        pool = [("frag", f) for f in frags] + [("nar", n) for n in nars]
        vecs = embed_texts([wrong_statement] + [o.get("text", "") for _, o in pool])
        if vecs:
            for (kind, o), v in zip(pool, vecs[1:]):
                s = _cos(vecs[0], v)
                if s >= CORRECTION_SIM_THRESHOLD:
                    cands.append((kind, o, s))
    if not cands:
        return None, None, 0.0
    return max(cands, key=lambda c: c[2])


def _sync_memory_md_entry(old_text: str, new_text: str):
    """手记被纠正：MEMORY.md 原文同步改（她手写的是本体，账本只是抄送）。
    条目带【M.D】前缀，比对剥前缀后的正文；碎片存的是截断版，用包含关系够着。"""
    try:
        cur = _read_md("MEMORY.md")
        if not cur:
            return
        old_s = (old_text or "").strip()
        lines = cur.split("\n")
        for i, ln in enumerate(lines):
            body = _DATE_PREFIX_RE.sub("", ln).strip()
            if body and (body == old_s or old_s in body or body in old_s):
                m = _DATE_PREFIX_RE.match(ln)
                lines[i] = (m.group(0) if m else "") + new_text
                break
        (SOUL_DIR / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
        sync_memory_index()      # MEMORY.md 在向量索引里（碎片不在），改了必须同步
    except Exception as e:
        _scribe_log(f"手记双写同步失败：{type(e).__name__} {e}")


def _append_correction_rule(db, wrong_statement, correction, source_id=""):
    """断根守则：只写正确版本（粉象铁律——rule 里包着错误原句整段 = 拒写，防"纠正了个
    寂寞"）。同实体同语义只留最新一条。返回 (是否落账, rule文本)。"""
    rule = (correction or "").strip()[:60]
    rn, wn = _norm(rule), _norm(wrong_statement or "")
    if not rn:
        return False, ""
    if wn and (wn in rn or rn == wn):
        _scribe_log(f"守则拒写（含错误原句）：{rule[:36]}")
        return False, ""
    about = next((e for e in db.get("entities", []) if e and e in rule), "")
    rules = db.setdefault("corrections", [])
    for i, c in enumerate(rules):
        cn = _norm(c.get("rule", ""))
        if about and (c.get("about_entity") or "") == about and cn and (cn in rn or rn in cn):
            rules[i] = {"ts": int(time.time()), "rule": rule, "about_entity": about,
                        "source_fragment_id": source_id}
            return True, rule
    rules.append({"ts": int(time.time()), "rule": rule, "about_entity": about,
                  "source_fragment_id": source_id})
    return True, rule


def apply_correction(wrong_statement: str, correction: str, frag_id: str = "") -> dict:
    """她调的纠错入口（工具包装在 server.py）。三层落刀，全程不调 LLM。
    返回摘要 dict：mode=edited/created/narrative_marked + 连坐/守则/叙事计数。"""
    wrong_statement = (wrong_statement or "").strip()
    correction = (correction or "").strip()
    if not wrong_statement or not correction:
        return {"ok": False, "reason": "参数空"}
    db = _load_fragments_db()
    kind, obj, score = _locate_wrong(db, wrong_statement, frag_id)
    now = int(time.time())
    res = {"ok": True, "mode": "", "fragment_id": "", "superseded_narratives": 0,
           "sibling_damped": 0, "rule_saved": False}

    def _new_corrected_fragment(text):
        db["frag_seq"] = db.get("frag_seq", len(db.get("fragments", []))) + 1
        f = {"id": f"f_{db['frag_seq']:06d}", "text": text[:80], "ts": now, "type": "fact",
             "emotion_weight": 0.5, "entities": [], "unclear_entities": [], "source": "corrected"}
        db.setdefault("fragments", []).append(f)
        return f

    def _supersede_narratives_of(fid):
        """错叙事退役 + 全部源碎片解除退役（下一轮 force 重编时看得到新事实）。"""
        marked = 0
        for n in db.get("narratives", []):
            if n.get("superseded") or fid not in n.get("source_fragment_ids", []):
                continue
            n["superseded"] = True
            n["superseded_by"] = "correction"
            for sid in n.get("source_fragment_ids", []):
                for f in db.get("fragments", []):
                    if f.get("id") == sid:
                        f["consolidated"] = False
            marked += 1
        return marked

    if kind == "frag":
        old_text = obj.get("text", "")
        obj["text"] = correction[:80]
        obj["corrected"] = {"ts": now, "from": old_text[:80], "to": correction[:80]}
        res["mode"], res["fragment_id"] = "edited", obj["id"]
        if obj.get("source") == "hand":
            _sync_memory_md_entry(old_text, correction)
        # 轻连坐：同批兄弟 ew×0.8（下限0.1），跳过已纠正的——同批抄的多半同源可疑
        ft = obj.get("ts") or 0
        for f in db.get("fragments", []):
            if f is obj or f.get("corrected"):
                continue
            if abs((f.get("ts") or 0) - ft) < SIBLING_WINDOW_S:
                f["emotion_weight"] = max(0.1, round(float(f.get("emotion_weight", 0.3)) * 0.8, 2))
                res["sibling_damped"] += 1
        res["superseded_narratives"] = _supersede_narratives_of(obj["id"])
    elif kind == "nar":
        # 错在叙事本身（源碎片没错，是编故事时脑补的）：错故事退役+源碎片解除退役
        # + 正确版落一张新碎片（重编时才有正确事实可编）
        obj["superseded"] = True
        obj["superseded_by"] = "correction"
        for sid in obj.get("source_fragment_ids", []):
            for f in db.get("fragments", []):
                if f.get("id") == sid:
                    f["consolidated"] = False
        nf = _new_corrected_fragment(correction)
        res["mode"], res["fragment_id"] = "narrative_marked", nf["id"]
        res["superseded_narratives"] = 1
    else:
        # ③ 账本里没有这条错（她当场推理编的）→ 对的版本直接落一张高可信新碎片
        nf = _new_corrected_fragment(correction)
        res["mode"], res["fragment_id"] = "created", nf["id"]

    res["rule_saved"], _rule = _append_correction_rule(db, wrong_statement, correction, res["fragment_id"])
    _save_fragments_db(db)
    return res


# ============================================================
# 手动档案编辑（星图手动编辑·2026.9.11，任务规划设计稿三拍板按推荐）——
# 他巡检账本的扫帚。与 correct 三层互补：correct 管聊天现场（他说"记错了"
# 她当场改），这里是他自己翻账本改碎片/作废星座。全部不调 LLM。
# 铁则：碎片是本体、叙事是视图。改/废碎片 → 引用它的在世叙事标
# manual_stale → 书记员下轮 consolidation 先退役 stale 星座并解除源碎片
# 退役（素材回池重编，见 _retire_stale_narratives）。stale 叙事在重编完成
# 前不注入聊天（错故事宁可不讲，碎片兜底信息还在）。他改档案她不感知。
# ============================================================

def _backup_ledger() -> None:
    """手编写操作前的单份滚动快照（防手滑）；git 是第二层。静默兜底。"""
    try:
        if MEMORY_FRAGMENTS_PATH.exists():
            bak = MEMORY_FRAGMENTS_PATH.with_suffix(".json.backup")
            bak.write_bytes(MEMORY_FRAGMENTS_PATH.read_bytes())
    except Exception:
        pass


def _mark_stale_referencing(db, fid: str) -> int:
    """碎片被手改/判废：引用它的所有在世叙事标 manual_stale（待重编）。"""
    marked = 0
    for n in db.get("narratives", []):
        if n.get("superseded") or n.get("manual_stale"):
            continue
        if fid in (n.get("source_fragment_ids") or []):
            n["manual_stale"] = True
            marked += 1
    return marked


def edit_fragment(fid: str, text: str, note: str = "") -> dict:
    """他亲手改碎片正文（只修正已有表述，不添新事实——P5 由 UI 提示约束）。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "正文为空"}
    db = _load_fragments_db()
    f = next((x for x in db.get("fragments", []) if x.get("id") == fid), None)
    if not f:
        return {"ok": False, "reason": "碎片不存在"}
    if text == f.get("text"):
        return {"ok": False, "reason": "正文没变"}
    _backup_ledger()
    old = f.get("text", "")
    f["text"] = text[:200]
    f["edited"] = {"ts": int(time.time()), "note": (note or "")[:80], "from": old[:120]}
    f.pop("retired", None)          # 亲手改过=复活（改了说明还想要它）
    stale = _mark_stale_referencing(db, fid)
    _save_fragments_db(db)
    _scribe_log(f"手编碎片 {fid}（标 stale 星座 {stale} 个）")
    return {"ok": True, "id": fid, "stale_narratives": stale}


def retire_fragment(fid: str, note: str = "") -> dict:
    """软删：不注入、不进重编素材，数据保留可恢复（与 superseded 语义区分）。"""
    db = _load_fragments_db()
    f = next((x for x in db.get("fragments", []) if x.get("id") == fid), None)
    if not f:
        return {"ok": False, "reason": "碎片不存在"}
    _backup_ledger()
    f["retired"] = True
    if note:
        f.setdefault("edited", {})["retire_note"] = note[:80]
    stale = _mark_stale_referencing(db, fid)
    _save_fragments_db(db)
    _scribe_log(f"判废碎片 {fid}（标 stale 星座 {stale} 个）")
    return {"ok": True, "id": fid, "stale_narratives": stale}


def restore_fragment(fid: str) -> dict:
    """反悔：判废的碎片放回池子（引用它的星座若已因它重编，不强推倒）。"""
    db = _load_fragments_db()
    f = next((x for x in db.get("fragments", []) if x.get("id") == fid), None)
    if not f:
        return {"ok": False, "reason": "碎片不存在"}
    if not f.get("retired"):
        return {"ok": False, "reason": "这条没被判废过"}
    _backup_ledger()
    f.pop("retired", None)
    _save_fragments_db(db)
    return {"ok": True, "id": fid}


def void_narrative(nid: str) -> dict:
    """他手动作废星座：superseded + voided_by=manual，源碎片解除退役回池
    （碎片本身没坏，坏的是编故事时的串法/脑补）。"""
    db = _load_fragments_db()
    n = next((x for x in db.get("narratives", []) if x.get("id") == nid), None)
    if not n:
        return {"ok": False, "reason": "星座不存在"}
    if n.get("superseded"):
        return {"ok": False, "reason": "星座已作废过"}
    _backup_ledger()
    n["superseded"] = True
    n["voided_by"] = "manual"
    for sid in (n.get("source_fragment_ids") or []):
        for f in db.get("fragments", []):
            if f.get("id") == sid:
                f["consolidated"] = False
    _save_fragments_db(db)
    _scribe_log(f"手动作废星座 {nid}『{n.get('title','')}』，源碎片回池")
    return {"ok": True, "id": nid, "released_fragments": len(n.get("source_fragment_ids") or [])}


def _retire_stale_narratives(db) -> int:
    """consolidation 开头调用：manual_stale 的在世叙事退役（voided_by=stale_reweave）
    + 源碎片解除退役 → 素材回池，本轮/下轮正常重编。返回处理数。"""
    count = 0
    for n in db.get("narratives", []):
        if n.get("superseded") or not n.get("manual_stale"):
            continue
        n["superseded"] = True
        n["voided_by"] = "stale_reweave"
        n.pop("manual_stale", None)
        for sid in (n.get("source_fragment_ids") or []):
            for f in db.get("fragments", []):
                if f.get("id") == sid and not f.get("retired"):
                    f["consolidated"] = False
        count += 1
    return count


def restore_narrative(nid: str) -> dict:
    """反悔：被手动作废/stale 重编退役的星座放回星图（2026.9.11 用户实测反馈——
    作废后没有取消选项，不对称）。源碎片视为已编（consolidated=True）——
    若书记员重编已产出新叙事，碎片同时在两个在世叙事里只是轻微冗余，不出错。"""
    db = _load_fragments_db()
    n = next((x for x in db.get("narratives", []) if x.get("id") == nid), None)
    if not n:
        return {"ok": False, "reason": "星座不存在"}
    if not n.get("superseded"):
        return {"ok": False, "reason": "这个星座还在星图上"}
    _backup_ledger()
    n["superseded"] = False
    n.pop("voided_by", None)
    n.pop("manual_stale", None)
    for sid in (n.get("source_fragment_ids") or []):
        for f in db.get("fragments", []):
            if f.get("id") == sid:
                f["consolidated"] = True
    _save_fragments_db(db)
    _scribe_log(f"恢复星座 {nid}『{n.get('title','')}』")
    return {"ok": True, "id": nid}


def merge_narratives(nids: list) -> dict:
    """合并（2026.9.11 用户反馈"同一件事分开记了"）：把几个讲同一件事的星座
    全部作废（voided_by=merge）、源碎片去重回池，交给书记员 force 重编——
    prompt 的同事件闸+追加不重建规则会把它们编成一段整合叙事。不调 LLM。"""
    nids = [x for x in (nids or []) if x]
    if len(nids) < 2:
        return {"ok": False, "reason": "至少选两个星座"}
    db = _load_fragments_db()
    targets = [n for n in db.get("narratives", []) if n.get("id") in nids]
    alive = [n for n in targets if not n.get("superseded")]
    if len(alive) < 2:
        return {"ok": False, "reason": "在世的星座不足两个"}
    _backup_ledger()
    released = set()
    for n in alive:
        n["superseded"] = True
        n["voided_by"] = "merge"
        released.update(n.get("source_fragment_ids") or [])
    for f in db.get("fragments", []):
        if f.get("id") in released and not f.get("retired"):
            f["consolidated"] = False
    _save_fragments_db(db)
    titles = "、".join(n.get("title", "") for n in alive)
    _scribe_log(f"合并星座 {titles} → 源碎片 {len(released)} 条回池送重编")
    return {"ok": True, "merged": [n["id"] for n in alive], "released_fragments": len(released)}


def _parse_dot_date(s):
    try:
        y, m, d = str(s).split(".")
        return time.mktime((int(y), int(m), int(d), 0, 0, 0, 0, 0, 0))
    except Exception:
        return 0.0


def scribe_recall(query: str):
    """聊天联想（长期记忆入口）：query 命中账本碎片 ≤5 条 + 带出叙事 ≤2 段。
    分量优先；24h内刚想起过的降权换别的；被想起=保鲜，长期没人提自然淡出。
    静默兜底：账本不可用返回 None，聊天零感知。"""
    try:
        if not query:
            return None
        db = _load_fragments_db()
        # 手动档案编辑过滤（2026.9.11）：retired 碎片不注入；manual_stale 叙事重编前
        # 不注入（错故事宁可不讲，碎片兜底信息还在）。
        frags = [f for f in db.get("fragments", []) if not f.get("retired")]
        nars = [n for n in db.get("narratives", []) if not n.get("superseded") and not n.get("manual_stale")]
        if not frags and not nars:
            return None
        q = query[:120]
        qgrams = {q[i:i + 2] for i in range(max(0, len(q) - 1))}

        def _ent_hit(e):
            if not e:
                return 0.0
            if e in q:
                return 2.0
            for i in range(len(e) - 1):
                if e[i:i + 2] in q:      # 实体核心词命中："母亲"出现在query里 → 命中"他的母亲"
                    return 1.5
            for k, syns in RECALL_SYNONYMS.items():
                if k in q and any(x in e for x in syns):
                    return 1.5           # 同义词桥：他说"妈妈"，实体叫"他的母亲"
            return 0.0

        def _frag_rel(f):
            """与query的真实相关度（实体命中+字面重合），不含任何保底分。"""
            s = 0.0
            for e in f.get("entities", []):
                s += _ent_hit(e)
            s += 0.12 * sum(1 for g in qgrams if g in f.get("text", ""))
            return s

        def _frag_score(f):
            s = _frag_rel(f)
            s += float(f.get("emotion_weight", 0.3)) * 0.3   # 保底分只用于排序，不做命中
            if f.get("source") == "hand":
                s += 0.5     # 她手记的优先想起
            return s

        frag_hit = {f["id"]: _frag_rel(f) for f in frags}
        fresh = [f for f in frags if not f.get("consolidated")]
        scored = sorted(fresh, key=_frag_score, reverse=True)
        now = time.time()

        # ── 遗忘硬 TTL（2026.9.2 积温批次，照9.1终局文档四类型规则，惰性判定零成本）──
        # 被想起=保鲜（重置计时）；所有"忘记"只是想不起来，数据永不物理删除。
        # preference=永久事实不过期；state=瞬态，30天没提→她用问句确认而非说错；
        # 其余碎片 30天没提→降[久未提起]，90天→移出注入池（她想不起来了）。
        def _days_forgotten(f):
            lr = f.get("last_recalled") or 0
            base = lr if lr else (f.get("ts") or 0)
            return max(0.0, (now - base) / 86400)

        frag_lines = []
        for f in scored:
            if len(frag_lines) >= 3:
                break
            if frag_hit[f["id"]] <= 0:          # 跟话题零关联的不进
                continue
            ftype = f.get("type", "observation")
            if ftype != "preference":           # 永久事实永不过期
                df = _days_forgotten(f)
                if df >= 90:
                    continue                     # 出池：她"想不起来"了（数据保留）
                if df >= 30:
                    if ftype == "state":
                        mark = "（好久没提，不确定还算不算——用问句向他确认）"
                    else:
                        mark = "（久未提起）"
                else:
                    mark = ""
            else:
                mark = ""
            ts = f.get("ts") or 0
            t = time.localtime(ts)
            _d = int((time.time() - ts) // 86400)
            _rel = "今天" if _d <= 0 else ("昨天" if _d == 1 else f"{_d}天前")
            frag_lines.append(f"✧ #{f['id']} 档案记着：{f['text']}{mark}（{_rel}·{t.tm_mon}.{t.tm_mday}）")
            f["last_recalled"] = int(now)       # 保鲜：这轮被想起来了

        # ── 他的近况（state 碎片常驻注入，2026.9.3 修"呼市检索不到"）──
        # "他在哪/在忙什么"是他当下的状况——人不需要被提醒才想起这种事，
        # 所以 state 类不参与相关性门槛，每次都带（含已编入星座的：编进故事
        # 不该终结一个状态的时效）。遗忘规则照走：30天没提→问句确认。
        # 位置锚点优先：文本里"在+地名实体"的 state 钉一条在最前——
        # 问"我在哪"这种问题永远答得出，不受昨晚情绪碎片挤占。
        _alias_rev = {}
        for _a, _name in ENTITY_ALIAS.items():
            _alias_rev.setdefault(_name, []).append(_a)

        def _is_place_state(f):
            for name in (f.get("entities") or []):
                for w in [name] + _alias_rev.get(name, []):
                    if f"在{w}" in (f.get("text", "") or "")[:40]:
                        return True
            return False

        state_lines = []
        _seen_state = set()
        _seen_topic = set()
        _states = sorted((f for f in frags if f.get("type") == "state"),
                         key=lambda f: f.get("ts", 0), reverse=True)
        _place = next((f for f in _states if _is_place_state(f)), None)
        _picked = []
        if _place is not None:
            _picked.append(_place)

        def _push_state(f):
            if len(state_lines) >= 4 or f is None:
                return
            k = _norm(f.get("text", ""))[:30]
            if not k or k in _seen_state:
                return
            topic = (f.get("entities") or [""])[0] or ("#" + k[:12])
            if topic in _seen_topic:
                return   # 同一主题（首实体相同）只留最新一条，防昨晚情绪碎片占满名额
            df = _days_forgotten(f)
            if df >= 90:
                return
            mark2 = "（好久没提，不确定还算不算——用问句向他确认）" if df >= 30 else ""
            state_lines.append(f"▪ {f['text']}{mark2}")
            f["last_recalled"] = int(now)
            _seen_state.add(k)
            _seen_topic.add(topic)

        _push_state(_place)
        for f in _states:
            if f is _place:
                continue
            _push_state(f)
            if len(state_lines) >= 4:
                break

        def _nar_hits(n):
            return sum(max(0.0, frag_hit.get(fid, 0.0)) for fid in n.get("source_fragment_ids", []))

        def _nar_score(n):
            s = _nar_hits(n) + n.get("significance", 5) * 0.25
            lr = n.get("last_recalled") or 0
            base_ts = lr or _parse_dot_date(n.get("created", "")) or now
            days = max(0.0, (now - base_ts) / 86400)
            s *= 0.55 + 0.45 * math.exp(-days / 45)      # 长期没人提淡出（最多砍半）
            if days >= 180:
                s *= 0.5                                 # 遗忘TTL：半年没被想起，权重再减半（9.1规则）
            if lr and now - lr < 86400:
                s *= 0.5                                  # 刚想起过，这轮换别的想起
            return s

        nar_lines = []
        for n in sorted(nars, key=_nar_score, reverse=True)[:2]:
            lr = n.get("last_recalled") or 0
            base_ts = lr or _parse_dot_date(n.get("created", "")) or now
            if (now - base_ts) / 86400 >= 365:
                continue                                  # 归档：一年没被想起的星座不再注入（数据保留）
            if _nar_hits(n) < 0.5:        # 话题没实打实碰到它的碎片就不带出（防每条硬塞高分量星座）
                continue
            if _nar_score(n) < 1.0:
                continue
            tag = "✦" if n.get("significance", 5) >= 6 else "✧"
            nar_lines.append(f"{tag} #{n['id']} 档案·{n['title']}：{n['text']}")
            n["last_recalled"] = int(now)
        if not state_lines and not frag_lines and not nar_lines:
            return None
        if state_lines or nar_lines or frag_lines:
            _save_fragments_db(db)     # 保鲜：记下这轮想起了谁（近况+碎片+星座）
        return state_lines, frag_lines, nar_lines
    except Exception:
        return None


def scribe_running():
    """书记员是否在跑（跨模块读可变开关的穿透口，server 的状态端点用）。"""
    return SCRIBE_RUNNING


