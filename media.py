# -*- coding: utf-8 -*-
"""media.py —— 外围外观 API（2026.9.10 拆分第二刀，自 server.py 移出）：
出门网址 / 头像 / 壁纸 / 他的名片与近况 / 签名池 / 统计 / 心情 / 音乐与点赞 /
日程 / 动态读取 / 星图数据（memory-graph）。全是薄路由，纯数据读写，
不依赖 server 内部——生成类逻辑（发动态/读书/聊天）仍在 server.py。
改这些接口来这里；路由挂在 router 上由 server.py include。"""
import base64
import re
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

import schedule as sched_mod
from common import (BG_DIR, CONFIG_PATH, DATA_DIR, LIKES_PATH, LINKS_PATH, MOMENTS_PATH,
                    MUSIC_DIR, SCHEDULE_PATH, SIGNATURES_PATH, SOUL_DIR, STATE_PATH,
                    DEFAULT_SIGNATURES, _load_json, _read_md, _save_json, history)
from memory import (_DATE_RE, _infer_emotion_weight, _load_fragments_db,
                    _parse_memory_ts, memory_entries, sync_memory_index)

router = APIRouter()


@router.get("/api/out-url")
def out_url():
    """当前出门隧道网址（启动她.bat 会把今天的网址写到这里），设置页展示用。"""
    p = Path.home() / ".zcode" / "tools" / "out-url.txt"
    if p.exists():
        u = p.read_text(encoding="utf-8", errors="ignore").strip()
        if u.startswith("http"):
            return {"url": u}
    return {"url": ""}


# ---------- 头像 / 壁纸 / 心情 / 音乐 / 日程 ----------

@router.get("/avatar")
def get_avatar():
    """她的头像：data/she.jpg，换头像直接覆盖这个文件。"""
    for name in ("she.jpg",):
        p = DATA_DIR / name
        if p.exists():
            return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)


async def _save_dataurl_image(request: Request, filename: str):
    body = await request.json()
    durl = body.get("image") or ""
    try:
        _, b64 = durl.split(",", 1)
        (DATA_DIR / filename).write_bytes(base64.b64decode(b64))
        return {"ok": True}
    except Exception:
        return JSONResponse({"error": "图片格式不对"}, status_code=400)


@router.get("/me/avatar")
def get_me_avatar():
    p = DATA_DIR / "me.jpg"
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)


@router.post("/api/me/avatar")
async def set_me_avatar(request: Request):
    return await _save_dataurl_image(request, "me.jpg")


@router.post("/api/her/avatar")
async def set_her_avatar(request: Request):
    return await _save_dataurl_image(request, "she.jpg")


@router.get("/background")
def get_background():
    """当前生效的壁纸。"""
    st = _load_json(STATE_PATH, {})
    name = st.get("bg") or ""
    if name:
        p = BG_DIR / Path(name).name
        if p.exists():
            return FileResponse(p)
    old = DATA_DIR / "bg.jpg"  # 兼容旧版单图
    if old.exists():
        return FileResponse(old)
    return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/backgrounds/{name}")
def get_background_file(name: str):
    p = (BG_DIR / Path(name).name).resolve()
    if str(p).startswith(str(BG_DIR.resolve())) and p.exists():
        return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/api/backgrounds")
def list_backgrounds():
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(f.name for f in BG_DIR.iterdir() if f.suffix.lower() in exts)
    st = _load_json(STATE_PATH, {})
    return {"backgrounds": files, "active": st.get("bg") or ""}


@router.post("/api/background")
async def set_background(request: Request):
    """壁纸库：{image} 上传新图 / {name} 选已有 / {clear} 恢复默认。"""
    body = await request.json()
    st = _load_json(STATE_PATH, {})
    if body.get("clear"):
        st["bg"] = ""
        _save_json(STATE_PATH, st)
        return {"ok": True, "cleared": True}
    if body.get("name"):
        name = Path(body["name"]).name
        if (BG_DIR / name).exists():
            st["bg"] = name
            _save_json(STATE_PATH, st)
            return {"ok": True, "name": name}
        return JSONResponse({"error": "没有这张壁纸"}, status_code=404)
    durl = body.get("image") or ""
    try:
        _, b64 = durl.split(",", 1)
        name = f"bg_{int(time.time() * 1000)}.jpg"
        (BG_DIR / name).write_bytes(base64.b64decode(b64))
        st["bg"] = name
        _save_json(STATE_PATH, st)
        return {"ok": True, "name": name}
    except Exception:
        return JSONResponse({"error": "图片格式不对"}, status_code=400)


