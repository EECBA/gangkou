# -*- coding: utf-8 -*-
"""日程闭环（2026.9.25f，任务指令-2026.9.24 第二件·代码层，UI 挪后续轮）。

体感：他聊天里说"今天下午两点提醒我拿快递"→她答应并在回复末尾写
（日程：时间 事项）系统通道→ server 剥离交到这里→ 解析成绝对时刻落
data/schedule.json（独立账本，不进 MEMORY/RAG/书记员——日程是待办不是回忆）
→ 到点前 15 分钟她主动叫（due）→ 没回应 10 分钟后追叫（re）→ 还没回应，
他下次发消息时她兜底带一嘴（mention，说完即销）→ 销账后当晚 19~22 点他
再说话，她可以好奇问一句办完没（followup，只一次）。

防轰炸的根本（用户拍板）：**不判时间，数次数**——每条日程每种接触各最多一次
（morning 每天一次），结构上不可能"过期时间反复提醒"。销账条目留 30 天自动清档。

口径（2026.9.25 grilling 拍板）：
- 早嘴绑"他当天第一次开口"（5 点后），不绑闹钟；只提"昨天及更早记下的、
  还没到点的、24 小时内到期的"——今天刚记的不提（刚说完她答应过），没账零动静
- 叫过之后他的任何一条消息都算销账（不必说"知道了"）
- 日期已过/含糊：她该当场反问（提示词规则）；万一还是写了，解析不出就落
  due=null 的挂账条目（不静默吞、可见可修，永不触发）
- 关机错过：不补叫，走 mention 兜底路径补一嘴然后照销（诚实约束：她只在
  服务开着时能叫）
"""

import re
import time

from common import SCHEDULE_JSON_PATH, _load_json, _save_json

DUE_LEAD_S = 15 * 60        # 到点叫的提前量
RE_AFTER_S = 10 * 60        # 没回应多久后追叫
RE_WINDOW_S = 2 * 3600      # 追叫窗口（过了就交给 mention 兜底）
KEEP_DONE_S = 30 * 86400    # 销账条目保留期
MORNING_MIN_HOUR = 5        # 凌晨五六点前的那条不算"当天第一次开口"
FOLLOWUP_HOURS = (19, 22)   # 晚间好奇问一句的窗口
INJECT_CAP = 2              # 一条消息最多带几条日程嘴


# ---------- 账本 ----------

def _load_db() -> dict:
    db = _load_json(SCHEDULE_JSON_PATH, None)
    if not isinstance(db, dict) or not isinstance(db.get("items"), list):
        return {"seq": 0, "items": []}
    return db


def _save_db(db: dict) -> None:
    _save_json(SCHEDULE_JSON_PATH, db)


def list_items() -> list:
    return _load_db()["items"]


def get_item(item_id: str):
    return next((i for i in _load_db()["items"] if i.get("id") == item_id), None)


def has_touch(item: dict, kind: str) -> bool:
    return any(t.get("kind") == kind for t in item.get("touches", []))


def _touch_day(item: dict, kind: str):
    for t in item.get("touches", []):
        if t.get("kind") == kind:
            return time.strftime("%Y%m%d", time.localtime(t.get("ts", 0)))
    return None


# ---------- 中文时间解析（手写小型，不引库） ----------

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn2int(s: str):
    """中文/阿拉伯数字 → int（支持到 99：二十三、15）。认不出返回 None。"""
    s = s or ""
    if s.isdigit():
        return int(s)
    if not s:
        return None
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        if (not left or left in _CN_NUM) and (not right or right in _CN_NUM):
            return tens * 10 + ones
        return None
    return _CN_NUM.get(s)


_REL_HALF_RE = re.compile(r"([0-9一二两三四五六七八九十]+)个半(小时|分钟|分)后")
_REL_HALFONLY_RE = re.compile(r"半(小时|分钟)后")
_REL_RE = re.compile(r"([0-9一二两三四五六七八九十]+)(小时|分钟|分)后")
_DATE_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})[号日]")
_DATE_D_RE = re.compile(r"(\d{1,2})[号日]")
_WEEK_RE = re.compile(r"(?:星期|礼拜)([一二三四五六日天])")
_NEXTWEEK_RE = re.compile(r"下周([一二三四五六日天])")
_WEEKDAY_IDX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_DAY_WORDS = [("大后天", 3), ("后天", 2), ("明天", 1), ("明晚", 1), ("今晚", 0), ("今天", 0)]
_TIME_DIGITAL_RE = re.compile(r"(\d{1,2}):(\d{2})")
_TIME_CN_RE = re.compile(
    r"(凌晨|早上|清晨|上午|中午|下午|傍晚|晚上)?"
    r"([0-9一二两三四五六七八九十]+)点(半|一刻|三刻)?(\d{1,2}分)?")
