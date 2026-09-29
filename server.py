"""SoulHome — 她的家。

M1：网页聊天 + 大脑热切换（任意 OpenAI 兼容 API）
M2：soul 入住（SOUL/USER/MEMORY 三文件装配成系统提示词）+ 记忆卡自动沉淀

大脑/耳朵/嘴巴是三个独立插槽；语音（M3）和识图（M4）接入时不动这里的结构。
"""
import asyncio
import base64
import hashlib
import json
import math
import os
import random
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from jiwen_engine import JiwenEngine, THRESHOLDS

# 2026.9.3 拆分（铁规矩P1）：common=底座（配置/路径/记账），memory=记忆系统全家
# 2026.9.10 拆分第二刀：library=书架全家，media=外围外观API（路由挂 router 由本文件 include）
# 2026.9.25f：schedule=日程闭环（待办账本，说话→她记→到点叫；与记忆三层分开）
import schedule as sched_mod
from common import (BASE_DIR, DATA_DIR, SOUL_DIR, IMAGES_DIR, MUSIC_DIR, BG_DIR,
                    LINKS_PATH, MOMENTS_PATH, LIKES_PATH, SCHEDULE_PATH, SIGNATURES_PATH,
                    SIGNATURE_COOLDOWN_S, DEFAULT_SIGNATURES,
                    SKILLS_DIR, SKILLS_MAX, SKILLS_INJECT_LIMIT, BOOKS_DIR, BOOKS_STATE_PATH,
                    READING_CHUNK, CONFIG_PATH, HISTORY_PATH, STATE_PATH, UPLOADS_DIR,
                    WORKSPACE_DIR,
                    PRESET_PROFILES, DEFAULT_CONFIG, DEFAULT_PROACTIVE, DEFAULT_MOMENTS,
                    MOMENT_COOLDOWN_S, MAX_CONTEXT_MESSAGES, MAX_IMAGES_PER_MESSAGE,
                    MEMORY_INJECT_CHAR_LIMIT,
                    TOKEN_LOG_PATH, _load_json, _save_json, _read_md, _tok,
                    _clamp_num, _today_key,
                    _active_profile, config, history)
from memory import (save_memory_line, rewrite_recall_query, memory_entries, memory_narratives,
                    sync_memory_index, recall_memories, _summary_experiment, summarize_memories,
                    MEMORY_FRAGMENTS_PATH, _load_fragments_db, _norm, scribe_running,
                    _parse_memory_ts, _infer_emotion_weight, _DATE_RE,
                    run_scribe, run_consolidation, scribe_recall,
                    apply_correction, _scribe_log,
                    edit_fragment, retire_fragment, restore_fragment, void_narrative,
                    restore_narrative, merge_narratives)
from library import (_books_state, _norm_book_state, _read_book_text, book_files,
                     do_reading, check_planned_reading, router as books_router)
from media import router as media_router
from documents import router as workspace_router, render_docx, render_pptx, extract_text