@router.get("/api/me")
def get_me():
    """他的名片（近况他在⚡卡里更新；名字/头衔读 data/config.json 的 me_name/me_title
    ——2026.9.26 去硬编码：分享版空房默认中性，本机在 config.json 里配置）。"""
    st = _load_json(STATE_PATH, {})
    cfg = _load_json(CONFIG_PATH, {})
    np = st.get("now_playing") or {}
    listening = np.get("name") if np.get("name") and time.time() - np.get("ts", 0) < 3 * 3600 else ""
    myst = st.get("my_status") or {}
    return {
        "name": cfg.get("me_name") or "主人",
        "title": cfg.get("me_title") or "这座港口的主人",
        "listening": listening,
        "my_status": {
            "mood": myst.get("mood", ""),
            "doing": myst.get("doing", ""),
            "note": myst.get("note", ""),
            "ts": myst.get("ts", 0),
        },
    }


@router.post("/api/me/status")
async def set_my_status(request: Request):
    """他更新自己的近况（心情/在忙/想说的话），她每次对话都能看到。"""
    body = await request.json()
    st = _load_json(STATE_PATH, {})
    st["my_status"] = {
        "mood": (body.get("mood") or "").strip()[:30],
        "doing": (body.get("doing") or "").strip()[:60],
        "note": (body.get("note") or "").strip()[:120],
        "ts": time.time(),
    }
    _save_json(STATE_PATH, st)
    return {"ok": True}


@router.get("/api/signatures")
def get_signatures():
    """个性签名池：data/signatures.json，可自行增删。"""
    if not SIGNATURES_PATH.exists():
        _save_json(SIGNATURES_PATH, DEFAULT_SIGNATURES)
    sigs = _load_json(SIGNATURES_PATH, DEFAULT_SIGNATURES)
    if not isinstance(sigs, list) or not sigs:
        sigs = DEFAULT_SIGNATURES
    return {"signatures": [str(s) for s in sigs]}


@router.get("/api/stats")
def get_stats():
    """状态卡的统计：相伴天数（从 history 最早一条算起——2026.9.26 去硬编码纪念日，
    本机口径不变；分享版自动从对方的第一句聊天起算）、今日/累计消息、记忆、动态、点过赞的歌。"""
    t = time.localtime()
    today_start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    today_msgs = sum(1 for m in history if m.get("ts", 0) >= today_start)
    first_ts = min((m.get("ts", 0) for m in history if m.get("ts")), default=0)
    return {
        "days": (int((time.time() - first_ts) / 86400) + 1) if first_ts else 1,
        "today": today_msgs,
        "messages": len(history),
        "memories": len(memory_entries()),
        "moments": len(_load_json(MOMENTS_PATH, [])),
        "songs": len(_load_json(LIKES_PATH, [])),
    }


@router.get("/api/mood")
def get_mood():
    m = _load_json(STATE_PATH, {}).get("mood") or {}
    return m if m.get("label") else {}


@router.get("/api/music")
def list_music():
    exts = {".mp3", ".flac", ".ogg", ".wav", ".m4a"}
    files = sorted(f.name for f in MUSIC_DIR.iterdir() if f.suffix.lower() in exts)
    # likes（2026.9.25）：前端要给心形显示持久状态——哪首点过赞
    return {"songs": files, "links": _load_json(LINKS_PATH, []), "likes": _load_json(LIKES_PATH, [])}


AUDIO_EXT_RE = re.compile(r"\.(mp3|m4a|flac|wav|ogg|aac)([?#]|$)", re.I)


