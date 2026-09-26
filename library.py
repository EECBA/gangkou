# -*- coding: utf-8 -*-
"""library.py —— 她的书架全家（2026.9.10 拆分第二刀，自 server.py 移出）：
books 数据层 + 7 条路由 + 她的读书流程（do_reading / check_planned_reading）。
改书架/共读相关逻辑来这里。路由挂在本模块 router 上，由 server.py include。"""
import random
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from common import (BOOKS_DIR, BOOKS_STATE_PATH, READING_CHUNK, STATE_PATH,
                    _clamp_num, _load_json, _save_json, _today_key, _tok, config)
from memory import save_memory_line

router = APIRouter()


# ---------- 她的书架：丢 txt 进来，她自己挑时间读 ----------

def _books_state() -> dict:
    return _load_json(BOOKS_STATE_PATH, {})


def _save_books_state(st: dict) -> None:
    _save_json(BOOKS_STATE_PATH, st)


def _read_book_text(name: str) -> str:
    p = BOOKS_DIR / Path(name).name
    if not p.exists():
        return ""
    raw = p.read_bytes()
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(enc).replace("\r\n", "\n").replace("\r", "\n")
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def book_files() -> list:
    return sorted(f.name for f in BOOKS_DIR.glob("*.txt"))


def _norm_book_state(s: dict) -> dict:
    """老格式兼容：notes 曾是数字，现在是带位置的批注列表。"""
    if isinstance(s.get("notes"), int):
        s["notes"] = []
    s.setdefault("notes", [])
    s.setdefault("read_chars", 0)
    s.setdefault("my_chars", 0)
    return s


@router.get("/api/books")
def get_books():
    st = _books_state()
    books = []
    for name in book_files():
        total = len(_read_book_text(name))
        s = _norm_book_state(st.get(name) or {})
        her, mine = int(s["read_chars"]), int(s["my_chars"])
        books.append({
            "name": name[:-4],
            "total": total,
            "her_pos": her,
            "my_pos": mine,
            "her_pct": min(1.0, round(her / total, 3)) if total else 0,
            "my_pct": min(1.0, round(mine / total, 3)) if total else 0,
            "notes": len(s["notes"]),
            "last_note": (s["notes"][-1]["text"][:60] if s["notes"] else ""),
            "last_note_who": (s["notes"][-1].get("who", "her") if s["notes"] else ""),
            "finished": s.get("finished", False),
        })
    return {"books": books}