_PERIOD_SHIFT = {"凌晨": 0, "早上": 0, "清晨": 0, "上午": 0,
                 "中午": 12, "下午": 12, "傍晚": 12, "晚上": 12}


def _local(ts: float):
    return time.localtime(ts)


def _mk(year, mon, day, hour, minute):
    return time.mktime((year, mon, day, hour, minute, 0, 0, 0, -1))


def parse_when(when: str, now_ts: float):
    """把『明天下午两点』这类说法解析成绝对时间戳。
    返回 (due_ts 或 None, note)：due_ts=None 时 note 说明原因（挂账不吞）。
    """
    t = re.sub(r"\s+", "", when or "")
    lt = _local(now_ts)
    today0 = _mk(lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0)

    # 1) 相对时长：两小时后 / 一个半小时后 / 半小时后 / 30分钟后
    m = _REL_HALF_RE.search(t)
    if m:
        n = _cn2int(m.group(1))
        if n is not None:
            return now_ts + (n + 0.5) * (3600 if m.group(2).startswith("小") else 60), ""
    if _REL_HALFONLY_RE.search(t):
        return now_ts + (1800 if "小时" in _REL_HALFONLY_RE.search(t).group(1) else 30), ""
    m = _REL_RE.search(t)
    if m:
        n = _cn2int(m.group(1))
        if n is not None:
            return now_ts + n * (3600 if m.group(2).startswith("小") else 60), ""

    # 2) 日期
    day_offset, y, mo, d = None, None, None, None
    for w, off in _DAY_WORDS:
        if w in t:
            day_offset = off
            break
    if day_offset is None:
        m = _DATE_MD_RE.search(t)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
    if day_offset is None and mo is None:
        m = _NEXTWEEK_RE.search(t)
        if m:
            wd = _WEEKDAY_IDX[m.group(1)]
            day_offset = 7 + (wd - lt.tm_wday) % 7   # 下周X：下周的那个 X（同曜日=+7）
    if day_offset is None and mo is None:
        m = _WEEK_RE.search(t)
        if m:
            wd = _WEEKDAY_IDX[m.group(1)]
            day_offset = (wd - lt.tm_wday) % 7   # 本周最近的前面那个（含今天）

    # 3) 时刻
    hour, minute = None, None
    m = _TIME_DIGITAL_RE.search(t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if "下午" in t or "傍晚" in t or "晚上" in t or "今晚" in t or "明晚" in t:
            if hour < 12:
                hour += 12
        elif "中午" in t and hour < 11:
            hour += 12
    else:
        m = _TIME_CN_RE.search(t)
        if m:
            period, h_raw, qua, mins = m.group(1), m.group(2), m.group(3), m.group(4)
            h = _cn2int(h_raw)
            if h is not None:
                if period == "中午":
                    hour = 12 if h in (12, 0) else (h + 12 if h < 11 else h)
                else:
                    hour = h + _PERIOD_SHIFT.get(period or "", 0)
                    if hour >= 24:
                        hour -= 24
                minute = 30 if qua == "半" else (15 if qua == "一刻" else (45 if qua == "三刻" else 0))
                if mins:
                    minute = int(mins[:-1])
                if (period or "") in ("下午", "傍晚", "晚上") and hour < 12:
                    hour += 12

    # 4) 组装
    default_time = 9 * 3600   # 只说了日期没说几点：默认上午 9 点
    if mo is not None:   # 显式 X月X号：今年的这个日期
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None, "日期本身不对（" + when + "）"
        due = _mk(lt.tm_year, mo, d, hour if hour is not None else 9, minute or 0)
        if due < now_ts:
            return None, "日期已经过了（" + when + "）——她该当场确认是不是下个月/明年"
    elif day_offset is not None:
        base = today0 + day_offset * 86400
        b = _local(base)
        if hour is None:
            due = _mk(b.tm_year, b.tm_mon, b.tm_mday, 9, 0)
        else:
            due = _mk(b.tm_year, b.tm_mon, b.tm_mday, hour, minute or 0)
        if due <= now_ts:
            if day_offset == 0:
                return None, "今天的这个时刻已经过了（" + when + "）"
            due += 7 * 86400   # 周几说今天但时刻已过 → 下周同一天（惯例默认）
    elif hour is not None:   # 只有时刻：今天
        due = _mk(lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute or 0)
        if due <= now_ts:
            return None, "今天的这个时刻已经过了（" + when + "）"
    else:
        return None, "没认出时间（" + when + "）"
    return due, ""


def humanize_due(due_ts: float) -> str:
    lt, now = _local(due_ts), _local(time.time())
    hm = time.strftime("%H:%M", lt)
    if lt.tm_yday == now.tm_yday and lt.tm_year == now.tm_year:
        return f"今天 {hm}"
    if lt.tm_yday == now.tm_yday + 1 and lt.tm_year == now.tm_year:
        return f"明天 {hm}"
    return time.strftime("%m月%d日", lt) + " " + hm


# ---------- 落账 / 手动操作 ----------

_HEAD_WHEN_RE = re.compile(
    r"^(?:[0-9一二两三四五六七八九十]+个?半?(?:小时|分钟|分)后|半(?:小时|分钟)后"
    r"|大后天|后天|明天|今天|明晚|今晚"
    r"|下?周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]"
    r"|\d{1,2}月\d{1,2}[号日]|\d{1,2}[号日])?"
    r"\s*"
    r"(?:(?:凌晨|早上|清晨|上午|中午|下午|傍晚|晚上)?[0-9一二两三四五六七八九十]+点(?:半|一刻|三刻)?(?:\d{1,2}分)?|\d{1,2}:\d{2})?"
    r"[\s,，、]*")


def split_when_event(raw: str):
    """（日程：…）里的内容拆成（时间说法, 事项）。时间在头：[日期][钟点]连着咬
    （『明天下午两点 去拿快递』→『明天下午两点』+『去拿快递』），空格/标点分隔；
    咬不出时间就整条当事项挂账（due=null 可见可修，不吞）。"""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    m = _HEAD_WHEN_RE.search(raw)
    if m and m.group(0).strip(" ,，、\t"):   # 全可选正则可能空转，咬到东西才算
        when = m.group(0).strip(" ,，、\t")
        return when, (raw[m.end():].strip() or "（没写事项）")
    return "", raw


def add_from_chat(raw_mark: str, ts: float, source: str = "chat") -> dict:
    when, event = split_when_event(raw_mark)
    due, note = parse_when(when, ts) if when else (None, "没写时间")
    db = _load_db()
    db["seq"] = db.get("seq", 0) + 1
    item = {"id": f"s_{db['seq']:06d}", "when": when[:60], "text": (event or raw_mark)[:200],
            "due_ts": due, "born_ts": ts, "source": source, "status": "active",
            "done_ts": None, "note": note[:120], "touches": []}
    db["items"].append(item)
    _save_db(db)
    return item


def mark_done(item_id: str) -> bool:
    db = _load_db()
    for i in db["items"]:
        if i["id"] == item_id and i.get("status") == "active":
            i["status"] = "done"
            i["done_ts"] = time.time()
            _save_db(db)
            return True
    return False


def delete_item(item_id: str) -> bool:
    db = _load_db()
    before = len(db["items"])
    db["items"] = [i for i in db["items"] if i.get("id") != item_id]
    if len(db["items"]) < before:
        _save_db(db)
        return True
    return False


def mark_touch(item_id: str, kind: str, ts: float = None) -> None:
    """打接触标。ts 可指定（隔离测试用合成时钟），默认此刻。"""
    db = _load_db()
    for i in db["items"]:
        if i["id"] == item_id:
            if kind == "morning":
                today = time.strftime("%Y%m%d", time.localtime(ts or time.time()))
                if _touch_day(i, "morning") == today:
                    return   # 早嘴每天最多一次
            elif has_touch(i, kind):
                return   # 其他接触一辈子一次——数次数封顶，不判时间
            i.setdefault("touches", []).append({"kind": kind, "ts": ts or time.time()})
            _save_db(db)
            return


def cleanup(now: float = None) -> int:
    """销账/挂账条目过期清档（保留 30 天：期间他问起她答得出靠注入兜不住——
    30 天外的事不值得占账本；手动删随时可以）。"""
    now = now or time.time()
    db = _load_db()
    keep = []
    removed = 0
    for i in db["items"]:
        ref = i.get("done_ts") or i.get("born_ts") or now
        if i.get("status") == "done" or not i.get("due_ts"):
            if now - ref > KEEP_DONE_S:
                removed += 1
                continue
        keep.append(i)
    if removed:
        db["items"] = keep
        _save_db(db)
    return removed


# ---------- 触发候选（给 server 的心跳用） ----------

def due_fire_candidates(now: float) -> list:
    out = []
    for i in _load_db()["items"]:
        due = i.get("due_ts")
        if i.get("status") != "active" or not due or has_touch(i, "due"):
            continue
        if due - DUE_LEAD_S <= now <= due + RE_AFTER_S:
            out.append(i)
    return out


def re_fire_candidates(now: float, last_user_ts: float) -> list:
    out = []
    for i in _load_db()["items"]:
        due = i.get("due_ts")
        if i.get("status") != "active" or not due or has_touch(i, "re") or not has_touch(i, "due"):
            continue
        if now < due + RE_AFTER_S or now > due + RE_WINDOW_S:
            continue
        # 叫过之后他回过话的话，settle 早就销账了；这里再兜一道
        due_touch_ts = next((t["ts"] for t in i.get("touches", []) if t.get("kind") == "due"), 0)
        if last_user_ts and last_user_ts > due_touch_ts:
            continue
        out.append(i)
    return out


# ---------- 聊天装配注入 + 销账结算 ----------

def pending_injections(now: float, first_of_day: bool) -> dict:
    """算这条回复该带哪些日程嘴（只读不动账——打标在 settle_on_exchange）。"""
    lt = _local(now)
    today0 = _mk(lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0)
    tomorrow24 = today0 + 2 * 86400
    morning, mention, followup = [], [], []
    if first_of_day and lt.tm_hour >= MORNING_MIN_HOUR:
        for i in _load_db()["items"]:
            due = i.get("due_ts")
            if (i.get("status") == "active" and due and not i.get("touches")
                    and i.get("born_ts", 0) < today0
                    and now < due < tomorrow24):
                morning.append(i)
    for i in _load_db()["items"]:
        due = i.get("due_ts")
        if (i.get("status") == "active" and due and not has_touch(i, "mention")
                and now > due + RE_AFTER_S):
            mention.append(i)
    mention.sort(key=lambda x: x.get("due_ts", 0))
    for i in _load_db()["items"]:
        done_ts = i.get("done_ts")
        if (i.get("status") == "done" and not has_touch(i, "followup")
                and done_ts and today0 <= done_ts <= now
                and FOLLOWUP_HOURS[0] <= lt.tm_hour < FOLLOWUP_HOURS[1]):
            followup.append(i)
    return {"morning": morning[:INJECT_CAP], "mention": mention[:INJECT_CAP],
            "followup": followup[:1]}


def settle_on_exchange(now: float, first_of_day: bool) -> None:
    """他这条消息已经换来她的回复（注入已交付）——现在按同口径打标：
    早嘴打标；兜底嘴打标+销账；叫过之后他回来了的静默销账；晚间好奇打标。
    重算而不是传值：跟 build_system_prompt 的注入条件同一套函数，不会走样。"""
    inj = pending_injections(now, first_of_day)
    db = _load_db()
    touched = False
    today_key = time.strftime("%Y%m%d")
    inj_ids = {}
    for k in ("morning", "mention", "followup"):
        for i in inj[k]:
            inj_ids[i["id"]] = k
    for i in db["items"]:
        kind = inj_ids.get(i["id"])
        if kind == "morning":
            if _touch_day(i, "morning") != today_key:
                i.setdefault("touches", []).append({"kind": "morning", "ts": now})
                touched = True
        elif kind == "mention":
            i.setdefault("touches", []).append({"kind": "mention", "ts": now})
            i["status"], i["done_ts"] = "done", now   # 说完即销（用户拍板）
            touched = True
        elif kind == "followup":
            i.setdefault("touches", []).append({"kind": "followup", "ts": now})
            touched = True
        else:
            # 静默销账：叫过（due/re）之后他发来了任何一条消息——都算这事了了
            if i.get("status") == "active":
                call_ts = max((t["ts"] for t in i.get("touches", [])
                               if t.get("kind") in ("due", "re")), default=0)
                if call_ts and now > call_ts:
                    i["status"], i["done_ts"] = "done", now
                    touched = True
    if touched:
        _save_db(db)