@router.post("/api/music/resolve")
async def resolve_music(request: Request):
    """粘贴网易云歌曲链接或音频直链 → 统一成可播放条目，存进链接歌单。
    其他网站链接她还没法播，直接拒绝，不存垃圾名字。"""
    body = await request.json()
    src = (body.get("url") or "").strip()
    custom_name = (body.get("name") or "").strip()[:60]
    if not src:
        return JSONResponse({"error": "链接为空"}, status_code=400)

    name, play_url = "", src
    m = re.search(r"(?:song\?id=|/song/)(\d+)", src)
    if m:
        song_id = m.group(1)
        play_url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"
        name = custom_name or f"网易云 #{song_id}"
        try:  # 尝试拿歌名（拿不到就用编号，不影响播放）
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"}) as client:
                r = await client.get(f"https://music.163.com/api/song/detail/?id={song_id}&ids=%5B{song_id}%5D")
            if r.status_code == 200:
                songs = r.json().get("songs") or []
                if songs:
                    artists = ",".join(a.get("name", "") for a in (songs[0].get("artists") or [])[:2])
                    name = custom_name or f"{songs[0].get('name', '')}" + (f" - {artists}" if artists else "")
        except Exception:
            pass
    else:
        # 音频直链必须以音频扩展名结尾，否则（YouTube 分享链等）尾巴全是跟踪参数，存进去就是乱码
        if not AUDIO_EXT_RE.search(src):
            return JSONResponse({"error": "这个链接她还没法播放：支持网易云歌曲链接，或 .mp3/.m4a/.flac/.wav/.ogg 结尾的音频直链"}, status_code=400)
        name = custom_name or src.split("?")[0].rsplit("/", 1)[-1][:60] or "自定义链接"

    links = _load_json(LINKS_PATH, [])
    links = [l for l in links if l.get("url") != play_url]
    links.append({"name": name, "url": play_url})
    _save_json(LINKS_PATH, links[-100:])
    return {"name": name, "url": play_url}


@router.get("/music/{name}")
def get_music(name: str):
    p = (MUSIC_DIR / Path(name).name).resolve()
    if str(p).startswith(str(MUSIC_DIR.resolve())) and p.exists():
        return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)


@router.post("/api/music/playing")
async def set_playing(request: Request):
    body = await request.json()
    st = _load_json(STATE_PATH, {})
    st["now_playing"] = {"name": (body.get("name") or "")[:80], "ts": time.time()}
    _save_json(STATE_PATH, st)
    return {"ok": True}