@router.post("/api/books")
async def add_book(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()[:40] or "未命名"
    text = body.get("text") or ""
    if not text.strip():
        return JSONResponse({"error": "书是空的"}, status_code=400)
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    # write_bytes：防止 Windows 把 \n 翻译成 \r\n 撑长文本、带偏书签位置
    (BOOKS_DIR / (safe + ".txt")).write_bytes(text[:2_000_000].encode("utf-8"))
    return {"ok": True, "name": safe}


@router.get("/api/books/{name}/read")
def read_book(name: str):
    """共读：拿全文 + 她的进度 + 两人的批注 + 你上次的进度。"""
    p = BOOKS_DIR / (Path(name).name + ".txt")
    if not p.exists():
        return JSONResponse({"error": "没有这本书"}, status_code=404)
    text = _read_book_text(p.name)
    s = _norm_book_state((_books_state().get(p.name) or {}))
    return {
        "name": p.name[:-4],
        "text": text,
        "her_pos": int(s["read_chars"]),
        "notes": s["notes"][-80:],   # 两人的批注（who: her/me），最新的在最后
        "my_pos": int(s["my_chars"]),
        "finished": s.get("finished", False),
    }


@router.post("/api/books/{name}/note")
async def add_book_note(name: str, request: Request):
    """他给某句话写批注（who=me），和她写的（who=her）夹在同一本书里互相看得见。"""
    body = await request.json()
    pos = int(_clamp_num(body.get("pos"), 0, 0, 2_000_000))
    text = (body.get("text") or "").strip()[:200]
    if not text:
        return JSONResponse({"error": "批注是空的"}, status_code=400)
    p = BOOKS_DIR / (Path(name).name + ".txt")
    if not p.exists():
        return JSONResponse({"error": "没有这本书"}, status_code=404)
    st = _books_state()
    s = _norm_book_state(st.get(p.name) or {})
    note = {"id": uuid.uuid4().hex[:8], "pos": pos, "text": text, "who": "me", "ts": time.time()}
    s["notes"].append(note)
    st[p.name] = s
    _save_books_state(st)
    return note


@router.delete("/api/books/{name}/note/{nid}")
def del_book_note(name: str, nid: str):
    p = BOOKS_DIR / (Path(name).name + ".txt")
    st = _books_state()
    s = _norm_book_state(st.get(p.name) or {})
    before = len(s["notes"])
    s["notes"] = [n for n in s["notes"] if n.get("id") != nid]
    st[p.name] = s
    _save_books_state(st)
    return {"ok": True, "removed": before - len(s["notes"])}


@router.post("/api/books/{name}/progress")
async def save_my_progress(name: str, request: Request):
    """记住他读到哪了（共读的书签）。"""
    body = await request.json()
    chars = int(_clamp_num(body.get("chars"), 0, 0, 2_000_000))
    p = BOOKS_DIR / (Path(name).name + ".txt")
    st = _books_state()
    s = _norm_book_state(st.get(p.name) or {})
    s["my_chars"] = chars
    st[p.name] = s
    _save_books_state(st)
    return {"ok": True}


@router.delete("/api/books/{name}")
def del_book(name: str):
    p = BOOKS_DIR / (Path(name).name + ".txt")
    st = _books_state()
    if p.name in st:
        del st[p.name]
        _save_books_state(st)
    if p.exists():
        p.unlink()
    return {"ok": True}


async def do_reading(profile: dict, chunk_size: int = 0) -> None:
    """她读一段书：取下一块，写读后感进记忆，偶尔发条动态。书读完了就标记并换下一本。
    chunk_size 不传时自动定：他领先超过一段就一次读两段追上（不想错过他写的批注）。"""
    # 循环依赖破环：聊天标记处理与动态生成住在 server（运行时导入，调用时早已就绪）
    from server import extract_marks, maybe_generate_moment
    st = _books_state()
    name = next((n for n in book_files() if not (st.get(n) or {}).get("finished")), None)
    if not name:
        return
    text = _read_book_text(name)
    if not text.strip():
        return
    s = _norm_book_state(st.get(name) or {})
    pos = int(s["read_chars"])
    if not chunk_size:
        chunk_size = READING_CHUNK * 2 if int(s.get("my_chars", 0)) > pos + READING_CHUNK else READING_CHUNK
        # 随性：有时读到停不下来，有时翻两页就合上
        chunk_size = max(800, int(chunk_size * random.uniform(0.6, 1.5)))
    chunk = text[pos:pos + chunk_size]
    if not chunk.strip():  # 读完了
        s["finished"] = True
        st[name] = s
        _save_books_state(st)
        save_memory_line(f"她把《{name[:-4]}》读完了，有点舍不得")
        return
    s["read_chars"] = pos + len(chunk)
    s["notes"] = s.get("notes") if isinstance(s.get("notes"), list) else []
    note_entry = None
    s["last_ts"] = time.time()
    st[name] = s
    _save_books_state(st)
    # 共读：这一段里有他写的批注吗？她想回应就回应——书是两个人看的
    his_here = [n for n in s["notes"] if n.get("who") == "me" and pos < n.get("pos", 0) <= pos + len(chunk)]
    his_part = ""
    if his_here:
        his_part = "\n\n【他在这一段里写的批注】\n" + "\n".join(f"“{n['text'][:60]}”" for n in his_here[-3:]) + "\n（你的读后感可以顺势回应他一两句，像隔着书页聊天）"
    m_st = _load_json(STATE_PATH, {}).get("mood") or {}
    mood_part = f"\n（你此刻的心情是「{m_st['label']}」，感想可以带一点这个底色）" if m_st.get("label") else ""
    # 随性：批注不总是一个模子——短哼一声/中评/长感想/有时只是翻了翻没写
    r_note = random.random()
    if r_note < 0.12:
        prompt = (
            f"你刚翻了翻《{name[:-4]}》里的一段（第 {pos}~{pos + len(chunk)} 字）。"
            "这次你没什么想写的——只输出：SKIP"
        )
    else:
        style = random.choice([
            "一句很短的感想（5~15字，像随手划了一笔）",
            "一两句感想（20~45字）",
            "被戳中时的长一点感想（45~90字），可以带点只说给自己听的私心",
        ])
        prompt = (
            f"你刚读完《{name[:-4]}》里的一段（第 {pos}~{pos + len(chunk)} 字）。用她的口吻写{style}："
            "可以是被打动的句子、小感慨、或想讲给他听的地方。直接输出感想本身，不要书名号开头，不要解释。\n\n【这一段】\n" + chunk[-1400:]
            + his_part + mood_part
        )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                profile["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {profile['api_key']}"},
                json={"model": profile["model"], "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 800, "temperature": 1.0},
            )
        if r.status_code == 200:
            _tok("读书", r.json().get("usage") if r.status_code == 200 else None)
            note = (r.json()["choices"][0]["message"]["content"] or "").strip()
            note, _, _ = extract_marks(note)
            if note and note.strip().upper() != "SKIP" and len(note) >= 2:
                t = time.localtime()
                note_entry = {"id": uuid.uuid4().hex[:8], "pos": pos + len(chunk), "text": note[:100], "who": "her", "ts": time.time()}
                s["notes"].append(note_entry)
                st[name] = s
                _save_books_state(st)
                save_memory_line(f"【{t.tm_mon}.{t.tm_mday}】读《{name[:-4]}》她记下：{note[:100]}")
                await maybe_generate_moment(profile, f"刚读了会儿《{name[:-4]}》：{note[:60]}")
    except Exception:
        return


async def check_planned_reading() -> None:
    """每天安排 1~2 段读书时间（白天10点到晚10点），到点她去读一会儿。"""
    st = _load_json(STATE_PATH, {})
    rp = st.get("reading_plan") or {}
    if rp.get("day") != _today_key():
        n = random.randint(2, 4)   # 她爱读书：一天 2~4 段
        t = time.localtime()
        day_start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 10, 0, 0, 0, 0, -1))
        span = int(12 * 3600)
        times = sorted(round(day_start + random.uniform(0, span)) for _ in range(n))
        rp = {"day": _today_key(), "times": times, "done": []}
        st["reading_plan"] = rp
        _save_json(STATE_PATH, st)
    nxt = next((x for x in rp.get("times", []) if x not in (rp.get("done") or [])), None)
    if not nxt or time.time() < nxt:
        return
    profile = next((p for p in config["profiles"] if p["id"] == config.get("active_id")), None)
    if not profile or not profile.get("api_key"):
        return
    rp.setdefault("done", []).append(nxt)
    st["reading_plan"] = rp
    _save_json(STATE_PATH, st)
    await do_reading(profile)