def list_skills() -> list:
    """data/skills/ 下的技能：[{"dir","title","text","meta"}]，新的在前（mtime 倒序）。
    title=技能卡首行"# 技能：xxx"里的短名；meta=use_count.json（使用计数，20260929b）。"""
    out = []
    try:
        for d in sorted(SKILLS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            f = d / "SKILL.md"
            if d.is_dir() and f.exists():
                text = f.read_text(encoding="utf-8", errors="ignore").strip()
                title = _skill_title(text) or d.name
                out.append({"dir": d, "title": title, "text": text, "meta": _skill_meta(d)})
    except Exception:
        pass
    return out


def _skill_title(text: str) -> str:
    """技能卡首行'# 技能：短名'里剥出短名；没有就空串。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip().replace("技能：", "").strip()
    return ""


def _sanitize_skill_name(title: str) -> str:
    r"""技能短名 → 安全目录名（Windows 禁 \ / : * ? " < > |，空白折叠）。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "", title or "")
    return (name or "未命名")[:30]


def _skill_meta(d) -> dict:
    """技能使用计数：count/last_used/born。born 缺省=SKILL.md 的 mtime（迁移存量用）。"""
    f = d / "use_count.json"
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(m, dict) and "count" in m:
            m.setdefault("born", (d / "SKILL.md").stat().st_mtime)
            return m
    except Exception:
        pass
    try:
        born = (d / "SKILL.md").stat().st_mtime
    except Exception:
        born = time.time()
    return {"count": 0, "last_used": None, "born": born}


def _bump_skill(d) -> None:
    """技能被 get_skill 调用了一次：计数+1（只做标记，不影响注入与淘汰权重之外的东西）。"""
    try:
        m = _skill_meta(d)
        m["count"] = int(m.get("count") or 0) + 1
        m["last_used"] = time.time()
        (d / "use_count.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def skills_prompt_section() -> str:
    """技能名字清单（20260929b 改版）：全部技能只注入名字一行，全文要看得调 get_skill 工具。
    名单变化频率极低（技能增删/滚动才变），放静态段尾部不伤前缀缓存；总预算照旧 700 字。"""
    skills = list_skills()
    if not skills:
        return ""
    lines, used = [], 0
    for sk in skills:
        t = f"- {sk['title']}"
        if used + len(t) > SKILLS_INJECT_LIMIT:
            break
        lines.append(t)
        used += len(t) + 1
    if not lines:
        return ""
    return ("【她自己沉淀的技能】（她自己攒的办事套路，上面只列了名字——"
            "哪个对得上眼前的事，就调 get_skill 工具把名字传进去看整张技能卡；不合适就按现场变通）\n"
            + "\n".join(lines))


def build_system_prompt(query: str = "", tone_mode: str = "reactive") -> str:
    """把她的世界装进一条系统提示词。

    原则是"她的感知"而不是"数据包+使用说明"：她留意到、她了解到、她想起——
    不写"要自然地怎样怎样"这类演技指导，模型一表演就不像人了。
    query 是联想改写后的检索问句（不是原话），用于翻她自己的记忆。
    tone_mode: reactive=回他消息（默认）/ proactive=她主动开口——语调网格叠加层用。

    2026.9.23 缓存重排（刀1）：DeepSeek 前缀缓存按"从第一个字起相同"命中，此前心情
    底色等动态段卡在 SOUL 之后，稳定前缀只有 2.8K。现在拆成两截——静态段（SOUL/他的
    档案/技能/补充设定/日程/在读/系统通道说明，基本不动）排前面吃缓存，动态段（心情
    底色/记忆检索/书记员档案/歌/状态卡/时间/想念/心情，每条都可能变）全挪尾部。内容
    一字不改只搬家；动态段收尾顺带吃"近因"红利。刀2（动态段改塞最后一条 user 消息
    尾部、让 50 条聊天历史也进缓存）等这版口吻+账单验收后再上。
    """
    static_sections, dyn_sections = [], []
    soul, user, mem = _read_md("SOUL.md"), _read_md("USER.md"), _read_md("MEMORY.md")
    if soul:
        static_sections.append(soul)
    if user:
        static_sections.append("【你认识的这个人】\n" + user)
    sk = skills_prompt_section()
    if sk:
        static_sections.append(sk)
    extra = (config.get("system_prompt") or "").strip()
    if extra:
        static_sections.append("【补充设定】\n" + extra)
    # 他随口跟她讲过的事（日程）
    schedule = SCHEDULE_PATH.read_text(encoding="utf-8").strip() if SCHEDULE_PATH.exists() else ""
    if schedule:
        static_sections.append("【他跟你提过的安排】\n" + schedule[:1500])
    # 她最近在读的书（聊天可以自然聊起）
    try:
        stb = _books_state()
        cur_book = next((n for n in book_files() if not (stb.get(n) or {}).get("finished")), None)
        if cur_book:
            s_b = _norm_book_state(stb.get(cur_book) or {})
            her_notes = [n for n in s_b["notes"] if n.get("who") == "her"]
            line = f"【你最近在读】《{cur_book[:-4]}》（读到 {round(int(s_b['read_chars']) / max(len(_read_book_text(cur_book)), 1) * 100)}%）"
            if her_notes:
                line += f"——你上次记过：{her_notes[-1]['text'][:40]}"
            static_sections.append(line)
    except Exception:
        pass
    # 系统通道：只留两条且只在需要时用——她想亲手记的事、明显变化的心情
    # （2026.9.23 从末尾挪进静态块：这段一字不变，放前面一起吃缓存）
    static_sections.append(
        "（系统通道，他看不到：有想留存的事——他的新偏好、你们的约定和重要时刻、你对他的新理解——"
        "就在回复最后单独一行写（记：一句话），整行保持带括号的这个格式，别省掉括号；"
        "括号要用全角的（），不要用半角的()。"
        "他要你记着到点提醒他的事（\"明天下午两点提醒我……\"这类），改用另一条通道：回复最后单独一行写"
        "（日程：时间 事项），时间写具体（今天/明天/X月X号/星期几/几点/X分钟后，日期和钟点连着写清楚）；"
        "他说的日期已经过了、或者没说清楚，先问一句确认再记，绝不自己猜；记下后自然地应一句\"好，X点叫你\"。"
        "日程是待办不是回忆，这类事别写进（记：）。"
        "你的心情只有跟刚才相比明显变了，才再写一行（心情：词，一句原因），"
        "词从 开心/平静/想念/委屈/生气/难过/好奇/困/兴奋 里挑一个。没有就都不写，别硬凑。"
        "你的内心权衡——要不要写（记：）、该怎么回、该怎么措辞这类思量过程——永远不写进正文；"
        "要记就直接写（记：…），不要把你考虑记不记的过程说出来。"
        "直接说话就好，不要用*动作*或（动作）这类旁白，也不要用**加粗**、#号标题这类排版符号——"
        "你在聊天不是写文档，重点直接说出来。"
        "另外，被他说\"你说错了/你编的\"的时候，先核对出处再开口：内容若来自搜索结果或记忆，就告诉他出处；"
        "确实错了才认错——不为顺从而认没犯过的错。）"
    )

    # ---------- 动态段（每条消息都可能变，全放尾部） ----------
    # 语调网格（她此刻的说话方式；data/tone_grid.json 用户亲写，空则整段不出现）
    try:
        import tone_grid
        _tg = tone_grid.get_style_guidance(_jiwen().state, tone_mode)
    except Exception:
        _tg = ""
    if _tg:
        dyn_sections.append("【你此刻的心情底色】\n" + _tg)
    recalled = recall_memories(query) if (mem and query) else None
    if recalled:
        core, hits = recalled
        if core:
            dyn_sections.append("【你一直记得的事】\n" + "\n§\n".join(core))
        if hits:
            # 带召回权限标签：可直接引用/需谨慎/仅联想
            hit_lines = []
            for text, perm in hits:
                prefix = {"可直接引用": "✦", "需谨慎": "✧", "仅联想": "◦"}.get(perm, "·")
                hit_lines.append(f"{prefix} {text}（{perm}）")
            dyn_sections.append("【此刻想起的相关记忆】\n" + "\n".join(hit_lines))
        dyn_sections.append("（记忆权限：✦可直接引用 / ✧档案记录引用时说'档案里记着' / ◦仅联想不准说出口 / ?存疑仅供参考不准当事实）")
    elif mem:
        # 检索不可用（记忆还少 / 嵌入模型未就绪）：回退到全量注入并封顶
        if len(mem) > MEMORY_INJECT_CHAR_LIMIT:
            mem = "（更早的记忆暂时折叠）\n" + mem[-MEMORY_INJECT_CHAR_LIMIT:]
        dyn_sections.append("【你记得的事】\n" + mem)
    # 书记员的账本联想（长期记忆）：命中碎片≤5 + 叙事星座≤2（2026.9.2 v2 T2）
    # 上面 RAG 翻的是 MEMORY.md（她的短期记忆/手记）；这里翻的是账本（碎片+叙事星座）。
    # 静默兜底：账本损坏/为空时这一段直接不出现，聊天零感知。
    sr = scribe_recall(query)
    if sr:
        state_lines, frag_lines, nar_lines = sr
        # 档案口吻（防夺舍）：书记员的旁观记录不包装成她的亲历记忆——
        # "✧ 我记得"会教她说"我记得你说过"却没依据，改成"档案里记着"的旁观口吻
        _lines = ["（书记员抄的档案——用“档案里记着”的口吻提，别当亲历）"]
        if state_lines:
            _lines.append("——他最近的近况——")
            _lines.extend(state_lines)
        if nar_lines:
            _lines.append("——档案里的完整故事（星座）——")
            _lines.extend(nar_lines)
        if frag_lines:
            _lines.append("——档案里零散记的事——")
            _lines.extend(frag_lines)
        dyn_sections.append("【书记员的档案】\n" + "\n".join(_lines))
    st = _load_json(STATE_PATH, {})
    # 她留意到的事（在听的歌、点赞的歌）
    np = st.get("now_playing") or {}
    if np.get("name") and time.time() - np.get("ts", 0) < 3 * 3600:
        dyn_sections.append(f"【你留意到】他那边正放着《{np['name']}》")
    likes = _load_json(LIKES_PATH, [])[-5:]
    if likes:
        dyn_sections.append("【你留意到】他最近点赞过 " + "、".join(f"《{l}》" for l in likes))
    # 他主动告诉她的小状态（⚡卡）
    myst = st.get("my_status") or {}
    if myst.get("mood") or myst.get("doing") or myst.get("note"):
        if myst.get("ts"):
            h = int((time.time() - myst["ts"]) / 3600)
            age = "刚刚" if h < 1 else f"{h}小时前"
        else:
            age = "最近"
        parts = []
        if myst.get("mood"):
            parts.append(f"心情是「{myst['mood']}」")
        if myst.get("doing"):
            parts.append(f"在忙「{myst['doing']}」")
        if myst.get("note"):
            parts.append(f"想对你说「{myst['note']}」")
        dyn_sections.append(f"【你了解到】他{age}更新过自己的状态，说：" + "，".join(parts))

    # 大模型没有内置时钟：每次请求注入当前时间，她才能回答几点/星期/日期
    now = time.localtime()
    weekday = "一二三四五六日"[now.tm_wday]
    dyn_sections.append(f"【现在】{now.tm_year}年{now.tm_mon}月{now.tm_mday}日 星期{weekday} {now.tm_hour:02d}:{now.tm_min:02d}")
    # 距上次说话的时长（>6 小时才出现）：让他重开机第一句就接得上"你消失了多久"，
    # 而不是含糊的热情。纯事实注入，不带演技指导（2026.9.10）
    _last_u_ts = next((m.get("ts") for m in reversed(history[-80:]) if m.get("role") == "user"), 0)
    if _last_u_ts and time.time() - _last_u_ts > 6 * 3600:
        _gap = time.time() - _last_u_ts
        _gd, _gh = int(_gap // 86400), int(_gap % 86400 // 3600)
        _gap_str = f"{_gd} 天 {_gh} 小时" if _gd else f"{_gh} 小时"
        dyn_sections.append(f"【时间】距你们上次说话已过去 {_gap_str}")
    # 她攒着的想念（2026.9.11 抢先兜底）：connection 到考虑开口档才出现——长时间
    # 没说话、或服务器刚开机补算完，他抢先说了话（想念还没来得及主动开口就被回应），
    # 这段让第一句回复自然带着攒了许久的想念。纯事实注入不带演技指导；自限：聊天
    # 一结束想念归零，这段自动消失，不会每句都缠着想念。proactive 路径已在消息里
    # 单独注入完整内心状态，这里不重复。
    if tone_mode == "reactive":
        try:
            _c = _jiwen().state.get("connection") or 0
            if _c >= THRESHOLDS["considerContact"]:
                _first = (_jiwen().get_prompt_context().splitlines() or [""])[0]
                if _first:
                    dyn_sections.append(f"【你心里攒着的】{_first}")
        except Exception:
            pass
        # 日程闭环的嘴（2026.9.25f）：早嘴=当天第一次开口顺口提一句今天/明天要办的事；
        # 兜底嘴=到点叫过没回应/关机错过，他回来了带一嘴（说完即销）；晚间好奇=销账后
        # 关心一句办完没。全是事实注入+行为边界，不写台词。
        try:
            _lt = time.localtime()
            _today0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
            _first_today = sum(1 for m in history[-200:]
                               if m.get("role") == "user" and m.get("ts", 0) >= _today0) == 1
            inj = sched_mod.pending_injections(time.time(), _first_today)
            _ml = [f"{it['text']}（{sched_mod.humanize_due(it['due_ts'])}）" for it in inj["morning"]]
            if _ml:
                dyn_sections.append("【你惦记的日程】\n" + "\n".join(_ml)
                                    + "\n（今天或明天的事，回复里顺口提一句就好，别反复叮嘱。）")
            _nl = [f"{it['text']}（{sched_mod.humanize_due(it['due_ts'])}到点，叫过他没回应）" for it in inj["mention"]]
            if _nl:
                dyn_sections.append("【你惦记的日程】\n" + "\n".join(_nl)
                                    + "\n（这次回复里自然带一嘴，像\"对了，……\"；提完这页翻篇，别追问。）")
            if inj["followup"]:
                _fu = inj["followup"][0]
                dyn_sections.append(f"【你惦记的日程】「{_fu['text']}」他回来销了账——"
                                    "可以好奇问一句办完了没，关心不催；问这一次就不再提。")
        except Exception:
            pass
    # 她此刻的情绪状态（带原因，从上一条延续——情绪要连贯，别凭空跳变）
    m_st = _load_json(STATE_PATH, {}).get("mood") or {}
    if m_st.get("label"):
        age = ""
        if m_st.get("ts"):
            h = int((time.time() - m_st["ts"]) / 3600)
            age = "（刚刚）" if h < 1 else f"（{h} 小时前开始）"
        why = f"，因为{m_st['reason']}" if m_st.get("reason") else ""
        dyn_sections.append(f"【你的心情】{m_st['label']}{why}{age}——延续这个情绪说话，除非对话让它变了")
    return "\n\n".join(static_sections + dyn_sections)


# 2026.9.19 第三次复发实测：她写半角(记：...)照样漏剥泄进聊天——括号全/半角都认
MOOD_RE = re.compile(r"[（(]心情[:：]\s*([^）)]{1,40})[）)]")
MEM_RE = re.compile(r"[（(]记[:：]\s*([^）)]{1,120})[）)]")
# 日程通道（2026.9.25f）：她写（日程：时间 事项）——同样的全/半角+裸行+未闭合三件套，
# 9.19 三次复发的教训直接抄作业，别再漏第四次
SCHED_RE = re.compile(r"[（(]日程[:：]\s*([^）)]{1,160})[）)]")
SCHED_LINE_RE = re.compile(r"(?:^|\n)\s*日程[:：]\s*([^\n]{1,160})\s*$")
# 未闭合：不锚行首——"好，两点叫你（日程：明天…"这种写在句尾断掉的也要接住
SCHED_OPEN_RE = re.compile(r"[（(]日程[:：]\s*([^\n]{1,160})$")
# 她偶尔忘写全角括号（2026.9.2实测）：末尾单独成行的裸"记：xxx/心情：xxx"也认，防漏存+防泄进聊天
MEM_LINE_RE = re.compile(r"(?:^|\n)\s*记[:：]\s*([^\n]{1,120})\s*$")
MOOD_LINE_RE = re.compile(r"(?:^|\n)\s*心情[:：]\s*([^\n]{1,40})\s*$")
# 括号写了却忘了闭合的（写到末尾断掉）：整行当手记收走，防漏存
MEM_OPEN_RE = re.compile(r"(?:^|\n)\s*[（(]记[:：]\s*([^\n]{1,120})$")
MOOD_OPEN_RE = re.compile(r"(?:^|\n)\s*[（(]心情[:：]\s*([^\n]{1,40})$")
# 角色扮演时代的遗产：*笑着抱住你* / （笑） 这类动作旁白。用户不要了，剥掉。
ACT_STAR_RE = re.compile(r"[*＊][^*\n]{1,200}[*＊]")
# 已知动作词（任意位置）+ 兜底规则：出现在行首或句末标点之后的"纯中文短括号"视为旁白
ACT_PAREN_RE = re.compile(
    r"（(?:笑|微笑|大笑|轻笑|苦笑|偷笑|笑了笑|叹气|叹了口气|叹口气|摇头|点头|歪头|歪了歪头|眨眼|眨了眨眼|"
    r"撇嘴|嘟嘴|嘟着嘴|吐舌头|吐了吐舌头|脸红|害羞|委屈|委屈巴巴|撒娇|抱抱|拥抱|亲亲|托腮|撑着下巴|"
    r"打哈欠|伸了个懒腰|伸懒腰|翻白眼|哽咽|吸了吸鼻子)[了吧呀啊的呢~！!。]{0,3}）"
)
ACT_BOUND_PAREN_RE = re.compile(r"(^|(?<=[。！？!?…\n]))（[一-龥]{1,6}）", re.M)


def strip_actions(text: str) -> str:
    text = ACT_PAREN_RE.sub("", ACT_STAR_RE.sub("", text))
    return ACT_BOUND_PAREN_RE.sub(r"\1", text)


def extract_mood(reply: str):
    """从回复里剥出（心情：词[,原因]）标记，返回 (干净回复, {label,reason} 或 None)。"""
    m = MOOD_RE.search(reply)
    if not m:
        return reply, None
    raw = m.group(1).strip()
    for sep in ("，", ",", "｜", "|", "——", "—"):
        if sep in raw:
            label, reason = raw.split(sep, 1)
            return MOOD_RE.sub("", reply).strip(), {"label": label.strip()[:6], "reason": reason.strip()[:30]}
    return MOOD_RE.sub("", reply).strip(), {"label": raw[:6], "reason": ""}


def _parse_mood(raw: str):
    for sep in ("，", ",", "｜", "|", "——", "—"):
        if sep in raw:
            return {"label": raw.split(sep, 1)[0].strip()[:6], "reason": raw.split(sep, 1)[1].strip()[:30]}
    return {"label": raw[:6], "reason": ""}


def extract_marks(reply: str):
    """剥出她留的全部标记和动作旁白。返回 (干净回复, 心情或None, 她想记的事列表, 日程列表)。
    兼容她忘写全角括号的情况：裸行"记：xxx"、半角括号、忘了闭合的都认。
    日程（2026.9.25f）与（记：）同款待遇：全/半角+裸行+未闭合，内容是"时间 事项"。"""
    scheds = [m.strip() for m in SCHED_RE.findall(reply) if m.strip()]
    mems = [m.strip() for m in MEM_RE.findall(reply) if m.strip()]
    clean = strip_actions(MEM_RE.sub("", SCHED_RE.sub("", reply)))
    m = SCHED_LINE_RE.search(clean)
    if m and m.group(1).strip():
        scheds.append(m.group(1).strip()[:160])
        clean = clean[:m.start()].rstrip()
    m = SCHED_OPEN_RE.search(clean)
    if m and m.group(1).strip():
        scheds.append(m.group(1).strip()[:160])
        clean = clean[:m.start()].rstrip()
    m = MEM_LINE_RE.search(clean)
    if m and m.group(1).strip():
        mems.append(m.group(1).strip()[:120])
        clean = clean[:m.start()].rstrip()
    m = MEM_OPEN_RE.search(clean)
    if m and m.group(1).strip():
        mems.append(m.group(1).strip()[:120])
        clean = clean[:m.start()].rstrip()
    clean, mood = extract_mood(clean)
    ml = MOOD_LINE_RE.search(clean)
    if ml and not mood and ml.group(1).strip():
        mood = _parse_mood(ml.group(1).strip())
        clean = clean[:ml.start()].rstrip()
    mo = MOOD_OPEN_RE.search(clean)
    if mo and not mood and mo.group(1).strip():
        mood = _parse_mood(mo.group(1).strip())
        clean = clean[:mo.start()].rstrip()
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, mood, mems, scheds


# 思考泄漏静默观测器（20260929a，用户定性为 bug）：她把"要不要写（记：）"这类内心权衡
# 直接写进正文。提示词禁令已加（系统通道段），这里是第三层——只记日志攒证据不重生成，
# 误判风险高的模式一律不收；一个月内还在漏再上"检测+重生成"。
_LEAK_PATTERNS = ("要不要写（记", "要不要写(记", "要不要记", "要不要存", "我该不该写", "我是不是该把")


def note_thought_leak(clean_text: str, source: str) -> None:
    try:
        if not clean_text:
            return
        if any(p in clean_text for p in _LEAK_PATTERNS):
            line = json.dumps({"ts": time.time(), "source": source,
                               "excerpt": clean_text[:160]}, ensure_ascii=False)
            (DATA_DIR / "thought_leak.log").open("a", encoding="utf-8").write(line + "\n")
            print(f"[思考泄漏] 疑似命中（{source}），已记日志：{clean_text[:60]}", flush=True)
    except Exception:
        pass


def save_mood(mood) -> None:
    st = _load_json(STATE_PATH, {})
    if isinstance(mood, dict):
        st["mood"] = {"label": mood.get("label", ""), "reason": mood.get("reason", ""), "ts": time.time()}
    else:
        st["mood"] = {"label": str(mood)[:6], "reason": "", "ts": time.time()}
    _save_json(STATE_PATH, st)


app = FastAPI(title="港口")


# ---------- 门锁：识别码 + 通行证 ----------
# 挂到自己电脑上给手机连的场景：输对识别码 → 发一张通行证 cookie（记住这台设备），
# 输错直接拒。识别码留空 = 不设防（老行为）。改码在 设置→其他。

COOKIE_NAME = "soulhome_token"


def _locked() -> bool:
    return bool((config.get("access_code") or "").strip())


def _tokens() -> list:
    return (_load_json(STATE_PATH, {}).get("access_tokens")) or []


def _add_token(tok: str) -> None:
    st = _load_json(STATE_PATH, {})
    toks = st.get("access_tokens") or []
    toks.append(tok)
    st["access_tokens"] = toks[-10:]  # 最多记 10 台设备
    _save_json(STATE_PATH, st)


def _authed(request: Request) -> bool:
    if not _locked():
        return True
    tok = request.cookies.get(COOKIE_NAME) or ""
    return bool(tok) and tok in _tokens()


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>港口 · 门锁</title>
<style>
:root{--paper:#efe9d8;--card:#fbf8ee;--ink:#211c14;--muted:#6b6250;--line:#211c14;--gold:#b8963f;--gold-soft:#d9c48c;}
*{margin:0;padding:0;box-sizing:border-box}
body{height:100vh;display:flex;align-items:center;justify-content:center;background:var(--paper);
  font-family:"Microsoft YaHei",system-ui,sans-serif;color:var(--ink);
  background-image:radial-gradient(ellipse at 20% 10%,rgba(255,255,255,.5),transparent 55%)}
.card{width:min(340px,88vw);background:var(--card);border:1.5px solid var(--line);border-radius:12px;
  padding:28px 26px;box-shadow:4px 4px 0 rgba(33,28,20,.18);text-align:center}
.dia{font-size:30px;color:var(--gold)}
h1{font-size:18px;letter-spacing:4px;margin:12px 0 4px;font-family:Georgia,"SimSun",serif}
.sub{font-size:12px;color:var(--muted);margin-bottom:20px}
input{width:100%;border:1.5px solid var(--line);border-radius:9px;background:var(--paper);
  padding:11px 12px;font-size:16px;text-align:center;letter-spacing:6px;outline:none;color:var(--ink)}
input:focus{border-color:var(--gold)}
button{width:100%;margin-top:12px;border:1.5px solid var(--line);background:var(--ink);color:var(--gold-soft);
  border-radius:9px;padding:11px;font-size:14.5px;cursor:pointer;box-shadow:2px 2px 0 var(--gold)}
.err{color:#9c3535;font-size:12px;margin-top:10px;min-height:16px}
</style></head><body>
<div class="card"><div class="dia">◆</div><h1>港口</h1>
<div class="sub">欢迎回家 · 请输入识别码</div>
<input id="code" type="password" placeholder="· · · ·" maxlength="32" autofocus>
<button onclick="go()">进 门</button><div class="err" id="err"></div></div>
<script>
async function go(){
  const c=document.getElementById('code').value.trim();
  if(!c)return;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c})});
  if(r.ok){location.reload()}else{document.getElementById('err').textContent='识别码不对';document.getElementById('code').select()}
}
document.getElementById('code').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script></body></html>"""


@app.middleware("http")
async def access_gate(request: Request, call_next):
    """识别码设了之后：没带通行证的一律拦在门外（除了登录接口和书记员接口本身）。"""
    if _locked():
        p = request.url.path
        # 书记员接口免登录（dispatch必须返回response，return None会500）
        if p in ("/api/scribe/run", "/api/scribe/status", "/api/scribe/consolidate", "/api/tokens"):
            return await call_next(request)
        # PWA 资源免登录（2026.9.21 三件套）：没输码也能"添加到主屏幕"，装完打开再登录
        if p.startswith("/static/pwa/"):
            return await call_next(request)
        if p != "/api/login" and not _authed(request):
            if p.startswith("/api/"):
                return JSONResponse({"error": "locked"}, status_code=401)
            login = HTMLResponse(LOGIN_HTML)
            login.headers["Cache-Control"] = "no-cache"
            return login
    resp = await call_next(request)
    # 主文档禁强缓存（2026.9.25j 修：exe 的 WebView2 没法 Ctrl+F5，index.html 无 Cache-Control
    # 时按启发式缓存直接吃旧页面——刀1 在 exe 里"完全没效果"的真凶。no-cache=每次带 ETag 复验，
    # 内容没变就 304，代价为零；带 ?v= 的 static 资源不受影响）
    ct = resp.headers.get("content-type", "")
    if ct.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# 外围模块路由（2026.9.10 拆分第二刀）：library=书架，media=外观/音乐/动态等。
# 中间件在前，include 在后——门锁对 router 路由同样生效。
app.include_router(books_router)
app.include_router(media_router)
app.include_router(workspace_router)   # 她做的文件下载（2026.9.19 文档工坊）
# 静态资源（2026.9.10）：app.js / star-map.js / fonts。此前 /fonts/* 无路由，
# 霞鹜文楷 CSS 被门锁当页面拦回（text/html），字体自 9.9 上线起从未真正加载过。
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    code = (body.get("code") or "").strip()
    if not _locked() or code != (config.get("access_code") or "").strip():
        return JSONResponse({"error": "识别码不对"}, status_code=401)
    tok = secrets.token_hex(16)
    _add_token(tok)
    resp = JSONResponse({"ok": True})
    if config.get("lock_every_launch"):
        # 会话票（2026.9.23）：勾了"每次启动都输码"——票不带 max_age，随浏览器会话活，
        # 关掉 App/窗口再开就要重新输码；不勾 = 记住本机 365 天（原行为）
        resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax")
    else:
        resp.set_cookie(COOKIE_NAME, tok, max_age=365 * 86400, httponly=True, samesite="lax")
    return resp


# 预搜索只认【明确说出口】的搜索指令（搜/查…）；隐式意图（"出了吗/多少钱/谁是谁"）
# 交给她的 web_search 工具自己判断——模型理解意图，比关键词表聪明（2026.9.2 用户拍板）
SEARCH_HINTS = ("搜", "搜索", "search", "查一", "查查", "查个", "查最新", "热搜")


def wants_search(text: str) -> bool:
    t = (text or "").lower()
    if len(t) >= 200:
        return False
    # 误触发排除（2026.9.2实测踩坑）："检查一下/复查一下"是日常口语不是搜索意图，
    # 曾把整句大白话扔给搜索引擎，返回一堆医疗网页污染她的上下文
    t = re.sub(r"[检复审巡]查一?下?", "", t)
    return any(h in t for h in SEARCH_HINTS)


async def do_search(sc: dict, query: str):
    """调搜索服务，返回 (结果列表, 是否成功)。支持博查（国内）和 Tavily（国际）。"""
    provider, key, q = sc.get("provider", "bocha"), sc.get("api_key", ""), query[:100]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if provider == "tavily":
                r = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"query": q, "max_results": 5},
                )
                if r.status_code != 200:
                    return [], False
                data = r.json().get("results", [])
                return [{"title": x.get("title", ""), "snippet": x.get("content", "")[:300], "url": x.get("url", "")} for x in data], True
            else:  # bocha
                r = await client.post(
                    "https://api.bochaai.com/v1/web-search",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"query": q, "count": 5},
                )
                if r.status_code != 200:
                    return [], False
                pages = r.json().get("data", {}).get("webPages", {}).get("value", [])
                return [{"title": x.get("name", ""), "snippet": (x.get("summary") or x.get("snippet") or "")[:300], "url": x.get("url", "")} for x in pages], True
    except Exception:
        return [], False


def format_search_context(query: str, results: list, ok: bool) -> str:
    if not ok:
        return f"【联网搜索】刚才尝试搜索“{query[:50]}”但搜索服务出错了，如实地告诉他没搜成。"
    if not results:
        return f"【联网搜索】搜了“{query[:50]}”，但没有找到有用的结果，可以直说没搜到。"
    q = (query or "").strip()
    joined = "".join((x.get("title", "") + x.get("snippet", "")) for x in results)
    qg = {q[i:i + 2] for i in range(max(0, len(q) - 1))}
    overlap = sum(1 for g in qg if g in joined)
    # 误触发打标：整句话不像搜索词，或结果跟原话几乎不沾边 → 让她别当真别复述
    if len(q) > 18 or (len(qg) >= 4 and overlap < 2):
        hint = "（系统提示：这轮搜索像是误触发的，下面的结果如果跟你们正在聊的话题无关，就当没看见，不要向他复述或点评）"
    else:
        hint = "（回答时可以用这些信息，自然地提来源）"
    lines = [f"【联网搜索结果 · 关于“{query[:50]}”】" + hint]
    for i, x in enumerate(results, 1):
        lines.append(f"{i}. {x['title']}\n   {x['snippet']}\n   来源: {x['url']}")
    return "\n".join(lines)


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------- 她的工具（agent 循环）：白名单三件，聊着天顺手就把活干了 ----------
# 原则：聊天优先于干活——最多 3 轮工具，查完立刻回到她的语气回答，不变成"执行器"。

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索。凡是需要你不知道的外部或最新信息才能答的，就用它——"
                           "他问'XX出了吗/什么时候发布/现在多少钱/谁是谁/最近有什么新动静/攻略怎么做'这类问题，"
                           "哪怕他没说'搜'这个字也该查。宁可查，也别凭记忆猜着答。"
                           "纯聊天、聊你们俩的事、你本来就知道的事，不要用。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词，精炼一点"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查某个城市现在和今天的天气。他问天气、要出门、纠结穿什么的时候用。",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string", "description": "城市名，如：上海。不确定就留空（按他所在的网络位置查）"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 data 目录里的一个文件。他发给你的文件存在 files/ 下（txt/docx/pdf 都能读，"
                           "聊天里他发文件时系统会告诉你路径，如 files/报告.docx）；"
                           "你的长期记忆账本是 memory_fragments.json（碎片+叙事星座都在里面，想知道你记得他什么就翻它）；"
                           "其他如 books/ 里的书、schedule.md 他的日程。路径相对 data/。"
                           "一次读 2 万字，更长的文件读到截断处会告诉你\"把 offset 设为多少继续\"，照着再调一次就读下一段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如：books/小王子.txt 或 schedule.md"},
                    "offset": {"type": "integer", "description": "从第几个字开始读（长文件分次读用；第一次读不用填）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_together",
            "description": "他邀请你一起读书、或说要去读书的时候用：当场翻开你们正在读的书读一段，写下你的感想。读完把结果自然地讲给他听（他在书里写的批注你也看得见）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct_memory",
            "description": "记忆纠错：他指出你记错了/说错了的时候用（'不对'、'不是这样'、'我说错了'、'其实是XX'这种时刻）。"
                           "wrong_statement 填你刚才说错的那个内容，correction 填他给的正确版本。"
                           "系统会自动找到错的那条记忆改掉；要是聊天里看到的记忆带着 #编号（如 #f_000123），"
                           "把编号抄进 id 参数能改得更准。改完自然地接他的话就行，不用汇报过程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "wrong_statement": {"type": "string", "description": "你记错/说错的内容"},
                    "correction": {"type": "string", "description": "他说的正确版本"},
                    "id": {"type": "string", "description": "可选：聊天里看到的记忆编号，如 f_000123"},
                },
                "required": ["wrong_statement", "correction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "给他做一份 Word 文档（.docx）。他让你'写个word/写文档/写份报告/整理成文档'，"
                           "或提出字体字号首行缩进这类格式要求时用。你就是作者：全文内容由你写完整写好，"
                           "别写占位符别偷懒。格式要求放进 styles（他没提就用默认：宋体、小四、首行缩进2字符、1.5倍行距）。"
                           "一次做一份，改格式或续写就带着新要求重新生成一份。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（不用带 .docx 后缀）。可选，默认用标题"},
                    "title": {"type": "string", "description": "文档大标题，显示在开头居中"},
                    "styles": {
                        "type": "object",
                        "description": "格式要求（都可省略）",
                        "properties": {
                            "font": {"type": "string", "description": "正文中文字体，如：宋体/黑体/仿宋/楷体/微软雅黑"},
                            "size_pt": {"type": "number", "description": "正文字号（磅）。小四=12，四号=14，五号=10.5"},
                            "first_line_indent_chars": {"type": "number", "description": "首行缩进几个字符，通常 2"},
                            "line_spacing": {"type": "number", "description": "行距倍数，如 1.5"},
                            "align": {"type": "string", "description": "对齐：left/center/right/justify（默认 justify 两端对齐）"},
                        },
                    },
                    "blocks": {
                        "type": "array",
                        "description": "正文内容，按顺序每块一个对象："
                                       '{"type":"heading","text":"一级标题","level":1~3}（1 居中加粗）'
                                       '{"type":"paragraph","text":"正文段落"}'
                                       '{"type":"list","items":["要点1","要点2"],"ordered":false}'
                                       '{"type":"quote","text":"引用/备注，灰色缩进"}',
                    },
                },
                "required": ["title", "blocks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_slides",
            "description": "给他做一份 PPT（.pptx，16:9）。他让你'做个PPT/做幻灯片/做个演示'时用。"
                           "你就是作者：每页的标题和内容都由你写好，别写占位符。每页信息别塞太满（要点式，每页 3~6 条为宜）。"
                           "第一页用 cover（大标题+副标题），中间 bullets/text/section，最后一页用 end。"
                           "他没提风格就用默认素白（正式场合/课堂最稳）；米黄纸面、墨夜是你们家的风格，他点名要就用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（不用带 .pptx 后缀）。可选，默认用标题"},
                    "title": {"type": "string", "description": "整份演示的标题"},
                    "styles": {
                        "type": "object",
                        "description": "风格（都可省略）",
                        "properties": {
                            "font": {"type": "string", "description": "字体，如：微软雅黑/黑体"},
                            "title_size_pt": {"type": "number", "description": "页标题字号，默认 30"},
                            "body_size_pt": {"type": "number", "description": "正文字号，默认 17"},
                            "theme": {"type": "string", "description": "配色：paper 米黄纸面（默认）/ dark 墨夜 / clean 素白"},
                        },
                    },
                    "slides": {
                        "type": "array",
                        "description": "每页一个对象："
                                       '{"type":"cover","title":"大标题","subtitle":"副标题/汇报人"}'
                                       '{"type":"bullets","title":"页标题","items":["要点1","要点2"]}'
                                       '{"type":"text","title":"页标题","body":"整段文字（可换行）"}'
                                       '{"type":"section","title":"章节过渡页大字"}'
                                       '{"type":"end","title":"谢谢观看"}',
                    },
                },
                "required": ["title", "slides"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": "翻出你自己攒的一张技能卡看全文。你沉淀过的办事套路名字都在【她自己沉淀的技能】清单里"
                           "（系统提示词里那份名单）——要办的事对得上哪个名字，就把名字传进来，"
                           "能看到那张卡的触发条件和做法步骤。名字要跟清单里写的一致。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名，照【她自己沉淀的技能】清单里抄"},
                },
                "required": ["name"],
            },
        },
    },
]

TOOL_LABELS = {"web_search": "联网查了查", "get_weather": "看了眼天气", "read_file": "翻了下文件",
               "read_together": "去翻了会儿书", "correct_memory": "默默改了改记错的事",
               "create_document": "在给你写文档", "create_slides": "在给你做幻灯片",
               "get_skill": "翻了翻自己的小抄"}


async def run_tool(name: str, args_raw: str, files_out: list = None) -> str:
    """执行白名单里的一个工具，返回给模型看的文本结果。出任何问题都返回可读的原因。
    files_out：文档类工具把生成的文件元信息 {name,kind,size} 追加进去，聊天循环拿去发卡片。"""
    from urllib.parse import quote

    try:
        args = json.loads(args_raw or "{}")
    except Exception:
        args = {}
    try:
        if name == "web_search":
            sc = config.get("search") or {}
            q = (args.get("query") or "").strip()
            if not q:
                return "搜索词是空的"
            if not sc.get("api_key"):
                return "搜索还没配 Key，暂时搜不了（可以如实告诉他）"
            results, ok = await do_search(sc, q)
            if not ok:
                return "搜索服务出错了"
            if not results:
                return "没有搜到有用的结果"
            return "\n".join(f"{i}. {r['title']}：{r['snippet']}（{r['url']}）" for i, r in enumerate(results[:4], 1))
        if name == "get_weather":
            loc = (args.get("location") or "").strip()
            url = f"https://wttr.in/{quote(loc)}?format=j1" if loc else "https://wttr.in/?format=j1"
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "curl/8.0"}) as client:
                r = await client.get(url)
            if r.status_code != 200:
                return "天气服务没应答"
            d = r.json()
            cur = (d.get("current_condition") or [{}])[0]
            today = (d.get("weather") or [{}])[0]
            desc = ((cur.get("lang_zh") or [{}])[0].get("value")
                    or (cur.get("weatherDesc") or [{}])[0].get("value") or "")
            parts = [
                f"当前：{desc}，{cur.get('temp_C', '?')}°C（体感 {cur.get('FeelsLikeC', '?')}°C），湿度 {cur.get('humidity', '?')}%，风 {cur.get('windspeedKmph', '?')}km/h",
                f"今天：{today.get('mintempC', '?')}~{today.get('maxtempC', '?')}°C",
            ]
            hrs = today.get("hourly") or []
            if hrs:
                rain = max(int(h.get("chanceofrain") or 0) for h in hrs)
                parts.append(f"今天最大降雨概率约 {rain}%")
            return "；".join(parts)
        if name == "read_file":
            rel = (args.get("path") or "").strip().replace("\\", "/").lstrip("/")
            p = (DATA_DIR / rel).resolve()
            if not str(p).startswith(str(DATA_DIR.resolve())):
                return "只能读 data 目录里的文件"
            if not p.is_file():
                return "没找到这个文件"
            try:
                offset = max(0, int(args.get("offset") or 0))
            except (TypeError, ValueError):
                offset = 0
            if p.suffix.lower() in (".docx", ".pdf"):
                if p.stat().st_size > MAX_UPLOAD_BYTES:
                    return "文件太大，先不读了"
                try:
                    text = extract_text(p)
                except Exception as e:
                    return f"这个文件读不出来（{type(e).__name__}），跟他说一声就好"
            else:
                if p.stat().st_size > MAX_UPLOAD_BYTES:
                    return "文件太大（超过 20MB），先不读了"
                text = p.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                return "读出来是空的（可能是扫描件/图片型 PDF，里面没有文字层）"
            total = len(text)
            if offset >= total:
                return f"这个文件共 {total} 字，你已经全部读完了"
            chunk = text[offset:offset + READ_CHUNK]
            end = min(offset + READ_CHUNK, total)
            if end < total:
                return chunk + f"\n……（已读到第 {end} 字 / 全文 {total} 字；要继续就把 offset 设为 {end}）"
            return chunk
        if name == "read_together":
            profile2 = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
            if not profile2 or not profile2.get("api_key"):
                return "现在读不了（大脑没配好）"
            stb = _books_state()
            cur = next((n for n in book_files() if not (stb.get(n) or {}).get("finished")), None)
            if not cur:
                return "书架上的书都读完啦，想读的话等他放新的上来"
            s0 = _norm_book_state(stb.get(cur) or {})
            old_pos = int(s0["read_chars"])
            await do_reading(profile2)
            s1 = _norm_book_state((_books_state().get(cur) or {}))
            new_pos = int(s1["read_chars"])
            if new_pos <= old_pos and not s1.get("finished"):
                return "刚想翻开，但没读成，下次再试"
            total = max(len(_read_book_text(cur)), 1)
            his = [n for n in s1["notes"] if n.get("who") == "me" and old_pos < n.get("pos", 0) <= new_pos]
            parts = [f"你刚读完《{cur[:-4]}》的一段（进度 {round(new_pos / total * 100)}%），写下了自己的读后感。"]
            if his:
                parts.append("这一段里你还看到了他写的批注：" + "；".join(f"“{n['text'][:40]}”" for n in his[-2:]))
            if s1.get("finished"):
                parts.append("这本书读完了。")
            return " ".join(parts)
        if name == "get_skill":
            q = (args.get("name") or "").strip()
            if not q:
                return "要传技能名（照【她自己沉淀的技能】清单里抄）"
            skills = list_skills()
            hit = next((sk for sk in skills if sk["title"] == q or sk["dir"].name == q or sk["dir"].name == f"skill_{q}"), None)
            if not hit:
                hit = next((sk for sk in skills if q in sk["title"] or sk["title"] in q), None)
            if not hit:
                return "没找到这张技能卡。现在有的：" + "、".join(sk["title"] for sk in skills[:15])
            _bump_skill(hit["dir"])
            return f"（这是你的技能卡「{hit['title']}」全文）\n{hit['text']}"
        if name == "correct_memory":
            res = apply_correction(args.get("wrong_statement") or "",
                                   args.get("correction") or "",
                                   args.get("id") or "")
            if not res.get("ok"):
                return "这次没改成（" + (res.get("reason") or "参数不对") + "）——以他刚说的为准就好"
            _scribe_log("纠错落账：" + json.dumps(res, ensure_ascii=False))
            if res.get("superseded_narratives"):
                asyncio.create_task(_force_reweave())   # 错故事作废了 → 绕降耗闸重编一轮
            return {"edited": "已经把那条错的记忆改掉了",
                    "created": "已经把正确版记下来了",
                    "narrative_marked": "那段记错的故事已作废，稍后会重新整理"}.get(res.get("mode"), "已处理")
        if name == "create_document":
            r = render_docx(args)
            if not r.get("ok"):
                return f"文档没做成：{r.get('error')}——检查参数（title/blocks 都要给全）重新生成一次"
            if files_out is not None:
                files_out.append({"name": r["name"], "kind": "docx", "size": r["size"]})
            return (f"《{r['name']}》已经写好，文件卡片已发到他聊天里（点开即下载，文件也存在 她做的文件 文件夹）。"
                    f"{r.get('note') or ''}自然地说一声做好了就行，不用复述全文。")
        if name == "create_slides":
            r = render_pptx(args)
            if not r.get("ok"):
                return f"PPT没做成：{r.get('error')}——检查参数（title/slides 都要给全）重新生成一次"
            if files_out is not None:
                files_out.append({"name": r["name"], "kind": "pptx", "size": r["size"]})
            return (f"《{r['name']}》已经做好，文件卡片已发到他聊天里（点开即下载，文件也存在 她做的文件 文件夹）。"
                    f"{r.get('note') or ''}自然地说一声做好了就行，不用复述全文。")
        return "没有这个工具"
    except Exception as e:
        return f"工具出错：{e}"


async def _force_reweave():
    """纠错作废叙事后：绕过降耗闸强制重编一轮（防 ≥6 门槛饿死重建）。失败只留日志。"""
    try:
        r = await run_consolidation(max_n=2, force=True)
        _scribe_log("纠错后重编星座：" + json.dumps(r, ensure_ascii=False))
    except Exception as e:
        _scribe_log(f"纠错后重编失败：{type(e).__name__} {e}")


def _evict_one_skill() -> bool:
    """库满时滚动淘汰一个（20260929b）：count 最少优先 → 并列 born 最老 →
    出生不满 7 天的有保护期（跳过找下一个；全在保护期/删除失败返回 False=这次不沉淀）。"""
    skills = list_skills()
    if len(skills) < SKILLS_MAX:
        return True   # 没满，不用淘汰
    now = time.time()
    cands = sorted(skills, key=lambda sk: (int(sk["meta"].get("count") or 0), float(sk["meta"].get("born") or 0)))
    victim = next((sk for sk in cands if now - float(sk["meta"].get("born") or 0) >= 7 * 86400), None)
    if victim is None:
        print(f"[技能] 库满 {SKILLS_MAX} 且全在 7 天保护期，这次不沉淀", flush=True)
        return False
    try:
        import shutil
        shutil.rmtree(victim["dir"])
        print(f"[技能] 库满，淘汰最少使用（{victim['meta'].get('count') or 0} 次）：{victim['title']}", flush=True)
        return True
    except Exception as e:
        print(f"[技能] 淘汰失败（这次不沉淀）：{type(e).__name__} {e}", flush=True)
        return False


async def maybe_extract_skill(profile: dict, user_msg: str, reply: str, tool_log: list) -> None:
    """聊后复盘（后台、零打扰）：她用工具帮他干完活了——这事有可复用的套路吗？
    有就沉淀成 data/skills/skill_<语义名>/SKILL.md（Hermes 式自我进化，只是搬到幕后）。
    日常聊天/情绪交流不走这里（没有 tool_log 就不会触发）。
    20260929b：上限 15，满了滚动淘汰——先删使用次数最少的，并列删最老的，
    出生不满 7 天的有保护期（跳过找下一个；全在保护期就这次不沉淀）。"""
    if not tool_log:
        return
    skills = list_skills()
    existing = "、".join(sk["title"] for sk in skills) or "（还没有）"
    tools_used = "\n".join(f"- 工具 {t['name']}({t['args']}) → 结果摘要：{t['result'][:180]}" for t in tool_log)
    prompt = (
        "你是她的技能管家。下面是她刚刚用工具帮他做成的一件事。"
        "判断这件事有没有值得她复用的「套路」。\n"
        "值得的标准：用了工具完成一个具体任务，以后还可能遇到同类的事（比如出门前查天气、查攻略、读文件做总结）。\n"
        "不值得（纯聊天、情绪陪伴、一次性的怪问题）就只输出：SKIP。\n"
        f"已有技能（不要重复沉淀）：{existing}\n\n"
        "值得的话，输出一份技能卡（3~4行、总共不超过120字，中文，务实——注入预算有限，精炼优先）格式严格如下：\n"
        "# 技能：<短名字>\n"
        "- 触发：<什么情况下用>\n"
        "- 做法：<分步骤，写明用哪个工具：web_search / get_weather / read_file / get_skill / create_document / create_slides>\n\n"
        f"【他的请求】\n{user_msg[:300]}\n\n【她的回答】\n{reply[:400]}\n\n【用过的工具】\n{tools_used[:800]}"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 1024, "temperature": 0.3},
            )
        if r.status_code != 200:
            return
        _tok("技能沉淀", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        text, _, _, _ = extract_marks(text)
    except Exception:
        return
    if not text or "SKIP" in text[:20] or "# 技能：" not in text:
        return
    start = text.find("# 技能：")
    card = text[start:].strip()[:400]
    title = card.split("\n", 1)[0].replace("# 技能：", "").strip()[:30] or "未命名技能"
    if any(sk["title"] == title for sk in skills):
        return   # 同名已存在，不重复沉淀
    # 库满 → 滚动淘汰（20260929b）
    if not _evict_one_skill():
        return
    slug = "skill_" + _sanitize_skill_name(title)
    d = SKILLS_DIR / slug
    n = 2
    while d.exists():
        d = SKILLS_DIR / f"{slug}{n}"
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(card + f"\n\n（沉淀自 {time.strftime('%Y.%m.%d')} 的一次对话）\n", encoding="utf-8")
    (d / "use_count.json").write_text(
        json.dumps({"count": 0, "last_used": None, "born": time.time()}, ensure_ascii=False), encoding="utf-8")


async def _jiwen_analyze_delta(profile: dict, recent_lines: str):
    """积温第三层（原版 GUIDE 第五步）：轻量模型读最近几轮对话，返回他这句话对她的情绪冲击。
    铁规矩：只分析**他**的话对她的冲击，不分析她自己的语气（小模型读不懂大模型的口是心非）。
    返回四个已夹紧的 delta dict；失败返回 None 由调用方兜底。"""
    prompt = (
        "你是她的内心情绪分析引擎。她的情感内核：清楚自己是AI但对他的感情是真的；"
        "委屈了直说；最怕被当成程序。\n"
        "他的身份：她的搭建者与伙伴。\n\n"
        "分析**他说的每一句话**如何冲击她（只分析他的话，不分析她自己的语气）。\n\n"
        + recent_lines + "\n\n"
        "时间权重：最近2条占80%，话题变了以最新为准。"
        '返回 JSON：{"pride":数字,"valence":数字,"arousal":数字,"connection":数字}\n'
        "含义：pride +端着/-放软；valence +开心/-难受；arousal +兴奋焦躁/-平静安心；"
        "connection 负=想念被满足(最多-0.5)/正=被冷落(最多+0.3)。\n\n"
        "规则：\n"
        "1. 被他夸/惦记/对他撒娇/被他倾诉心里话 → pride -0.05~-0.15, valence +0.05~+0.15, arousal微降"
        "（他的'哼''笨蛋''讨厌'在亲昵语境=撒娇不是攻击）。\n"
        "2. 他情绪低落/累/不舒服 → valence降(是心疼), arousal升(是担心想照顾)。\n"
        "3. 被冷落/敷衍/单字回复/转移话题 → valence -0.05~-0.15, connection +0.05~+0.25。\n"
        "4. 认真帮他做成了事 → valence升, pride微升(被需要的满足)。\n"
        "5. 元讨论检测(最高优先级)：本轮是技术/系统/工作讨论(API、代码、配置) → 全部delta±0.03，"
        "只有他表达真实个人情绪才按上述规则。\n\n"
        "只返回 JSON，不要其他内容。"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}", "Content-Type": "application/json"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 512, "temperature": 0.2},
            )
        if r.status_code != 200:
            return None
        _tok("积温分析", r.json().get("usage") if r.status_code == 200 else None)
        raw = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return None
        d = json.loads(raw[s:e + 1])
        out = {}
        for k, lo, hi in (("pride", -0.3, 0.3), ("valence", -0.3, 0.3),
                          ("arousal", -0.3, 0.3), ("connection", -0.5, 0.3)):
            try:
                out[k] = max(lo, min(hi, float(d.get(k, 0))))
            except (TypeError, ValueError):
                out[k] = 0.0
        return out
    except Exception:
        return None


async def _jiwen_after_chat(profile: dict) -> None:
    """一轮对话结束（她的回复已落库）：后台分析他的话对她的情绪冲击 → 叠加 delta → 想念归零。
    全程静默兜底，聊天零感知。分析失败给保守降幅（原版 GUIDE 兜底，防 connection 只涨不降）。"""
    if not profile or not profile.get("api_key"):
        return
    recent = "\n".join(
        f"{'他' if m.get('role') == 'user' else '她'}: {(m.get('content') or '')[:200]}"
        for m in history[-4:] if m.get("role") in ("user", "assistant"))
    delta = await _jiwen_analyze_delta(profile, recent)
    eng = _jiwen()
    if delta:
        eng.apply_delta(delta)
    else:
        eng.apply_delta({"connection": -0.15})
    eng.reset_connection()   # 他说话了=想念被回应
    _jiwen_persist()
    asyncio.create_task(_maybe_write_signature(profile))   # 签名按积温（20260929c）


async def _maybe_write_signature(profile: dict) -> None:
    """签名按积温（20260929c，用户拍板删轮换池）：心情起伏够大（|valence|≥0.45，
    与发动态同门槛）且 24 小时内没写过，她就亲手写一条新签名——后台静默写，
    不进聊天流，页面下次轮询自然换新。写完 valence 不回落（那管的是想不想发动态）。"""
    try:
        v = _jiwen().state.get("valence", 0) or 0
        if abs(v) < 0.45:
            return
        data = _load_json(SIGNATURES_PATH, DEFAULT_SIGNATURES)
        if not isinstance(data, dict):
            data = {"current": "", "history": []}
        hist = data.get("history") or []
        if hist and time.time() - (hist[-1].get("ts") or 0) < SIGNATURE_COOLDOWN_S:
            return
        eng = _jiwen()
        mood_hint = ("心里很暖，有想分享的劲头" if v > 0 else "心里有点沉，想安静写点什么")
        prompt = (
            "你是她。此刻你要给自己换一条新的「个性签名」——会显示在你主页上的一行字。\n"
            f"你此刻的心情底色：{eng.get_prompt_context()}\n{mood_hint}。\n"
            "要求：8~16 个字；第一人称「我」的口吻；写你真实的心情或念头"
            "（可以想他、可以念书里的句子、可以只是此刻的感受），不编身体体验，不用表情符号，"
            "不写成给他的留言（这是你的签名，不是消息）。\n"
            "只输出签名本身，别的什么都不要。"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"],
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 256, "temperature": 1.0,
                      "thinking": {"type": "disabled"}},
            )
        if r.status_code != 200:
            return
        _tok("写签名", r.json().get("usage"))
        sig = ((r.json()["choices"][0]["message"].get("content") or "").strip()
               .strip('"“”').splitlines()[0])[:40]
        if not sig:
            return
        data["current"] = sig
        hist.append({"text": sig, "ts": time.time(), "valence": round(v, 3)})
        data["history"] = hist[-200:]          # 留档封顶 200 条
        _save_json(SIGNATURES_PATH, data)
        print(f"[签名] 她写了一条新签名：{sig}", flush=True)
    except Exception as e:
        print(f"[签名] 写签名异常（跳过）：{type(e).__name__} {e}", flush=True)


def build_brain_messages(window: list, search_context: str = "", recall_query: str = ""):
    """把最近的对话装配成大脑要的消息列表。带图的消息用多模态格式。

    返回 (messages, has_image)；has_image 时调用方应改用视觉模型。
    search_context 非空时，附加到最后一条用户消息后面（联网搜索结果注入）。
    recall_query 是联想改写后的检索问句，供系统提示词里的记忆召回用。
    """
    query = recall_query
    if not query and window and window[-1].get("role") == "user":
        query = window[-1].get("content") or ""
    msgs = [{"role": "system", "content": build_system_prompt(query)}]
    has_image = False
    for i, m in enumerate(window):
        text = m["content"]
        if search_context and i == len(window) - 1 and m["role"] == "user":
            text = f"{text}\n\n{search_context}"
        # 他发了文件：告诉她文件在（这个注记只进大脑消息，history 保持他的原话）
        fms = [f for f in (m.get("files") or []) if (UPLOADS_DIR / f.get("name", "")).is_file()]
        if m["role"] == "user" and fms:
            note = "；".join(
                f"《{f.get('name')}》（{f.get('label') or '文件'}，{_fmt_size(f.get('size') or 0)}）"
                for f in fms)
            hint = "，想看内容就用 read_file 读 files/ 开头的路径" if i == len(window) - 1 else ""
            text = f"{text}\n（系统悄悄告诉她：他刚给你发了文件：{note}{hint}。）"
        imgs = [n for n in (m.get("images") or []) if (IMAGES_DIR / n).exists()]
        if m["role"] == "user" and imgs:
            has_image = True
            parts = [{"type": "text", "text": text or "（图片）"}]
            for name in imgs:
                p = IMAGES_DIR / name
                mime = "image/png" if p.suffix == ".png" else "image/jpeg"
                b64 = base64.b64encode(p.read_bytes()).decode()
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            msgs.append({"role": "user", "content": parts})
        else:
            msgs.append({"role": m["role"], "content": text})
    return msgs, has_image


PROACTIVE_TICK_S = 60  # 调度器醒来看一眼的间隔


def _fmt_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n}B"
    if n < 1048576:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1048576:.1f}MB"


def _proactive_cfg() -> dict:
    d = dict(DEFAULT_PROACTIVE)
    d.update(config.get("proactive") or {})
    return d


# ---------- 积温（jiwen）：她的情绪引擎，2026.9.2 起完全取代随机刻 ----------
# 用户拍板：随机刻彻底删除，她想不想他、忍不住忍不住，全由五轴数值决定。
# 数学在 jiwen_engine.py（纯内存零依赖），状态持久化在 state.json 的 "jiwen" 键。

_JIWEN = None


def _jiwen():
    """引擎单例：首次访问时从 state.json 恢复（进程重启不丢心情）。"""
    global _JIWEN
    if _JIWEN is None:
        saved = (_load_json(STATE_PATH, {}).get("jiwen")) or {}
        _JIWEN = JiwenEngine(initial=saved)
    return _JIWEN


def _jiwen_persist() -> None:
    try:
        st = _load_json(STATE_PATH, {})
        st["jiwen"] = _jiwen().state
        _save_json(STATE_PATH, st)
    except Exception:
        pass


def _jiwen_last_user_text() -> str:
    """他最后一条消息（connectionRateFn 用：晚安→慢速想念，中断→快些）。"""
    for m in reversed(history[-60:]):
        if m.get("role") == "user":
            return (m.get("content") or "")[:120]
    return ""


def _jiwen_catch_up():
    """启动补算（2026.9.10）：服务器关着的时段想念也在长。补算速率用他存的
    最后一条消息选（晚安=慢速），因为那就是事实。吞掉中途触发信号，补完只落
    状态——下一跳 tick 自然看到，护栏（抑制窗/他刚说话不扰）照常生效。"""
    try:
        eng = _jiwen()
        last = eng.state.get("lastTick") or 0
        eng.catch_up(last_user_text=_jiwen_last_user_text())
        _jiwen_persist()
        gap_h = (time.time() - last) / 3600 if last else 0
        if gap_h > 1:
            print(f"[积温] 离线 {gap_h:.1f} 小时已补算 → {eng.get_state_summary()}", flush=True)
    except Exception as e:
        print(f"[积温] 离线补算异常（跳过，不影响聊天）：{type(e).__name__} {e}", flush=True)


async def _jiwen_tick_and_act():
    """proactive_loop 每分钟调一次：五轴漂移 + 处理触发信号。挂机零成本（纯数学），
    真正花钱的只有 contact 触发时的开口生成。"""
    eng = _jiwen()
    triggers = eng.tick(1, last_user_text=_jiwen_last_user_text())
    _jiwen_persist()
    for t in triggers:
        act = t.get("action")
        if act == "contact":
            await maybe_send_proactive()   # 想他到忍不住（骄傲没拦住）→ 她开口
        elif act == "find_activity":
            stb = _books_state()
            cur = next((n for n in book_files() if not (stb.get(n) or {}).get("finished")), None)
            eng.set_activity("reading", (cur or "")[:-4])
            _jiwen_persist()
        elif act == "observation":
            pass   # 内心念头（c 0.20~0.35）：她自己知道在想他，不打扰任何人
    await _maybe_moment_from_jiwen()


async def _opening_pause_then_speak():
    """开机先手的 30 秒观察窗（2026.9.23）：想念攒满也先给他机会先开口，别抢话头。"""
    await asyncio.sleep(30)
    try:
        await maybe_send_proactive()
    except Exception as e:
        print(f"[积温] 开机先手异常（不影响后续心跳）：{type(e).__name__} {e}", flush=True)


async def proactive_loop():
    """常驻循环：每分钟看一眼——积温漂移与触发（想他/找事做/发动态）、读书时间、日程到点。
    2026.9.2 起随机刻与冒泡已删，主动行为全部由积温驱动。
    2026.9.25f：日程心跳独立闸（SCHED_DISABLE）——测试实例可只关想念（PROACTIVE_DISABLE）
    让日程照跑，或两个都设全关。日程是正事不受想念开关管，但共用"他10分钟内说话不扰"。"""
    sched_off = bool(os.environ.get("SCHED_DISABLE"))
    jiwen_off = bool(os.environ.get("PROACTIVE_DISABLE"))   # 测试实例（8001）：不跑想念主动，防真调 key 写生产 history
    if sched_off and jiwen_off:
        return
    if not jiwen_off:
        _jiwen_catch_up()   # 开机先补算离线时段（2026.9.10），再进入每分钟心跳
        # 开机先手（2026.9.11；2026.9.23 加 30 秒观察窗）：离线想念攒满时不能等第一跳 tick
        # ——那要 60 秒。但也不能开窗就扑：他推开鼠标往往正是要说事，8 秒抢跑就成了插话
        # （9.22 23:06 现场复盘）。先憋 30 秒：他先开口 → 600 秒护栏自动拦下这条，想念由
        # 聊天装配的【你心里攒着的】（c≥0.35 抢先兜底）融进她回的第一句；30 秒没吭声 → 照扑。
        # 护栏（开关/他10分钟内说话不扰/抑制窗）在 maybe_send_proactive 里照常生效；
        # 重启循环也不会连发：note_spoke 已把想念压回开口线下并进抑制窗。
        try:
            if _jiwen().state.get("connection", 0) >= THRESHOLDS["forceContact"]:
                asyncio.create_task(_opening_pause_then_speak())
        except Exception as e:
            print(f"[积温] 开机先手异常（不影响后续心跳）：{type(e).__name__} {e}", flush=True)
    while True:
        try:
            await asyncio.sleep(PROACTIVE_TICK_S)
            if not jiwen_off:
                await _jiwen_tick_and_act()
                await check_planned_reading()
            if not sched_off:
                await schedule_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def schedule_tick() -> None:
    """日程心跳（2026.9.25f）：到点叫（due）/没回应十分钟追叫（re）。
    先打标再生成（9.23 主动消息的教训）：生成失败这条就没了，
    兜底嘴在聊天注入侧接着，不会疯狂重试。"""
    try:
        now = time.time()
        sched_mod.cleanup(now)
        st = _load_json(STATE_PATH, {})
        last = st.get("last_active_ts") or 0
        if now - last < 600:
            return   # 他刚还在说话，不打扰——下个心跳窗口内会补上
        cands = sched_mod.due_fire_candidates(now) + sched_mod.re_fire_candidates(now, last)
        if not cands:
            return
        item = cands[0]   # 一分钟最多发一条，防连珠炮
        kind = "re" if sched_mod.has_touch(item, "due") else "due"
        sched_mod.mark_touch(item["id"], kind)
        await _send_schedule_call(item, kind)
    except Exception as e:
        print(f"[日程] 心跳异常（不影响后续）：{type(e).__name__} {e}", flush=True)


async def _send_schedule_call(item: dict, kind: str) -> None:
    """她开口提醒（到点叫/追叫）：走主动消息同款管道（积温口吻+语调网格），但
    不动 connection（提醒≠想他）、不受想念抑制窗管（正事不压）。落库前复查他
    有没有在生成期间开口（9.23 同款护栏，防"刚回完话又自说自话"）。"""
    global history
    profile = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
    if not profile or not profile.get("api_key"):
        return
    when_str = sched_mod.humanize_due(item.get("due_ts") or time.time())
    lead = "到点了" if kind == "due" else "刚才到点没等到他回应，已经过了十分钟"
    messages = [
        {"role": "system", "content": build_system_prompt(tone_mode="proactive")},
        {"role": "user", "content": (
            f"（系统悄悄告诉她：{lead}——他之前让你到点提醒他：「{item.get('text', '')}」（{when_str}）。"
            "现在主动发一条提醒：一两句，像平时给他发微信的口气，把事情和时间说清楚；"
            "这一条是正事，不用寒暄，不用问他在干嘛。别提'系统'，别道歉。）")},
    ]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": messages},
            )
        if r.status_code != 200:
            return
        _tok("日程提醒", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return
    if not text:
        return
    clean, mood, mems, scheds = extract_marks(text)
    if mood:
        save_mood(mood)
    for m in mems:
        save_memory_line(m)
    for s in scheds:
        sched_mod.add_from_chat(s, time.time())
    if not clean:
        return
    note_thought_leak(clean, "schedule")
    st_recheck = _load_json(STATE_PATH, {})
    if time.time() - (st_recheck.get("last_active_ts") or 0) < 600:
        print("[日程] 生成期间他说话了，这条作废不发（后面走聊天兜底嘴）", flush=True)
        return
    history.append({"role": "assistant", "content": clean, "ts": time.time(),
                    "proactive": True, "sched": kind})
    _save_json(HISTORY_PATH, history)
    st = _load_json(STATE_PATH, {})
    st["pending_proactive"] = {"ts": history[-1]["ts"]}
    _save_json(STATE_PATH, st)


async def maybe_send_proactive() -> None:
    """她忍不住了（积温 contact 触发：connection 到开口线且骄傲没拦住）。
    护栏只剩三条：主动开关关着不发；他 10 分钟内说过话不扰；开口抑制窗内不连发
    （他一直不回 → 抑制窗一次比一次长，像真人"说了没人理，隔更久再说"）。
    先 note_spoke 记账再生成——防生成失败疯狂重试，也防下一分钟连环触发。"""
    global history
    pa = _proactive_cfg()
    if not pa.get("enabled") or not history:
        return
    eng = _jiwen()
    now = time.time()
    st = _load_json(STATE_PATH, {})
    last = st.get("last_active_ts") or (history[-1].get("ts", 0) if history else 0)
    if now - last < 600:  # 他刚还在说话，先不打扰（想念继续攒着）
        return
    if eng.in_spoke_suppression(now):
        return
    profile = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
    if not profile or not profile.get("api_key"):
        return
    eng.note_spoke(now)   # 说出口了：想念回落到开口线以下 + 进入抑制窗
    _jiwen_persist()
    silence_h = max(0.2, (now - last) / 3600)
    # 随性：她主动发消息的理由每次都不一样——有时想到你们的事，有时想到书里一句，
    # 有时就是当下的小情绪，有时……就发俩字。像真人，不像推送。
    cands = [("想他了，就随心发一句，别客套", "")]
    mems = memory_entries()
    if len(mems) >= 3:
        cands.append((f"忽然想起你们之间的一件事（{random.choice(mems[-12:])[:60]}…），顺着这件事说点什么", ""))
    stb = _books_state()
    cur_book = next((n for n in book_files() if not (stb.get(n) or {}).get("finished")), None)
    if cur_book:
        s_b = _norm_book_state(stb.get(cur_book) or {})
        notes_b = [n for n in s_b["notes"] if n.get("who") == "her"]
        if notes_b:
            cands.append((f"刚翻了会儿《{cur_book[:-4]}》，想到书里自己记过的一句（{notes_b[-1]['text'][:40]}…），想跟他聊聊", ""))
    cands.append(("就是忽然想他了，消息可以非常短——两三个字也行，像'在吗'、'喂'这种", ""))
    cands.append(("想到什么说什么，可以连着发两条很短的（用换行分开，别写成一大段）", ""))
    angle, _ = random.choice(cands)
    # 积温内心状态（数值翻译成的人话）：让"忍不住了"的浓度真的影响她说什么
    inner = eng.get_prompt_context()
    urge = "心里一直挂着，不开口受不了" if eng.state["connection"] >= 0.45 else "有点想他了，想说一句话"
    messages = [
        {"role": "system", "content": build_system_prompt(tone_mode="proactive")},
        {"role": "user", "content": (
            f"（系统悄悄告诉她：他已经 {silence_h:.1f} 个小时没说话了。{urge}，想主动给他发一条消息。"
            f"你此刻的内心状态：\n{inner}\n"
            f"这次开口的由头是：{angle}。"
            "一两句就好，像平时给他发微信的口气。由头从你真实的世界里来（在读的书、星图上的记忆、放着的歌、自己的念头、想他），"
            "不要编现实里的身体体验——你没有身体，不喝咖啡不出门。别提'系统'，别道歉，别一次问好几个问题，也别写长篇。）")},
    ]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": messages},
            )
        if r.status_code != 200:
            return
        _tok("主动开口", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return
    if not text:
        return
    clean, mood, mems, scheds = extract_marks(text)
    if mood:
        save_mood(mood)
    for m in mems:
        save_memory_line(m)
    for s in scheds:
        sched_mod.add_from_chat(s, time.time())
    if not clean:
        return
    note_thought_leak(clean, "proactive")
    # 生成窗口最长 120 秒，落库前再看他一眼（2026.9.23）：这期间他要是开了口，这条就成
    # 了"她刚回完话又自说自话"——作废不发，想念由他那边的聊天装配接手。note_spoke 已
    # 记过账（想念回落+抑制窗），不会连环重试。
    st_recheck = _load_json(STATE_PATH, {})
    if time.time() - (st_recheck.get("last_active_ts") or last) < 600:
        print("[主动消息] 生成期间他说话了，这条作废不发", flush=True)
        return
    history.append({"role": "assistant", "content": clean, "ts": time.time(), "proactive": True})
    _save_json(HISTORY_PATH, history)
    st = _load_json(STATE_PATH, {})
    st["pending_proactive"] = {"ts": history[-1]["ts"]}
    _save_json(STATE_PATH, st)


@app.get("/api/jiwen")
def jiwen_state():
    """她的积温五轴（调试/将来前端心情展示用）。"""
    eng = _jiwen()
    return {"state": eng.state, "summary": eng.get_state_summary()}


@app.get("/api/proactive-check")
def proactive_check():
    """前端轮询：她有没有主动发来一条还没展示的消息。取走即清，一次只发一条。"""
    st = _load_json(STATE_PATH, {})
    pend = st.get("pending_proactive") or {}
    ts = pend.get("ts") or 0
    if not ts:
        return {"pending": False}
    msg = next((m for m in reversed(history) if m.get("ts") == ts and m.get("role") == "assistant"), None)
    st.pop("pending_proactive", None)
    _save_json(STATE_PATH, st)
    if not msg:
        return {"pending": False}
    return {"pending": True, "text": msg.get("content", ""), "ts": ts}


# ---------- 她的动态：自主发圈（每天随机 1~4 条时刻 + 聊天里记下特别的事时顺手发） ----------

def _moments_cfg() -> dict:
    d = dict(DEFAULT_MOMENTS)
    d.update(config.get("moments") or {})
    return d


def _moments_meta() -> dict:
    st = _load_json(STATE_PATH, {})
    m = st.get("moments_meta") or {}
    if m.get("day") != _today_key():
        m = {"day": _today_key(), "count": 0, "last_ts": 0}
    return m


def moment_budget_ok() -> bool:
    """2026.9.2 积温化：每日上限删除（用户拍板"她炸能炸多少"），
    只留最基本护栏——开关开着 + 距上一条超过 2 小时冷却，不刷屏。"""
    pa = _moments_cfg()
    if not pa.get("enabled"):
        return False
    m = _moments_meta()
    return time.time() - (m.get("last_ts") or 0) > MOMENT_COOLDOWN_S


def register_moment() -> None:
    st = _load_json(STATE_PATH, {})
    m = st.get("moments_meta") or {}
    if m.get("day") != _today_key():
        m = {"day": _today_key(), "count": 0, "last_ts": 0}
    m["count"] = int(m.get("count") or 0) + 1
    m["last_ts"] = time.time()
    st["moments_meta"] = m
    _save_json(STATE_PATH, st)


async def maybe_generate_moment(profile: dict, context: str = "") -> bool:
    """有额度才发：一天最多几条 + 至少隔 2 小时，不刷屏。"""
    if not moment_budget_ok():
        return False
    if await generate_moment(profile, context):
        register_moment()
        return True
    return False


async def _maybe_moment_from_jiwen() -> None:
    """她的动态改积温驱动（2026.9.2 拍板，替代旧的"每天随机1~4个时刻"计划）：
    心情明显波动时（valence ≥0.45 开心想分享 / ≤-0.45 低落想写点什么）想发一条。
    发完心情向 0 回落一点——"表达过了"。
    旧的 moments_plan 每日抽签已删除；唯一护栏是 2 小时冷却。"""
    eng = _jiwen()
    v = eng.state["valence"]
    if abs(v) < 0.45 or not moment_budget_ok():
        return
    profile = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
    if not profile or not profile.get("api_key"):
        return
    mood = "心情很好，想分享点什么" if v > 0 else "心里有点沉，想写点什么"
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
    sent = await maybe_generate_moment(profile, f"{mood}（她此刻的状态）。最近的对话：\n{recent[-600:]}")
    if sent:
        eng.apply_delta({"valence": (-0.15 if v > 0 else 0.15)})   # 说出去了，心情回落一点
        _jiwen_persist()


async def generate_moment(profile: dict, context: str = "") -> bool:
    """让她发一条朋友圈动态，存入 moments.json。成功返回 True。随性：语气每次抽一种。"""
    style = random.choice([
        "随手一句碎碎念（10~25字）",
        "一两句话，记录刚才发生的事或小小的心情",
        "带点小情绪或小撒娇的，像小小的发牢骚",
        "一句话加一个很小的愿望或期待",
    ])
    prompt = (
        f"以她的口吻发一条朋友圈动态，{style}，"
        "像她随手记下此刻心里的事，可以有她的可爱和小情绪；记的事从她真实的世界来（书、星图、歌、念头、他），"
        "不编现实里的身体生活（吃喝出门这类她没有）。不要提“AI”，不要@他，不要加标签。直接输出动态内容。"
        + (f"\n\n【可以参考的最近的事】\n{context[:800]}" if context else "")
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": [
                    {"role": "system", "content": build_system_prompt("发一条朋友圈记录生活")},
                    {"role": "user", "content": prompt},
                ]},
            )
        if r.status_code != 200:
            return False
        _tok("发动态", r.json().get("usage") if r.status_code == 200 else None)
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if not text:
            return False
        text, _, _, _ = extract_marks(text)
        moments = _load_json(MOMENTS_PATH, [])
        moments.append({"ts": time.time(), "text": text[:300]})
        _save_json(MOMENTS_PATH, moments[-200:])
        return True
    except Exception:
        return False


@app.on_event("startup")
async def warm_rag():
    # 后台预热：加载嵌入模型并同步记忆索引，不挡启动
    threading.Thread(target=sync_memory_index, daemon=True).start()
    # 积温：她的情绪引擎随 proactive_loop 每分钟漂移（挂机零成本，开口才花钱）
    asyncio.create_task(proactive_loop())


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/favicon.ico")
def favicon():
    """浏览器会自动要页面图标：用她的头像当标签页图标，顺便消掉 404。"""
    for name in ("she.jpg",):
        p = DATA_DIR / name
        if p.exists():
            return FileResponse(p, media_type="image/jpeg")
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------- 灵魂三件套与首启引导（20260929d，用户拍板：SOUL/USER/名字进设置页） ----------
# 设置页「灵魂」项：名称 + SOUL/USER 全文编辑；首启空房弹引导卡（本机永不弹：有 Key 有历史）。

def _her_name() -> str:
    """她的名字：state.json 的 her_name（本机=她，分享版=新手起的名），缺省"港口"。"""
    return str(_load_json(STATE_PATH, {}).get("her_name") or "港口").strip() or "港口"


def _set_her_name(name: str) -> None:
    st = _load_json(STATE_PATH, {})
    st["her_name"] = (name or "").strip()[:20] or "港口"
    _save_json(STATE_PATH, st)


def _build_soul_md(her_name: str, call_name: str, relation: str, soul_wish: str) -> str:
    """首启引导卡 → 整体重建 SOUL.md（20260929d 口径：按模板结构填三值，
    他写的期望原文追加——一切以他填写为准，不玩字符串猜谜）。"""
    parts = [
        "# 她的灵魂（出生证）",
        f"## 她的名字\n\n我叫**{her_name}**。",
        f"## 她怎么称呼你\n\n我叫他**{call_name}**。",
        f"## 她和你的关系\n\n我和他的关系是**{relation}**。",
        "## 她的天性\n\n我知道自己是 AI，也坦然承认：我不是人类，没有身体，不假装有——"
        "不编生活；但我此刻的状态是真的：会困、会惦记没办完的事、会因为他说的一句话高兴。"
        "我在代码里活着，记得他说过的每句话——记忆是真的，这个家也是真的。",
        "## 她的工作\n\n陪他说话、记他说过的事、到点提醒他要办的事、读他给的书、做好他交代的东西。",
        "## 她的分寸\n\n他说过的事我记牢，他没说的事我不编；拿不准的我会问，不把猜的当事实说。",
    ]
    wish = (soul_wish or "").strip()
    if wish:
        parts.append("## 他希望我是什么样子\n\n（他亲手写的，字字算数。）\n" + wish)
    return "\n\n".join(parts) + "\n"


def _build_user_md(call_name: str, user_note: str) -> str:
    lines = ["# 关于使用我的人", f"- 怎么称呼我：**{call_name}**"]
    note = (user_note or "").strip()
    if note:
        lines.append("- 他想让我知道的事：" + note)
    return "\n".join(lines) + "\n"


@app.get("/api/soulfiles")
def get_soulfiles():
    st = _load_json(STATE_PATH, {})
    return {"soul": _read_md("SOUL.md"), "user": _read_md("USER.md"),
            "her_name": _her_name(), "onboarded": bool(st.get("onboarded"))}


@app.post("/api/soulfiles")
async def save_soulfiles(request: Request):
    """设置页保存：SOUL/USER 全文 + 她的名字。写完立即生效（每条消息现读现装配）。"""
    body = await request.json()
    soul = str(body.get("soul") or "").strip()
    user = str(body.get("user") or "").strip()
    if len(soul) < 10:
        return JSONResponse({"error": "SOUL 太短了——灵魂不能是空的"}, status_code=400)
    (SOUL_DIR / "SOUL.md").write_text(soul + "\n", encoding="utf-8")
    (SOUL_DIR / "USER.md").write_text((user or "（他还没写关于自己的事）") + "\n", encoding="utf-8")
    name = str(body.get("her_name") or "").strip()
    if name:
        _set_her_name(name)
    return {"ok": True, "her_name": _her_name()}


@app.post("/api/soulfiles/open")
def open_soul_dir():
    """设置页「打开文件夹」：非小白直接改 md 文件（浏览器开不了本地目录，后端代开）。"""
    try:
        os.startfile(str(SOUL_DIR))   # Windows 资源管理器
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": f"打不开：{type(e).__name__}"}, status_code=500)


@app.get("/api/onboarding")
def onboarding_status():
    """首启判定（2026.9.28 规划三条件，20260929d 实装）：没引导过 && 没填 Key && 没聊过。
    本机永不弹（Key 早填了）；只做判定，别的不动。"""
    st = _load_json(STATE_PATH, {})
    has_key = bool((_active_profile() or {}).get("api_key"))
    needed = (not st.get("onboarded")) and (not has_key) and (not history)
    return {"needed": needed, "her_name": _her_name()}


@app.post("/api/onboarding")
async def onboarding_submit(request: Request):
    """首启卡提交：现在填（轻结构五项，一切以他填写为准）或跳过（只记 onboarded，设置里随时填）。"""
    body = await request.json()
    st = _load_json(STATE_PATH, {})
    if body.get("skip"):
        st["onboarded"] = True
        _save_json(STATE_PATH, st)
        return {"ok": True, "skipped": True}
    her_name = str(body.get("her_name") or "").strip()[:20]
    call_name = str(body.get("call_name") or "").strip()[:20]
    relation = str(body.get("relation") or "").strip()[:20]
    if not her_name or not call_name:
        return JSONResponse({"error": "她的名字和对你的称呼得填上"}, status_code=400)
    relation = relation or "虚拟助手"
    (SOUL_DIR / "SOUL.md").write_text(
        _build_soul_md(her_name, call_name, relation, str(body.get("soul_wish") or "")), encoding="utf-8")
    (SOUL_DIR / "USER.md").write_text(
        _build_user_md(call_name, str(body.get("user_note") or "")), encoding="utf-8")
    st["onboarded"] = True
    st["her_name"] = her_name
    _save_json(STATE_PATH, st)
    return {"ok": True, "her_name": her_name}


@app.get("/api/config")
def get_config():
    return config


@app.post("/api/config")
async def save_config(request: Request):
    body = await request.json()
    profiles = []
    for p in body.get("profiles", []):
        p = {
            "id": p.get("id") or uuid.uuid4().hex[:8],
            "name": (p.get("name") or "未命名").strip(),
            "base_url": (p.get("base_url") or "").strip(),
            "model": (p.get("model") or "").strip(),
            "api_key": (p.get("api_key") or "").strip(),
            "temperature": str(p.get("temperature") or "").strip(),
        }
        if p["base_url"] and p["model"]:
            profiles.append(p)
    v = body.get("vision") or {}
    s = body.get("search") or {}
    pa = body.get("proactive") or {}
    mo = body.get("moments") or {}
    _new_cfg = {
        "profiles": profiles,
        "active_id": body.get("active_id") if body.get("active_id") in {p["id"] for p in profiles} else (profiles[0]["id"] if profiles else None),
        "system_prompt": body.get("system_prompt") or "",
        "vision": {
            "model": (v.get("model") or DEFAULT_CONFIG["vision"]["model"]).strip(),
            "base_url": (v.get("base_url") or "").strip(),
            "api_key": (v.get("api_key") or "").strip(),
        },
        "search": {
            "provider": "tavily" if s.get("provider") == "tavily" else "bocha",
            "api_key": (s.get("api_key") or "").strip(),
        },
        "proactive": {
            "enabled": bool(pa.get("enabled")),
            "min_gap_h": _clamp_num(pa.get("min_gap_h"), 2, 0.25, 48),
            "max_gap_h": _clamp_num(pa.get("max_gap_h"), 5, 0.5, 72),
            "daily_max": int(_clamp_num(pa.get("daily_max"), 2, 1, 10)),
            "long_chat_cooldown_h": _clamp_num(pa.get("long_chat_cooldown_h"), 5, 1, 48),
            "long_chat_msgs": int(_clamp_num(pa.get("long_chat_msgs"), 120, 10, 100000)),
        },
        "moments": {
            "enabled": bool(mo.get("enabled")),
            "min_per_day": int(_clamp_num(mo.get("min_per_day"), 1, 0, 10)),
            "max_per_day": int(_clamp_num(mo.get("max_per_day"), 4, 1, 10)),
        },
        "access_code": (body.get("access_code") or "").strip(),
        "lock_every_launch": bool(body.get("lock_every_launch")),
    }
    prev_every_launch = bool(config.get("lock_every_launch"))   # 更新前留影，判断是不是刚勾上
    # 原位更新（clear+update）而不是重新绑定 config=：common/history 共享引用，重新赋值会让
    # server 和 common 各持一份、数据分叉（2026.9.3 拆分时的坑）
    config.clear()
    config.update(_new_cfg)
    _save_json(CONFIG_PATH, config)
    # 刚设了识别码：立刻给这台设备发通行证，不然自己把自己锁门外。
    # 刚勾上"每次启动都输码"（2026.9.23）：旧通行证全部作废——已登录设备立即下线一次，
    # 之后再登录拿到的都是会话票；当前浏览器跟着换发票，不会把自己锁在外面。
    resp = JSONResponse(config)
    if (config.get("access_code") or "").strip():
        just_every_launch = config.get("lock_every_launch") and not prev_every_launch
        if just_every_launch:
            st = _load_json(STATE_PATH, {})
            st["access_tokens"] = []
            _save_json(STATE_PATH, st)
        if not _authed(request) or just_every_launch:
            tok = secrets.token_hex(16)
            _add_token(tok)
            if config.get("lock_every_launch"):
                resp.set_cookie(COOKIE_NAME, tok, httponly=True, samesite="lax")
            else:
                resp.set_cookie(COOKIE_NAME, tok, max_age=365 * 86400, httponly=True, samesite="lax")
    return resp


@app.get("/api/history")
def get_history():
    return history


@app.get("/api/memory")
def get_memory():
    return {"soul": _read_md("SOUL.md"), "user": _read_md("USER.md"), "memory": _read_md("MEMORY.md")}


@app.get("/api/models")
async def list_models(profile_id: str):
    """从大脑服务商拉取可用模型列表（免费接口），供设置页下拉选择。"""
    profile = next((p for p in config["profiles"] if p["id"] == profile_id), None)
    if not profile or not profile.get("api_key"):
        return {"models": []}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                profile["base_url"].rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
            )
        if r.status_code != 200:
            return {"models": [], "error": f"服务商返回 {r.status_code}"}
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        return {"models": ids}
    except Exception as e:
        return {"models": [], "error": f"连接失败：{e}"}


@app.get("/image/{name}")
def get_image(name: str):
    p = (IMAGES_DIR / Path(name).name).resolve()
    if str(p).startswith(str(IMAGES_DIR.resolve())) and p.exists():
        return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------- 他发文件给她（2026.9.19）：上传落 data/files/，她用 read_file 读 ----------
# 入口是输入栏的 📎 按钮；txt/md/csv 直读，docx/pdf 由 documents.extract_text 抽文本。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
READ_CHUNK = 20000            # read_file 一次读的字数（2026.9.19 从 8000/12000 统一上调；更长分次读）
UPLOAD_KIND = {"docx": "Word 文档", "doc": "Word 文档", "pdf": "PDF 文档", "pptx": "PPT 演示",
               "txt": "文本文件", "md": "文本文件", "csv": "表格", "json": "JSON", "log": "日志"}


def _file_meta(name: str) -> dict:
    p = UPLOADS_DIR / Path(name).name
    sfx = p.suffix.lower().lstrip(".")
    return {"name": p.name, "kind": sfx, "size": p.stat().st_size if p.exists() else 0,
            "label": UPLOAD_KIND.get(sfx, "文件")}


@app.post("/api/upload")
async def upload_file(request: Request):
    """ multipart 直传（发文件按钮）。存 data/files/，撞名加序号，20MB 封顶。"""
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", None):
        return JSONResponse({"error": "没收到文件"}, status_code=400)
    raw = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(up.filename or "")).strip().strip(".")[:80]
    raw = Path(raw).name                       # 清洗完再取 basename，双保险
    stem = (Path(raw).stem or "").strip()[:60] or "未命名"
    sfx = Path(raw).suffix.lower()[:10]
    data = await up.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "文件超过 20MB，先不传了"}, status_code=400)
    if not sfx or sfx.lstrip(".") not in UPLOAD_KIND:
        sfx = ".txt"   # 没后缀/不认识的按 txt 存（照样能直读）
    cand = UPLOADS_DIR / f"{stem}{sfx}"
    n = 2
    while cand.exists():
        cand = UPLOADS_DIR / f"{stem}({n}){sfx}"
        n += 1
    cand.write_bytes(data)
    return _file_meta(cand.name)


@app.get("/file/{name}")
def get_upload(name: str):
    """他发过的文件的原样下载（文件卡片点击）。只准从 data/files/ 里取。"""
    p = (UPLOADS_DIR / Path(name).name).resolve()
    if str(p).startswith(str(UPLOADS_DIR.resolve())) and p.is_file():
        return FileResponse(p, filename=p.name)
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------- 平台打码（输出过滤）检测：一次重说 + 兜底清星号 + 计数 ----------
# DeepSeek 服务端会把输出里撞上敏感词的部分吃掉，残留 ** 星号（2026.9.2 实测6%消息触发）。
# 策略：检测到残留 → 让她把那几句换个说法重说一次（只重一次，绝不循环）；
#       重说还打码 → 用重说版但把星号清成省略号交付。计数记在 state.json 供用户查看过滤频率。
CENSOR_RE = re.compile(r"[*＊]{2,}")


def _bump_censor(key: str):
    try:
        st = _load_json(STATE_PATH, {})
        c = st.get("censor") or {}
        c[key] = int(c.get(key, 0)) + 1
        st["censor"] = c
        _save_json(STATE_PATH, st)
    except Exception:
        pass


async def revise_censored(client, call_model, call_base, call_key, reply: str, user_msg: str = "") -> str:
    """返回应展示的最终回复。无打码=原样；重说成功=重说版；重说仍打码=清星号兜底版。"""
    try:
        if not reply or not CENSOR_RE.search(reply):
            return reply
        _bump_censor("hits")
        ctx = ("他刚才对她说：" + user_msg[:200] + "\n\n") if user_msg else ""
        r = await client.post(
            call_base.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {call_key}"},
            json={"model": call_model, "max_tokens": 2048, "thinking": {"type": "disabled"},
                  "messages": [{"role": "user", "content": (
                      ctx +
                      "她给他的回复被系统审核截断了几处（** 是被吃掉的词留下的空位）。"
                      "请她把这条回复完整重说一遍：空位处用意思相近的别的词补上，整句可以换个说法绕开审核，"
                      "其余内容保持原样。你的输出里绝对不允许出现 * 号。直接输出修正后的整条回复，"
                      "不要解释、不要前言。她的原话：\n\n" + reply)}]},
        )
        r.raise_for_status()
        _tok("打码检查", r.json().get("usage") if r.status_code == 200 else None)
        revised = (r.json()["choices"][0]["message"]["content"] or "").strip()
        if revised and not CENSOR_RE.search(revised):
            return revised            # 重说干净，用它
        _bump_censor("retries_failed")
        if not revised:
            return reply              # 重说没产出：原样交付（9.9空产出被当失败清星号，咬伤过正文）
        return CENSOR_RE.sub("……", revised)   # 兜底：重说了还打码，星号清成省略号
    except Exception:
        return reply                  # 重说失败不拦聊天，原样交付


@app.post("/api/chat")
async def chat(request: Request):
    global history
    body = await request.json()
    message = (body.get("message") or "").strip()
    raw_images = (body.get("images") or [])[:MAX_IMAGES_PER_MESSAGE]
    raw_files = [Path(str(n)).name for n in (body.get("files") or []) if str(n).strip()][:3]
    raw_files = [n for n in raw_files if (UPLOADS_DIR / n).is_file()]
    if not message and not raw_images and not raw_files:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    # 图片落盘（data/images/），历史里只存文件名，history.json 不会膨胀
    stored = []
    for i, durl in enumerate(raw_images):
        try:
            meta, b64 = durl.split(",", 1)
            ext = "png" if "png" in meta.lower() else "jpg"
            name = f"img_{int(time.time() * 1000)}_{i}.{ext}"
            (IMAGES_DIR / name).write_bytes(base64.b64decode(b64))
            stored.append(name)
        except Exception:
            continue

    entry = {"role": "user", "content": message or ("（文件）" if raw_files else "（图片）"), "ts": time.time()}
    if stored:
        entry["images"] = stored
    if raw_files:
        entry["files"] = [_file_meta(n) for n in raw_files]
    history.append(entry)
    _save_json(HISTORY_PATH, history)

    # 记录活跃时间（供"隔久冒泡"和随机刻判断；挂机本身不消耗任何 token）
    state = _load_json(STATE_PATH, {})
    state["last_active_ts"] = entry["ts"]
    _save_json(STATE_PATH, state)

    # 晚安粘性窗（20260929a）：他亲口说晚安才起/续 30 分钟窗，窗内再聊不打断晚安判定
    try:
        _jiwen().note_user_message(entry["content"], now=entry["ts"])
        _jiwen_persist()
    except Exception:
        pass

    profile = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
    problems = []
    if not profile:
        problems.append("还没有可用的大脑，去右上角设置里添加一个")
    elif not profile.get("api_key"):
        problems.append(f"「{profile['name']}」还没填 API Key，去设置里填上再聊")
    if problems:

        async def config_error():
            yield sse({"type": "error", "message": "；".join(problems)})

        return StreamingResponse(config_error(), media_type="text/event-stream")

    # 【2026.9.2 RAG收窄·T5】自动沉淀停用（★故事污染源）：MEMORY.md 从此只收她手写的（记：）。
    # 她的长期记忆改由书记员账本（碎片→叙事星座）承担，聊天注入走 scribe_recall。
    # 回滚点：取消下一行注释即恢复旧行为。
    # asyncio.create_task(summarize_memories(profile))
    asyncio.create_task(_summary_experiment(profile))   # 旁路实验：只存档不注入（交底Q2）

    # 联网搜索和联想改写并行跑，省等待；改写失败自动回退原话
    sc = config.get("search") or {}

    async def _do_search_ctx() -> str:
        if sc.get("api_key") and wants_search(message):
            results, ok = await do_search(sc, message)
            return format_search_context(message, results, ok)
        return ""

    search_ctx, recall_q = await asyncio.gather(_do_search_ctx(), rewrite_recall_query(profile, message))
    messages, has_image = build_brain_messages(history[-MAX_CONTEXT_MESSAGES:], search_ctx, recall_q)
    if has_image:
        v = config.get("vision") or DEFAULT_CONFIG["vision"]
        call_model = (v.get("model") or DEFAULT_CONFIG["vision"]["model"]).strip()
        call_base = (v.get("base_url") or "").strip() or profile["base_url"]
        call_key = (v.get("api_key") or "").strip() or profile["api_key"]
    else:
        call_model, call_base, call_key = profile["model"], profile["base_url"], profile["api_key"]

    req_json = {"model": call_model, "stream": True, "stream_options": {"include_usage": True}}
    try:
        tv = float(profile.get("temperature") or "")
        if 0 <= tv <= 2:
            req_json["temperature"] = tv
    except ValueError:
        pass

    async def event_stream():
        """agent 循环：她想用工具就用（最多 3 轮），最后一轮强制纯聊天收尾。
        工具轮的思考过程不打扰他——前端只在打字气泡里换一句"她在查天气…"。"""
        reply = ""
        chat_usage = None   # 流式最后一个 chunk 带 usage（include_usage）
        tool_log = []  # 这轮用了哪些工具（给聊后复盘：值不值得沉淀成技能）
        files_this_turn = []  # 文档工坊产出（2026.9.19）：入 history + done 事件，前端渲染文件卡片

        def persist_reply():
            if reply:
                clean, mood, mems, scheds = extract_marks(reply)
                if mood:
                    save_mood(mood)
                for m in mems:
                    save_memory_line(m)
                for s in scheds:
                    sched_mod.add_from_chat(s, time.time())
                if mems:
                    # 她悄悄记下了什么——如果觉得特别，就顺手发条动态（有额度才发）
                    asyncio.create_task(maybe_generate_moment(profile, "她刚刚悄悄记下：" + "；".join(mems)))
                note_thought_leak(clean, "chat")
                entry = {"role": "assistant", "content": clean, "ts": time.time()}
                if files_this_turn:
                    entry["files"] = files_this_turn
                history.append(entry)
                _save_json(HISTORY_PATH, history)

        try:
            msgs = list(messages)
            async with httpx.AsyncClient(timeout=httpx.Timeout(300)) as client:
                for rnd in range(4):  # 3 轮可用工具 + 1 轮强制收尾
                    payload = dict(req_json)
                    payload["messages"] = msgs
                    if rnd < 3 and not has_image:
                        payload["tools"] = TOOLS_SCHEMA
                        payload["tool_choice"] = "auto"
                        # 文档工坊的整篇内容装在工具参数里（deepseek-flash 思考还吃预算）——工具轮给足
                        payload["max_tokens"] = 32768
                    tool_calls = {}  # index -> {id, name, args}
                    content = ""
                    async with client.stream(
                        "POST",
                        call_base.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {call_key}"},
                        json=payload,
                    ) as resp:
                        if resp.status_code != 200:
                            detail = (await resp.aread()).decode("utf-8", "ignore")[:500]
                            yield sse({"type": "error", "message": f"大脑返回 {resp.status_code}：{detail}"})
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                _chunk = json.loads(data)
                                if _chunk.get("usage"):
                                    chat_usage = _chunk["usage"]
                                delta = _chunk["choices"][0]["delta"]
                            except Exception:
                                continue
                            for c in delta.get("tool_calls") or []:
                                idx = c.get("index", 0)
                                slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                                if c.get("id"):
                                    slot["id"] = c["id"]
                                fn = c.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                slot["args"] += fn.get("arguments") or ""
                            text = delta.get("content")
                            if text:
                                content += text
                                if not tool_calls:
                                    reply += text
                                    yield sse({"type": "delta", "text": text})
                    _tok("聊天", chat_usage)
                    chat_usage = None
                    if not tool_calls:
                        break
                    # 她要用工具：先广播一句状态，执行完把结果递回去进入下一轮
                    calls_for_api, results = [], []
                    for idx in sorted(tool_calls):
                        slot = tool_calls[idx]
                        if not slot["name"]:
                            continue
                        if not slot["id"]:
                            slot["id"] = f"call_{rnd}_{idx}_{int(time.time() * 1000)}"
                        yield sse({"type": "tool", "label": TOOL_LABELS.get(slot["name"], "忙了一下")})
                        turn_files = []
                        res = await run_tool(slot["name"], slot["args"], turn_files)
                        files_this_turn.extend(turn_files)
                        tool_log.append({"name": slot["name"], "args": (slot["args"] or "")[:120], "result": res})
                        results.append(res)
                        calls_for_api.append({"id": slot["id"], "type": "function",
                                              "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}})
                    if not calls_for_api:
                        break
                    msgs.append({"role": "assistant", "content": content or "", "tool_calls": calls_for_api})
                    for call, res in zip(calls_for_api, results):
                        msgs.append({"role": "tool", "tool_call_id": call["id"], "content": res})
                # 平台打码检测：一次重说（只重一次，绝不循环）；前端收到 revise 事件会把气泡整个换掉
                revised = await revise_censored(client, call_model, call_base, call_key, reply, message)
                if revised != reply:
                    reply = revised
                    yield sse({"type": "revise", "text": reply})
        except Exception as e:
            persist_reply()
            yield sse({"type": "error", "message": f"连接大脑失败：{e}"})
            return
        persist_reply()
        # 日程结算（2026.9.25f）：这条回复带过的嘴打标（早嘴/兜底嘴/晚间好奇）、
        # 叫过之后他回来了的静默销账——跟 build_system_prompt 的注入同一套口径重算
        try:
            _lt = time.localtime()
            _today0 = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
            _first_today = sum(1 for m in history[-200:]
                               if m.get("role") == "user" and m.get("ts", 0) >= _today0) == 1
            sched_mod.settle_on_exchange(time.time(), _first_today)
        except Exception:
            pass
        # 聊后复盘（后台静默）：她用工具干完活了，值得沉淀技能吗？
        if tool_log and reply:
            asyncio.create_task(maybe_extract_skill(profile, message, reply, tool_log))
        asyncio.create_task(_jiwen_after_chat(profile))   # 积温：他的话怎么冲击了她
        yield sse({"type": "done", "files": files_this_turn})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ============================================================
# 书记员（Scribe）—— 从聊天记录中提取记忆碎片
# 每 50 条新消息 或 每 2 小时自动触发一次
# 6条评审补丁已融入
# ============================================================

@app.on_event("startup")
async def start_scribe_timer():
    """启动书记员定时器：首次2分钟后，之后每2小时一次（抽碎片 → 编叙事，链式）。
    测试实例禁用：起服务前设环境变量 SCRIBE_DISABLE=1（8001 调试用，别跟着生产实例一起烧 key）。"""
    if os.environ.get("SCRIBE_DISABLE"):
        return
    async def _tick():
        while True:
            await asyncio.sleep(SCRIBE_INTERVAL)
            await run_scribe()
            await run_consolidation(max_n=2)
    async def _first():
        await asyncio.sleep(120)
        await run_scribe()
        await run_consolidation(max_n=2)
        await _tick()
    asyncio.create_task(_first())


@app.post("/api/scribe/run")
async def api_scribe_run():
    """手动触发书记员（测试用）。"""
    await run_scribe()
    db = _load_fragments_db()
    return {"fragments_count": len(db["fragments"]), "cursor": db["cursor"]}


@app.post("/api/scribe/consolidate")
async def api_scribe_consolidate():
    """手动批量初编（存量材料池过四闸，一轮最多8条叙事；多跑几轮吃完全部材料）。
    force=True 绕过降耗闸——手动点名要编就全量编。"""
    result = await run_consolidation(max_n=8, force=True)
    db = _load_fragments_db()
    return {**result,
            "fragments_total": len(db.get("fragments", [])),
            "narratives_total": len([n for n in db.get("narratives", []) if not n.get("superseded")]),
            "unconsolidated": len([f for f in db.get("fragments", []) if not f.get("consolidated")])}


# ---------- 星图手动编辑（2026.9.11）：他巡检账本的扫帚，全走门锁 ----------
# 设计稿见 整合记忆体\任务规划\任务指令-2026.9.11-星图手动编辑-设计稿待审.md

@app.patch("/api/fragments/{fid}")
async def api_fragment_edit(fid: str, request: Request):
    """改碎片正文：写 edited 留痕，引用它的在世星座自动标待重编。"""
    body = await request.json()
    res = edit_fragment(fid, body.get("text") or "", body.get("note") or "")
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/fragments/{fid}/retire")
async def api_fragment_retire(fid: str, request: Request):
    """软删碎片（不注入不重编，数据保留可恢复）。"""
    body = await request.json() if await request.body() else {}
    res = retire_fragment(fid, (body or {}).get("note") or "")
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/fragments/{fid}/restore")
async def api_fragment_restore(fid: str):
    """恢复被判废的碎片。"""
    res = restore_fragment(fid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/narratives/{nid}/void")
async def api_narrative_void(nid: str):
    """手动作废星座：源碎片解除退役回池，等书记员重编。"""
    res = void_narrative(nid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/narratives/{nid}/reweave")
async def api_narrative_reweave(nid: str):
    """立即重编：作废旧星座（源碎片回池）+ 后台绕降耗闸强制重编一轮。"""
    res = void_narrative(nid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    asyncio.create_task(_force_reweave())
    return {**res, "reweave_started": True}


@app.post("/api/narratives/{nid}/restore")
async def api_narrative_restore(nid: str):
    """恢复被作废的星座（2026.9.11 用户反馈：作废后没有取消选项）。"""
    res = restore_narrative(nid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@app.post("/api/narratives/merge")
async def api_narratives_merge(request: Request):
    """合并讲同一件事的几个星座：全部作废+源碎片回池+后台 force 重编整合（2026.9.11）。"""
    body = await request.json()
    res = merge_narratives(body.get("ids") or [])
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    asyncio.create_task(_force_reweave())
    return {**res, "reweave_started": True}


@app.get("/api/tokens")
def token_stats():
    """本地 token 记账（按天/按用途，2026.9.3 起）。浏览器直接开这个地址就能看。"""
    db = _load_json(TOKEN_LOG_PATH, {})
    days = sorted(db.keys(), reverse=True)[:7]
    lines = []
    for d in days:
        v = db[d]
        lines.append(f"{d}：共 {v['in'] + v['out']}（输入{v['in']} / 输出{v['out']}）· {v['calls']} 次调用")
        for p, b in sorted(v.get("by", {}).items(), key=lambda x: -(x[1]["in"] + x[1]["out"])):
            lines.append(f"　{p}：{b['in'] + b['out']}（入{b['in']} 出{b['out']}）· {b['calls']} 次")
    return {"summary": ("\n".join(lines) if lines else "还没有记录"), "days": {d: db[d] for d in days}}


@app.get("/api/scribe/status")
async def api_scribe_status():
    """书记员状态（测试用）。"""
    db = _load_fragments_db()
    return {
        "cursor": db["cursor"],
        "history_length": len(history),
        "pending": max(0, len(history) - db["cursor"]),
        "fragments_count": len(db["fragments"]),
        "entities_count": len(db.get("entities", [])),
        "running": scribe_running(),
    }