@router.post("/api/music/like")
async def like_song(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()[:80]
    on = bool(body.get("on", True))   # on=False 取消赞（2026.9.25）：心形再点一下撤回
    if not name:
        return JSONResponse({"error": "no name"}, status_code=400)
    likes = _load_json(LIKES_PATH, [])
    entry = f"他很喜欢《{name}》这首歌，反复听还点过赞"
    if on:
        if name not in likes:
            likes.append(name)
            _save_json(LIKES_PATH, likes[-50:])
        # 点赞直接写进她的记忆：她由此知道他喜欢什么歌（RAG 同步）
        if entry not in memory_entries():
            current = _read_md("MEMORY.md")
            (SOUL_DIR / "MEMORY.md").write_text((current + "\n§\n" + entry) if current else entry, encoding="utf-8")
            sync_memory_index()
    else:
        if name in likes:
            likes.remove(name)
            _save_json(LIKES_PATH, likes)
        # 取消赞撤掉当初那条模板记忆——只删一字不差的那条，聊天里另写的印象不碰（RAG 同步重建）
        if entry in memory_entries():
            raw = _read_md("MEMORY.md")
            for pat in (f"\n§\n{entry}", f"{entry}\n§\n", entry):
                if pat in raw:
                    raw = raw.replace(pat, "", 1)
                    break
            (SOUL_DIR / "MEMORY.md").write_text(raw.strip("\n"), encoding="utf-8")
            sync_memory_index()
    return {"ok": True, "liked": name in _load_json(LIKES_PATH, [])}


@router.get("/api/schedule")
def get_schedule():
    """手写 md（原样）+ 结构化日程条目（2026.9.25f：聊天里她记的（日程：）落 schedule.json；
    列表界面挪 UI 轮，现在先返回数据，AI 也能用接口帮清）。"""
    return {"text": SCHEDULE_PATH.read_text(encoding="utf-8") if SCHEDULE_PATH.exists() else "",
            "items": sched_mod.list_items()}


@router.post("/api/schedule")
async def save_schedule(request: Request):
    body = await request.json()
    SCHEDULE_PATH.write_text((body.get("text") or "")[:3000], encoding="utf-8")
    return {"ok": True}


@router.post("/api/schedule/items/{item_id}/done")
def schedule_item_done(item_id: str):
    """手动销账（UI 轮之前的口子：这事办完了，不用她再叫）。"""
    if sched_mod.mark_done(item_id):
        return {"ok": True}
    return JSONResponse({"error": "not found"}, status_code=404)


@router.delete("/api/schedule/items/{item_id}")
def schedule_item_delete(item_id: str):
    """手动删条目（整条清掉——日程账本与她的记忆完全分开，删了不影响任何记忆）。"""
    if sched_mod.delete_item(item_id):
        return {"ok": True}
    return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/api/moments")
def get_moments():
    ms = _load_json(MOMENTS_PATH, [])
    return list(reversed(ms[-50:]))


@router.get("/api/memory-graph")
def memory_graph():
    """返回记忆数据给前端画星图：MEMORY.md碎片 + memory_fragments.json碎片 + 实体星座 + 叙事。"""
    # 1. MEMORY.md 碎片（旧碎片）
    raw = _read_md("MEMORY.md")
    fragments = []
    for line in raw.split("§"):
        line = line.strip()
        if not line or line.startswith("## "):
            continue
        ts = _parse_memory_ts(line)
        ew = _infer_emotion_weight(line)
        dm = _DATE_RE.search(line)
        date_tag = f"{dm.group(1)}.{dm.group(2)}" if dm else ""
        content = _DATE_RE.sub("", line).strip()
        fragments.append({
            "text": content, "date": date_tag, "ts": ts, "ew": ew,
            "age_days": round((time.time() - ts) / 86400, 1) if ts > 0 else -1,
            "source": "memory_md",
        })
    # 2. memory_fragments.json 碎片（书记员账本：抄录✧ + 她的手记✦）
    db = _load_fragments_db()
    for f in db.get("fragments", []):
        ts = f.get("ts", 0)
        t = time.localtime(ts) if ts else None
        fragments.append({
            "id": f.get("id", ""),
            "text": f.get("text", ""),
            "date": f"{t.tm_mon}.{t.tm_mday}" if t else "",
            "ts": ts,
            "ew": f.get("emotion_weight", 0.3),
            "age_days": round((time.time() - ts) / 86400, 1) if ts > 0 else -1,
            "source": f.get("source", "scribe"),
            "type": f.get("type", "observation"),
            "entities": f.get("entities", []),
            "consolidated": bool(f.get("consolidated")),
            "retired": bool(f.get("retired")),
            "corrected": bool(f.get("corrected")),
            "edited": bool(f.get("edited")),
        })
    # 3. 叙事星座（2026.9.2 v2：星座=叙事，碎片不做可视化，只随星座卡片清单输出）
    frag_by_id = {f.get("id"): f for f in db.get("fragments", [])}
    narratives = []
    for n in db.get("narratives", []):
        if n.get("superseded"):
            continue
        src = []
        for fid in n.get("source_fragment_ids", []):
            f = frag_by_id.get(fid)
            if f:
                t = time.localtime(f.get("ts", 0)) if f.get("ts") else None
                src.append({
                    "id": fid, "text": f.get("text", ""),
                    "date": f"{t.tm_mon}.{t.tm_mday}" if t else "",
                    "ew": f.get("emotion_weight", 0.3),
                })
        narratives.append({
            "id": n.get("id", ""), "title": n.get("title", ""), "galaxy": n.get("galaxy", ""),
            "entities": n.get("entities", []), "text": n.get("text", ""),
            "significance": n.get("significance", 5), "ew": n.get("ew", 0.3),
            "created": n.get("created", ""), "source_fragment_ids": n.get("source_fragment_ids", []),
            "fragments": src,
            "manual_stale": bool(n.get("manual_stale")),
        })
    # 4. 实体聚合（旧字段保留，正式星图不再消费）
    entity_map = {}
    for f in fragments:
        for ent in f.get("entities", []):
            if ent not in entity_map:
                entity_map[ent] = {"name": ent, "fragments": [], "count": 0}
            entity_map[ent]["fragments"].append(f["text"][:80])
            entity_map[ent]["count"] += 1
    constellations = sorted(entity_map.values(), key=lambda x: -x["count"])
    # 5. 已作废星座（2026.9.11 恢复入口用）：手动作废/合并退役的，星图不画、可反悔
    voided = [{"id": n.get("id", ""), "title": n.get("title", ""), "galaxy": n.get("galaxy", ""),
               "voided_by": n.get("voided_by", ""),
               "fragments": len(n.get("source_fragment_ids") or [])}
              for n in db.get("narratives", [])
              if n.get("superseded") and n.get("voided_by") in ("manual", "merge")]
    return {"fragments": fragments, "narratives": narratives, "constellations": constellations,
            "voided": voided}
