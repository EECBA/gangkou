const $ = (s) => document.querySelector(s);
const chat = $("#chat"), input = $("#input"), sendBtn = $("#send");
let cfg = { profiles: [], active_id: null, system_prompt: "", vision: {}, search: {} };
let streaming = false;
let herName = "港口";   // 她的名字（20260929d）：boot 从 /api/soulfiles 拉取，填进所有显示位

function applyHerName() {
  ["#headName", "#sessName", "#popName"].forEach((s) => { const el = $(s); if (el) el.textContent = herName; });
  updateDot();
}

const TEMPLATES = {
  "ds":        { name: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-v4-flash" },
  "ds-pro":    { name: "DeepSeek V4 Pro", base_url: "https://api.deepseek.com/v1", model: "deepseek-v4-pro" },
  "kimi":      { name: "Kimi", base_url: "https://api.moonshot.cn/v1", model: "kimi-latest" },
  "or-hermes": { name: "OpenRouter Hermes", base_url: "https://openrouter.ai/api/v1", model: "nousresearch/hermes-4-70b" },
  "blank":     { name: "自定义", base_url: "", model: "" },
};

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function md(text) {
  if (window.marked) {
    try { return marked.parse(text); } catch (e) {}
  }
  const d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}

// 底部轻提示（2026.9.25b 从 boot 内提到全局：顶层代码——如放歌的心形切换——也要弹反馈，
// 留在 boot 里顶层够不着，点了心报 ReferenceError 界面就不刷新了）
function toastTip(msg) {
  let t = document.getElementById("tip");
  if (!t) { t = document.createElement("div"); t.id = "tip"; document.body.appendChild(t); }
  t.textContent = msg;
  t.style.opacity = 1;
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.style.opacity = 0; }, 1800);
}

// 剥掉系统标记（心情/记）和角色扮演旁白（*动作*）——都不进聊天气泡
function cleanText(t) {
  return t
    .replace(/[*＊][^*＊\n]{1,30}[*＊]/g, "")
    .replace(/[（(]记[:：][^）)]*[）)]/g, "").replace(/[（(]记[^）)\n]*$/gm, "")
    .replace(/[（(]心情[:：][^）)]*[）)]/g, "").replace(/[（(]心情[^）)\n]*$/gm, "").trim();
}

const MOOD_EMOJI = { "开心": "😊", "平静": "🙂", "想念": "🥺", "委屈": "🥹", "生气": "😠", "难过": "😢", "好奇": "🤔", "困": "😴", "兴奋": "✨" };

async function refreshMood() {
  try {
    const m = await (await fetch("/api/mood")).json();
    $("#moodChip").textContent = m.label ? `${MOOD_EMOJI[m.label] || "💭"} ${m.label}` : "💭";
  } catch (e) {}
}

// 积温五轴（她的此刻·主页心情卡 2026.9.21）：数值看得见、自己动。
// 条以中线为 0 点：正值金色向右，负值墨灰向左；每轴带人话标签，底部一句话串起来。
const JIWEN_AXES = [
  { key: "connection", name: "牵挂", label: v => v < 0.20 ? "悠闲" : v < 0.35 ? "留意" : v < 0.45 ? "想念" : "焦躁" },
  { key: "pride",      name: "分寸", label: v => v <= 0.1 ? "放软" : v <= 0.3 ? "微矜持" : "端着" },
  { key: "valence",    name: "心情", label: v => v > 0.3 ? "开心" : v < -0.3 ? "难受" : "中性" },
  { key: "arousal",    name: "心绪", label: v => v > 0.3 ? "焦躁" : v < -0.3 ? "慵懒" : "平静" },
  { key: "immersion",  name: "沉浸", label: v => v > 0.3 ? "入神" : "空闲" },
];

async function refreshJiwen() {
  try {
    const d = await (await fetch("/api/jiwen")).json();
    const st = d.state || {};
    const box = $("#pfJiwen");
    const words = [];
    box.innerHTML = "";
    for (const ax of JIWEN_AXES) {
      const v = Math.max(-1, Math.min(1, Number(st[ax.key]) || 0));
      const w = ax.label(v);
      words.push(w);
      const row = document.createElement("div");
      row.className = "jiwen-axis";
      row.innerHTML =
        `<span class="jname">${ax.name}</span>` +
        `<span class="jtrack"><span class="jfill${v < 0 ? " neg" : ""}" style="${v >= 0 ? "left:50%" : "right:50%"};width:${Math.abs(v) * 50}%;"></span></span>` +
        `<span class="jval">${v.toFixed(2)}</span>`;
      row.title = w;
      box.appendChild(row);
    }
    $("#pfJiwenNote").textContent = `她此刻：${words.join("、")}`;
  } catch (e) {}
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  // 日月图标：暗=太阳（点回白天），亮=月牙（点进夜里）——SVG 线条与图标栏同语言
  $("#tileTheme").innerHTML = t === "dark"
    ? '<svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 3 V5 M12 19 V21 M3 12 H5 M19 12 H21 M5.6 5.6 L7 7 M17 17 L18.4 18.4 M18.4 5.6 L17 7 M7 17 L5.6 18.4"/></svg>'
    : '<svg class="ico" viewBox="0 0 24 24"><path d="M19.5 13.8 A8 8 0 1 1 10.2 4.2 A6.6 6.6 0 0 0 19.5 13.8 Z"/></svg>';
  localStorage.setItem("theme", t);
  if (window.applyBg) window.applyBg();  // 夜间不吃壁纸：切日夜时重新决定铺不铺
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}
applyTheme(localStorage.getItem("theme") || "light");

// ---------- 副窗口 + 主区视图（✉❀📖🎬 点开直接替换右侧主区；头像/🎵/⚙ 只换侧栏） ----------
const SUB_TILES = { chat: "tileChat", moments: "tileMoments", book: "tileBook", movie: "tileMovie", star: "tileStar", music: "tileMusic" };
const SUB_TITLES = { chat: "对话", moments: "她的动态", book: "读书角", movie: "观影室", star: "记忆星图", music: "放歌" };
const VIEW_IDS = { none: "view-blank", chat: "view-chat", moments: "view-moments", book: "view-book", movie: "view-movie", star: "view-star", schedule: "view-schedule", settings: "view-settings" };
let currentSub = null;
let didDrag = false;
let activeView = "none";   // 当前主区视图（判断"看没看见"用）
let unreadN = 0;           // 不在聊天页/切走标签页时，她发来的未读数

function showView(key) {
  Object.entries(VIEW_IDS).forEach(([k, id]) => $("#" + id).classList.toggle("hidden", k !== key));
  activeView = key;
  if (key === "chat") {
    // 打开聊天永远落在最底部（隐藏期间追加的消息不会带跑滚动位置）
    requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
    if (!document.hidden) clearUnread();
  }
}

function updateDot() {
  const dot = $("#unreadDot");
  if (!unreadN) {
    dot.classList.add("hidden");
    document.title = herName || "港口";
  } else {
    dot.classList.remove("hidden");
    dot.textContent = unreadN > 99 ? "99+" : String(unreadN);
    document.title = `(${unreadN}) ${herName || "港口"}`;
  }
}

function clearUnread() {
  unreadN = 0;
  updateDot();
}

function bumpUnread(n = 1) {
  unreadN += n;
  updateDot();
}

function openSub(key, keepCollapse) {
  if (didDrag) return;  // 刚拖完图标，忽略拖拽结束误触的点击
  // 手机上 ✉ 直接进聊天（会话列表盖满全屏，手机上没意义；宽屏才弹）
  if (key === "chat" && window.matchMedia("(max-width: 720px)").matches) {
    Object.entries(SUB_TILES).forEach(([k, id]) => $("#" + id).classList.toggle("active", k === "chat"));
    $("#tileSchedule").classList.remove("active");   // 日程不属副窗体系，active 自己管（20260925j）
    ["chat", "moments", "book", "movie", "star", "music"].forEach((k) => $("#sub-" + k).classList.add("hidden"));
    $("#subWrap").classList.add("hidden");
    $("#subPanel").classList.add("hidden");
    currentSub = null;
    showView("chat");
    return;
  }
  if (!key) {   // 手机覆盖层的 ✕ 收起（桌面关副窗走 subToggle 箭头）
    $("#subWrap").classList.add("hidden");
    $("#subPanel").classList.add("hidden");
    currentSub = null;
    return;
  }
  if (key === currentSub && !$("#subPanel").classList.contains("hidden") && activeView === key) return;   // 双栏常驻（20260925i）：再点同一图标不收起（20260925l 补：从设置/日程等主区视图回来不算"再点"，activeView 也得对上）
  Object.entries(SUB_TILES).forEach(([k, id]) => $("#" + id).classList.remove("active"));
  $("#tileSchedule").classList.remove("active");   // 日程/设置不属副窗体系，active 各自管（20260925j/l）
  $("#tileSettings").classList.remove("active");
  ["chat", "moments", "book", "movie", "star", "music"].forEach((k) => $("#sub-" + k).classList.add("hidden"));
  $("#subTitle").textContent = SUB_TITLES[key];
  $("#sub-" + key).classList.remove("hidden");
  $("#subWrap").classList.remove("hidden");
  $("#subPanel").classList.remove("hidden");
  if (!keepCollapse) {   // 点导航=想看副窗（宽屏展开偏好；中等宽度=覆盖层 forced；开机自动进聊天传 true 尊重偏好）
    if ($("#subWrap").classList.contains("narrow")) {
      $("#subWrap").classList.add("forced");
      $("#subToggle").innerHTML = SUB_TOGGLE_SVGS.left;
    } else setSubCollapsed(false);
  }
  $("#" + SUB_TILES[key]).classList.add("active");
  currentSub = key;
  if (VIEW_IDS[key]) showView(key);   // ✉❀📖🎬✦：右侧主区整个换成对应界面
  if (key === "chat") loadChatSide();
  if (key === "moments") loadMoments();
  if (key === "star") initStarView();
  if (key === "book") {
    $("#bookShelf").classList.remove("hidden");
    $("#bookReader").classList.add("hidden");
    loadBooks();
  }
  if (key === "music") loadSongs();
}

// ---------- 日程视图（20260925j 刀2）：rail 时钟入口，只切主区不占副窗 ----------
function openSchedule() {
  if (didDrag) return;
  Object.entries(SUB_TILES).forEach(([k, id]) => $("#" + id).classList.remove("active"));
  $("#tileSettings").classList.remove("active");
  $("#tileSchedule").classList.add("active");
  showView("schedule");
  loadScheduleView();
}

// ---------- 设置视图（20260925l 刀4）：rail 齿轮入口，只切主区不占副窗 ----------
function openSettings() {
  Object.entries(SUB_TILES).forEach(([k, id]) => $("#" + id).classList.remove("active"));
  $("#tileSchedule").classList.remove("active");
  $("#tileSettings").classList.add("active");
  showView("settings");
}

// ---------- 她的悬浮卡（20260925k 刀3；同刀改：rail 顶头像撤位，唯一入口=聊天顶栏 headAva，卡从其正下方弹出） ----------
function toggleHerPop(btn) {
  const pop = $("#herPop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  const r = btn.getBoundingClientRect();
  pop.classList.remove("hidden");
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let left = r.left, top = r.bottom + 8;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 10));
  top = Math.max(8, Math.min(top, window.innerHeight - h - 10));
  pop.style.left = left + "px";
  pop.style.top = top + "px";
  loadProfile();
  refreshJiwen();
}
(function initHerPop() {
  document.addEventListener("click", (e) => {
    const pop = $("#herPop");
    if (pop.classList.contains("hidden")) return;
    if (pop.contains(e.target)) return;
    if (e.target.closest("#headAva")) return;   // 入口按钮自己管 toggle
    pop.classList.add("hidden");
  });
})();

// ---------- 副窗收起箭头（20260925i 刀1；20260925m 刀5 升级中等宽度让位） ----------
// 宽屏：collapsed=用户偏好（localStorage）。中等宽度（721~1100）：副窗自动让位（CSS .narrow 下 width:0），
// 点箭头 toggle .forced=覆盖层形态展开（不动偏好）；拉回宽屏恢复偏好。≤720 手机箭头隐藏不受影响。
const SUB_TOGGLE_SVGS = {
  left: '<svg viewBox="0 0 24 24"><polyline points="14.5 5.5 9 12 14.5 18.5"/></svg>',   // ‹ 收起
  right: '<svg viewBox="0 0 24 24"><polyline points="9.5 5.5 15 12 9.5 18.5"/></svg>',   // › 展开
};
const NARROW_MQ = window.matchMedia("(max-width: 1100px)");
const PHONE_MQ = window.matchMedia("(max-width: 720px)");
function setSubCollapsed(c) {
  $("#subWrap").classList.toggle("collapsed", c);
  $("#subToggle").classList.toggle("collapsed", c);
  $("#subToggle").innerHTML = c ? SUB_TOGGLE_SVGS.right : SUB_TOGGLE_SVGS.left;
  localStorage.setItem("subCollapsed", c ? "1" : "0");
}
function syncNarrow() {
  const wrap = $("#subWrap"), tog = $("#subToggle");
  if (NARROW_MQ.matches && !PHONE_MQ.matches) {
    // 摘掉 collapsed（其 width:0 特异性会压过 .forced 的展开）；窄屏收起态由 CSS .narrow 兜底
    wrap.classList.add("narrow");
    wrap.classList.remove("forced", "collapsed");
    tog.classList.remove("collapsed");
    tog.innerHTML = SUB_TOGGLE_SVGS.right;
  } else {
    wrap.classList.remove("narrow", "forced");
    tog.classList.remove("collapsed");
    setSubCollapsed(localStorage.getItem("subCollapsed") === "1");   // 恢复宽屏偏好
  }
}
(function initSubToggle() {
  // 恢复上次偏好；初始化瞬间关过渡，防刷新闪收起动画
  const els = [$("#subWrap"), $("#subToggle")];
  els.forEach((el) => (el.style.transition = "none"));
  syncNarrow();
  requestAnimationFrame(() => requestAnimationFrame(() => els.forEach((el) => (el.style.transition = ""))));
  $("#subToggle").addEventListener("click", () => {
    const wrap = $("#subWrap");
    if (wrap.classList.contains("narrow")) {              // 中等宽度：覆盖层开/关，不写偏好
      const forced = wrap.classList.toggle("forced");
      $("#subToggle").innerHTML = forced ? SUB_TOGGLE_SVGS.left : SUB_TOGGLE_SVGS.right;
    } else {
      setSubCollapsed(!wrap.classList.contains("collapsed"));
    }
  });
  NARROW_MQ.addEventListener("change", syncNarrow);
})();

// ---------- 键盘弹起辅助（20260925m 刀5，安卓）：interactive-widget=resizes-content 让视口自动缩，
// 这里补一手——点输入框时聊天滚到底，正在打的对话不被键盘盖住历史 ----------
document.addEventListener("focusin", (e) => {
  if (e.target && (e.target.id === "input" || e.target.closest?.(".composer"))) {
    requestAnimationFrame(() => requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; }));
  }
});

// ---------- 图标上下拖动排序（指针实现：只认竖直方向，鼠标+触屏通用） ----------
let railDrag = null;
(function initRailDrag() {
  const box = $("#railTop");
  const dropLine = document.createElement("div");
  dropLine.className = "drop-line";
  box.appendChild(dropLine);

  box.querySelectorAll(".tile").forEach((t) => {
    t.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      railDrag = { el: t, y0: e.clientY, started: false };
    });
  });

  window.addEventListener("pointermove", (e) => {
    if (!railDrag) return;
    const dy = e.clientY - railDrag.y0;
    if (!railDrag.started) {
      if (Math.abs(dy) < 6) return;
      railDrag.started = true;
      didDrag = true;
      railDrag.el.classList.add("dragging");
    }
    railDrag.el.style.transform = `translateY(${dy}px)`;
    let ref = null;
    box.querySelectorAll(".tile").forEach((s) => {
      if (s === railDrag.el) return;
      const r = s.getBoundingClientRect();
      if (e.clientY > r.top + r.height / 2) ref = s;
    });
    const target = ref ? ref.nextSibling : box.querySelector(".tile");
    box.insertBefore(dropLine, target === railDrag.el ? railDrag.el.nextSibling : target);
    dropLine.style.display = "block";
  });

  const endRailDrag = () => {
    if (!railDrag) return;
    if (railDrag.started) {
      box.insertBefore(railDrag.el, dropLine);
      railDrag.el.style.transform = "";
      railDrag.el.classList.remove("dragging");
      dropLine.style.display = "none";
      saveRailOrder();
      setTimeout(() => { didDrag = false; }, 100);
    }
    railDrag = null;
  };
  window.addEventListener("pointerup", endRailDrag);
  window.addEventListener("pointercancel", endRailDrag);
})();
function saveRailOrder() {
  localStorage.setItem("railOrder", JSON.stringify([...$("#railTop").children].map((c) => c.dataset.key)));
}
(function restoreRailOrder() {
  const box = $("#railTop");
  let order = [];
  try { order = JSON.parse(localStorage.getItem("railOrder") || "[]"); } catch (e) {}
  order.forEach((k) => { const el = box.querySelector(`[data-key="${k}"]`); if (el) box.appendChild(el); });
})();

// ---------- 她的个人主页 ----------
// 签名（20260929c 改制）：预设轮换池已删——签名由她按积温情绪自己写，永远显示最新一条
let curSignature = "";
function applySignature() {
  const txt = curSignature || "";
  if ($("#pfSign")) $("#pfSign").textContent = txt || "（她还没写下想说的）";
  if ($("#headSign")) $("#headSign").textContent = txt || "…";
  if ($("#moSig")) $("#moSig").textContent = txt || "…";
}
const loadProfile = async () => {
  try {
    const m = await (await fetch("/api/mood")).json();
    $("#pfMood").textContent = m.label ? `${MOOD_EMOJI[m.label] || "💭"} ${m.label}（${relTime(m.ts)}）` : "还没说过话";
  } catch (e) {}
  try {
    const d = await (await fetch("/api/signatures")).json();
    curSignature = d.current || "";
    applySignature();
  } catch (e) {}
};

// ---------- 灵魂设置页（20260929d）：名称 + SOUL/USER 全文编辑 + 打开文件夹 ----------
const loadSoulTab = async () => {
  try {
    const d = await (await fetch("/api/soulfiles")).json();
    $("#soulName").value = d.her_name || "";
    $("#soulText").value = d.soul || "";
    $("#userText").value = d.user || "";
  } catch (e) {}
  $("#soulSaveTip").textContent = "";
};
async function saveSoulFiles() {
  const body = {
    her_name: $("#soulName").value.trim(),
    soul: $("#soulText").value,
    user: $("#userText").value,
  };
  const tip = $("#soulSaveTip");
  tip.textContent = "保存中…";
  try {
    const r = await fetch("/api/soulfiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { tip.textContent = d.error || "保存失败"; return; }
    herName = d.her_name || herName;
    applyHerName();
    tip.textContent = "已保存，立即生效——她下一句话就开始用新的。";
    toastTip("灵魂已更新");
  } catch (e) {
    tip.textContent = "保存失败（网络问题），再试一次";
  }
}
async function openSoulDir() {
  try {
    const r = await fetch("/api/soulfiles/open", { method: "POST" });
    if (!r.ok) toastTip("打不开文件夹");
  } catch (e) { toastTip("打不开文件夹"); }
}

// ---------- 首启引导卡（20260929d）：空房（没 Key 没聊过没引导过）第一次打开弹一次 ----------
function showOnboard() {
  const pop = $("#onboardPop");
  pop.classList.remove("hidden");
  $("#obRelation").addEventListener("change", () => {
    const custom = $("#obRelation").value === "自定义";
    $("#obRelationCustom").style.display = custom ? "" : "none";
  });
  const close = () => pop.classList.add("hidden");
  $("#obGo").addEventListener("click", async () => {
    const relation = $("#obRelation").value === "自定义"
      ? $("#obRelationCustom").value.trim() : $("#obRelation").value;
    const body = {
      her_name: $("#obName").value.trim(),
      call_name: $("#obCall").value.trim(),
      relation,
      soul_wish: $("#obWish").value.trim(),
      user_note: $("#obNote").value.trim(),
    };
    if (!body.her_name || !body.call_name) { $("#obTip").textContent = "她的名字和对你的称呼得填上，别的都可以空着。"; return; }
    $("#obTip").textContent = "正在为她落笔…";
    try {
      const r = await fetch("/api/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) { $("#obTip").textContent = d.error || "没保存上，再点一次"; return; }
      herName = d.her_name || herName;
      applyHerName();
      close();
      toastTip("她准备好了。接下来去 设置 → 大脑 填上 API Key，她就能说话了");
    } catch (e) {
      $("#obTip").textContent = "网络问题没保存上，再点一次";
    }
  });
  $("#obSkip").addEventListener("click", async () => {
    close();
    try { await fetch("/api/onboarding", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ skip: true }) }); } catch (e) {}
    toastTip("随时来 设置 → 灵魂 塑造她");
  });
  $("#obToSettings").addEventListener("click", async () => {
    close();
    try { await fetch("/api/onboarding", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ skip: true }) }); } catch (e) {}
    openSettings();
    // 直接切到灵魂 tab：高亮第一项
    const item = document.querySelector('#setMenu .set-menu-item[data-tab="soul"]');
    if (item) item.click();
  });
}
// 「她记得的事」整页只读视图（20260925l 刀4：从她的主页挪来设置页安家，全文展示）
const loadMemoryView = async () => {
  try {
    const mem = await (await fetch("/api/memory")).json();
    const txt = (mem.memory || "").trim();
    $("#memoryText").textContent = txt || "（她还没亲手记下什么——聊天里她说（记：…）时会写进来）";
  } catch (e) {}
};
function relTime(ts) {
  if (!ts) return "";
  const m = Math.floor((Date.now() / 1000 - ts) / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return m + " 分钟前";
  const h = Math.floor(m / 60);
  if (h < 24) return h + " 小时前";
  return Math.floor(h / 24) + " 天前";
}

// 把她的一整段回复拆成一条条短消息（微信节奏）
function splitMsgs(t) {
  const parts = [];
  t.split("\n").forEach((line) => {
    line = line.trim();
    if (!line) return;
    const sentences = line.match(/[^。！？!?!~…]+[。！？!?!~…]*/g) || [line];
    let buf = "";
    sentences.forEach((s) => {
      buf += s;
      if (buf.trim().length >= 4) { parts.push(buf.trim()); buf = ""; }
    });
    if (buf.trim()) parts.push(buf.trim());
  });
  return parts.length ? parts : [t.trim() || "…"];
}

function fmtTs(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

function addBubble(role, html, cls, images, ts) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (role === "user" ? "me" : "her");
  const ava = document.createElement("div");
  ava.className = "ava";
  if (role === "user") {
    ava.innerHTML = '<img src="/me/avatar" onerror="this.remove();this.parentElement.textContent=\'宝\'">';
  } else {
    ava.innerHTML = '<img src="/avatar" onerror="this.remove();this.parentElement.textContent=\'港\'">';
  }
  wrap.appendChild(ava);
  const col = document.createElement("div");
  col.className = "bcol";
  const b = document.createElement("div");
  b.className = "bubble" + (cls ? " " + cls : "");
  if (images && images.length) {
    const strip = document.createElement("div");
    strip.className = "img-strip";
    images.forEach((src) => {
      const im = document.createElement("img");
      im.className = "chat-img";
      im.src = src;
      im.loading = "lazy";
      im.addEventListener("click", () => {
        $("#lightboxImg").src = src;
        $("#lightbox").classList.remove("hidden");
      });
      strip.appendChild(im);
    });
    b.appendChild(strip);
  }
  const span = document.createElement("div");
  span.innerHTML = html;
  b.appendChild(span);
  col.appendChild(b);
  const t = document.createElement("div");
  t.className = "btime";
  t.textContent = fmtTs(ts || Date.now() / 1000);
  col.appendChild(t);
  wrap.appendChild(col);
  if (ts) lastRenderedTs = Math.max(lastRenderedTs, ts);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  return b;
}

let lastRenderedTs = 0;   // 已渲染进聊天流的最大消息时间戳（防主动消息被历史渲染+轮询双显）

function fmtSize(n) {
  if (typeof n !== "number" || isNaN(n)) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

// 她递文件（文档工坊 2026.9.19）：docx/pptx 做好后以文件卡片出现在聊天里，点击下载
// 他发文件（同日）：role=user、file.url="/file/..." 走上传文件路由
function addFileBubble(role, file, ts) {
  const isPpt = String(file.kind || file.name || "").includes("ppt");
  const kindLabel = file.label || (isPpt ? "PPT 演示" : "Word 文档");
  const hrefBase = file.url || "/workspace/";
  const icon = isPpt
    ? '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="1.5"/><path d="M12 4l3.5 5h-7L12 4z" fill="currentColor" stroke="none"/><path d="M8 21h8M12 17v4"/></svg>'
    : '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v5h5"/><path d="M9 13h7M9 17h7"/></svg>';
  const wrap = document.createElement("div");
  wrap.className = "msg " + (role === "user" ? "me" : "her");
  const ava = document.createElement("div");
  ava.className = "ava";
  ava.innerHTML = role === "user"
    ? '<img src="/me/avatar" onerror="this.remove();this.parentElement.textContent=\'宝\'">'
    : '<img src="/avatar" onerror="this.remove();this.parentElement.textContent=\'港\'">';
  wrap.appendChild(ava);
  const col = document.createElement("div");
  col.className = "bcol";
  const b = document.createElement("div");
  b.className = "bubble file-bubble";
  const a = document.createElement("a");
  a.className = "file-card";
  a.href = hrefBase + encodeURIComponent(file.name || "");
  if (file.name) a.setAttribute("download", file.name);
  a.innerHTML = '<span class="fc-icon">' + icon + "</span>" +
    '<span class="fc-meta"><span class="fc-name"></span>' +
    '<span class="fc-sub">' + kindLabel + (fmtSize(file.size) ? " · " + fmtSize(file.size) : "") + " · 点击下载</span></span>";
  a.querySelector(".fc-name").textContent = file.name || "文件";
  b.appendChild(a);
  col.appendChild(b);
  const t = document.createElement("div");
  t.className = "btime";
  t.textContent = fmtTs(ts || Date.now() / 1000);
  col.appendChild(t);
  wrap.appendChild(col);
  if (ts) lastRenderedTs = Math.max(lastRenderedTs, ts);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
}

function renderHistory(list, limit) {
  limit = limit || 60;
  chat.innerHTML = "";
  if (!list.length) {
    chat.innerHTML = '<div class="empty-hint">她还没有住进来。<br>先去左下角的设置 → 大脑 填上 API Key，然后说第一句话。</div>';
    return;
  }
  const tail = list.slice(-limit);
  if (tail.length < list.length) {
    const more = document.createElement("button");
    more.className = "mini-btn";
    more.textContent = "︿ 加载更早的消息（还有 " + (list.length - tail.length) + " 条）";
    more.style.cssText = "align-self:center;margin:4px auto 14px;display:block;";
    chat.appendChild(more);
    const sh = chat.scrollHeight;
    more.addEventListener("click", () => {
      renderHistory(list, limit + 100);
      chat.scrollTop += chat.scrollHeight - sh;
    });
  }
  let lastDay = "";
  let prevTs = 0;
      for (const m of tail) {
    const d = new Date((m.ts || 0) * 1000);
    const today = new Date();
    const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate();
    let dayKey = d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
    let dayLabel = "";
    if (sameDay) {
      dayLabel = "今天";
    } else {
      const yesterday = new Date(today);
      yesterday.setDate(yesterday.getDate() - 1);
      const yDay = d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
      const yKey = yesterday.getFullYear() + "-" + yesterday.getMonth() + "-" + yesterday.getDate();
      dayLabel = yDay === yKey ? "昨天" : (d.getMonth() + 1) + "月" + d.getDate() + "日";
    }
    if (dayKey !== lastDay) {
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.textContent = dayLabel;
      chat.appendChild(sep);
      lastDay = dayKey;
      lastMsgDate = dayKey;
      prevTs = m.ts || 0;
    } else if (m.ts && prevTs && (m.ts - prevTs) > 1800) {
      // 同一天内 >30 分钟间隔，插入时间戳
      const timeSep = document.createElement("div");
      timeSep.className = "time-sep";
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      timeSep.textContent = hh + ":" + mm;
      chat.appendChild(timeSep);
    }
    prevTs = m.ts || prevTs;
    lastMsgTs = m.ts || lastMsgTs;
    lastRenderedTs = Math.max(lastRenderedTs, m.ts || 0);
    if (m.role === "user") {
      addBubble("user", m.content ? md(m.content) : "", null, (m.images || []).map((n) => "/image/" + encodeURIComponent(n)), m.ts);
      (m.files || []).forEach((f) => addFileBubble("user", { ...f, url: "/file/" }, m.ts));
    }
    else if (m.role === "assistant") {
      splitMsgs(cleanText(m.content)).forEach((s) => addBubble("her", md(s), null, null, m.ts));
      (m.files || []).forEach((f) => addFileBubble("her", f, m.ts));
    }
  }
}

function refreshChip() {
  const p = cfg.profiles.find((x) => x.id === cfg.active_id);
  $("#brainChip").textContent = p ? `${p.name} · ${p.model}` : "未设置大脑";
}

// ---------- 设置：大脑 ----------
function renderBrainTab() {
  const idx = cfg.profiles.findIndex((p) => p.id === cfg.active_id);
  const act = idx >= 0 ? cfg.profiles[idx] : null;
  const others = cfg.profiles.filter((p) => p.id !== cfg.active_id);

  $("#brainNow").textContent = act ? `当前大脑：${act.name} · ${act.model}` : "还没有大脑，先添加一个";

  const box = $("#brainCard");
  box.innerHTML = "";
  if (act) {
    const card = document.createElement("div");
    card.className = "profile-card";
    card.innerHTML = `
      <div class="field"><label>名称</label><input data-f="name" value="${esc(act.name)}"></div>
      <div class="field"><label>API 地址</label><input data-f="base_url" value="${esc(act.base_url)}" placeholder="https://api.deepseek.com/v1"></div>
      <div class="field">
        <label>模型</label>
        <div style="display:flex;gap:6px;">
          <input data-f="model" list="mdl-act" value="${esc(act.model)}" style="flex:1;">
          <button class="mini-btn fetch-models" type="button">拉取</button>
        </div>
        <datalist id="mdl-act"></datalist>
      </div>
      <div class="field"><label>API Key</label><input data-f="api_key" type="password" value="${esc(act.api_key)}" placeholder="sk-…"></div>
      <div class="field"><label>温度（0.7克制 / 1.0自然 / 1.3热情，留空默认）</label><input data-f="temperature" type="number" step="0.1" min="0" max="2" value="${esc(act.temperature ?? "")}" placeholder="默认"></div>`;
    card.querySelectorAll("input[data-f]").forEach((inp) => {
      inp.addEventListener("input", () => { cfg.profiles[idx][inp.dataset.f] = inp.value; renderBrainNowOnly(); });
    });
    card.querySelector(".fetch-models").addEventListener("click", async (e) => {
      const btn = e.target;
      btn.textContent = "…";
      try {
        const r = await fetch("/api/models?profile_id=" + encodeURIComponent(act.id));
        const d = await r.json();
        card.querySelector("datalist").innerHTML = (d.models || []).map((m) => `<option value="${esc(m)}"></option>`).join("");
        btn.textContent = d.models?.length ? `可选 ${d.models.length} 个` : "无模型";
      } catch (err) { btn.textContent = "失败"; }
    });
    box.appendChild(card);
  }

  const swWrap = $("#brainSwitchWrap");
  const sel = $("#brainSwitch");
  if (others.length) {
    swWrap.style.display = "block";
    sel.innerHTML = '<option value="">切换到…</option>' + others.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.model)}</option>`).join("");
  } else {
    swWrap.style.display = "none";
  }
  sel.onchange = () => {
    if (sel.value) { cfg.active_id = sel.value; renderBrainTab(); refreshChip(); }
  };
  $("#btnDelBrain").style.display = cfg.profiles.length > 1 ? "inline-block" : "none";
}

function renderBrainNowOnly() {
  const act = cfg.profiles.find((p) => p.id === cfg.active_id);
  if (act) $("#brainNow").textContent = `当前大脑：${act.name} · ${act.model}`;
}

function renderVisionTab() {
  const v = cfg.vision || {};
  $("#visionNow").textContent = v.model || "deepseek-v4-flash-vision-exp";
  $("#visionKeyState").textContent = !v.api_key ? "跟随当前大脑" : "使用单独的 Key";
}

function renderSearchTab() {
  const s = cfg.search || {};
  const name = s.provider === "tavily" ? "Tavily" : "博查";
  $("#searchState").textContent = s.api_key ? `已启用 · ${name}` : "未配置（她不联网）";
}

// ---------- 音乐 ----------
let songList = [];
let songIdx = -1;
let songLikes = new Set();   // 心动歌名单（2026.9.25）：心形持久显示，再点取消
const audio = $("#audio");
const rawSongName = (s) => s.name.replace(/^🔗 /, "");
const HEART_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';
const PLAY_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>';
const PAUSE_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
// 显示名（2026.9.25c）：剥掉文件后缀——.ogg/.mp3 这类字符噪音不该出现在界面上
const dispName = (s) => s.name.replace(/\.(mp3|flac|ogg|wav|m4a)$/i, "");
// 跑马灯通用启动器（2026.9.25d）：inner 装在 overflow:hidden 的 wrap 里，量溢出定偏移定速
const startMarquee = (inner, wrap) => {
  const overflow = inner.scrollWidth - wrap.clientWidth;
  if (overflow > 4) {
    inner.style.setProperty("--mq-shift", -(overflow + 16) + "px");
    inner.style.setProperty("--mq-dur", Math.max(6, Math.round(overflow / 18)) + "s");
    inner.classList.add("mq-on");
  }
};
// 正在放标签：固定单行，超长跑马灯来回滚（2026.9.25c——长歌名曾把卡片顶高、子窗上下跳）
const setNowPlaying = (text) => {
  const label = $("#nowPlayingLabel");
  label.innerHTML = "";
  const inner = document.createElement("span");
  inner.className = "mq-inner";
  inner.textContent = text;
  label.appendChild(inner);
  startMarquee(inner, label);
};
// 播放键状态化（2026.9.25b）：图标跟播放态走——放=⏸且金框高亮，停=▶；旁边小字同步 ♪/⏸
const syncPlayState = () => {
  const playing = songIdx >= 0 && !audio.paused;
  const btn = $("#musicPlay");
  btn.innerHTML = playing ? PAUSE_SVG : PLAY_SVG;
  btn.classList.toggle("on-air", playing);
  setNowPlaying(songIdx < 0 ? "♪ 没在放歌" : (playing ? "♪ " : "⏸ ") + dispName(songList[songIdx]));
};
// 音量（2026.9.25e）：喇叭图标三态（静音/小声/大声）+滑杆+百分比，记 localStorage 跨会话记住
const VOL_SVGS = {
  off: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>',
  low: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>',
  on: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>',
};
const applyVol = (v) => {
  v = Math.max(0, Math.min(100, Math.round(v)));
  audio.volume = v / 100;
  localStorage.setItem("musicVol", String(v));
  $("#musicVol").value = v;
  $("#musicVolLabel").textContent = v + "%";
  $("#musicVolBtn").innerHTML = v === 0 ? VOL_SVGS.off : (v < 50 ? VOL_SVGS.low : VOL_SVGS.on);
};
const loadSongs = async () => {
  const d = await (await fetch("/api/music")).json();
  songLikes = new Set(d.likes || []);
  songList = [
    ...(d.songs || []).map((f) => ({ name: f, src: "/music/" + encodeURIComponent(f) })),
    ...(d.links || []).map((l) => ({ name: "🔗 " + l.name, src: l.url })),
  ];
  if (songIdx >= songList.length) songIdx = -1;
  renderSongs();
  updateLikeBtn();
  scrollPlayingIntoView();
};
const updateLikeBtn = () => {
  $("#musicLike").classList.toggle("liked", songIdx >= 0 && songLikes.has(rawSongName(songList[songIdx])));
};
async function toggleLike(i) {
  if (i < 0 || i >= songList.length) return;
  const name = rawSongName(songList[i]);
  const on = !songLikes.has(name);
  try {
    const r = await fetch("/api/music/like", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, on }) });
    if (!r.ok) return;
  } catch (e) { return; }
  if (on) { songLikes.add(name); toastTip("她记住了这首心动歌"); }
  else { songLikes.delete(name); toastTip("已取消心动"); }
  renderSongs();
  updateLikeBtn();
}
const scrollPlayingIntoView = () => {
  // 歌单成了固定高度滚动区，正在放的那首要滚进可见区（只在加载/切歌时滚，点赞重渲染不跳动）
  const cur = $("#songList").querySelector(".moment.playing");
  if (cur) cur.scrollIntoView({ block: "nearest" });
};
const renderSongs = () => {
  const box = $("#songList");
  box.innerHTML = songList.length ? "" : '<div class="desc">还没有歌：丢文件进 data\\music，或粘贴链接添加</div>';
  const marquees = [];   // [inner, wrap]——挂进 DOM 后才量得准宽度
  songList.forEach((s, i) => {
    const d = document.createElement("div");
    d.className = "moment" + (i === songIdx ? " playing" : "");
    const name = document.createElement("span");
    name.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    const shown = s.name.replace(/\.(mp3|flac|ogg|wav|m4a)$/i, "");
    if (i === songIdx) {
      // 正在放的那行：名字不硬截断，超长跑马灯来回滚（2026.9.25d）
      const inner = document.createElement("span");
      inner.className = "mq-inner";
      inner.textContent = shown;
      name.appendChild(inner);
      marquees.push([inner, name]);
    } else {
      name.textContent = shown;
    }
    d.appendChild(name);
    const st = document.createElement("span");
    st.style.cssText = "color:var(--gold);flex-shrink:0;font-size:12px;";
    st.textContent = i === songIdx ? "♪ 正在放" : "";
    d.appendChild(st);
    const h = document.createElement("span");
    h.className = "row-like" + (songLikes.has(rawSongName(s)) ? " liked" : "");
    h.title = songLikes.has(rawSongName(s)) ? "心动歌 · 再点取消" : "心动这首歌";
    h.innerHTML = HEART_SVG;
    h.addEventListener("click", (e) => { e.stopPropagation(); toggleLike(i); });
    d.appendChild(h);
    d.addEventListener("click", () => playSong(i));
    box.appendChild(d);
  });
  marquees.forEach(([inner, wrap]) => startMarquee(inner, wrap));
};
function playSong(i) {
  if (!songList.length) return;
  songIdx = (i + songList.length) % songList.length;
  audio.src = songList[songIdx].src;
  audio.play();   // 界面状态由 audio 的 play/pause 事件统一驱动（syncPlayState），这里不再手写标签
  fetch("/api/music/playing", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: songList[songIdx].name.replace(/^🔗 /, "") }) });
  renderSongs();
  updateLikeBtn();
  scrollPlayingIntoView();
}

// ---------- 动态（主区视图：日志流；侧栏：统计 + 她说） ----------
const loadMoments = async () => {
  try {
    const s = await (await fetch("/api/stats")).json();
    $("#moDays").innerHTML = `${s.days}<small>相伴 · 天</small>`;
    const box = $("#momentsStats");
    box.innerHTML = "";
    [["今日", `${s.today} 条`], ["聊天", `${s.messages} 条`], ["记忆", `${s.memories} 条`], ["心动歌", `${s.songs} 首`]].forEach(([k, v]) => {
      const c = document.createElement("div");
      c.className = "stat-cell";
      c.innerHTML = `<b>${esc(v)}</b><span>${k}</span>`;
      box.appendChild(c);
    });
  } catch (e) {}
  $("#moSig").textContent = curSignature || "…";
  const list = await (await fetch("/api/moments")).json();
  const box = $("#momentsMainList");
  box.innerHTML = list.length ? "" : '<div class="desc" style="margin:0;">她还没发过动态。想到什么的时候，她自己会发。</div>';
  list.forEach((m) => {
    const d = document.createElement("div");
    d.className = "moment-full";
    const t = new Date((m.ts || 0) * 1000);
    const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    d.innerHTML = `<div class="m-date">${t.getMonth() + 1}月${t.getDate()}日 ${hm}</div><div class="m-text">${md(m.text || "")}</div><div class="m-sign">—— ${esc(herName)}</div>`;
    box.appendChild(d);
  });
};

// ---------- 她的书架（铺满流）+ 共读阅读器 ----------
const BOOK_PAGE = 2200;   // 一段多少字（翻页粒度，短一点读起来不累）
let readerState = null;

const loadBooks = async () => {
  try {
    const d = await (await fetch("/api/books")).json();
    const box = $("#bookList");
    box.innerHTML = d.books.length ? "" : '<div class="desc" style="margin:0;">书架还空着：点下面的按钮上架第一本 txt。</div>';
    d.books.forEach((b) => {
      const row = document.createElement("div");
      row.className = "moment-full";
      row.style.cursor = "pointer";
      const herPct = Math.round(b.her_pct * 100), myPct = Math.round(b.my_pct * 100);
      row.innerHTML = `
        <div style="display:flex;align-items:baseline;gap:10px;">
          <div class="m-date" style="flex:1;">她读了 ${herPct}% · 你读了 ${myPct}% · ${b.notes} 条批注${b.finished ? " · 她读完了" : ""}</div>
          <button class="mini-btn del book-del" title="下架">✕</button>
        </div>
        <div class="m-text serif" style="font-size:19px;letter-spacing:2px;">《${esc(b.name)}》</div>
        ${b.last_note ? `<div class="m-sign" style="text-align:left;">✎ ${b.last_note_who === "me" ? "你" : "她"}最近记：“${esc(b.last_note)}”</div>` : '<div class="m-sign" style="text-align:left;color:var(--line-soft);">✎ 还没有人写下什么</div>'}`;
      row.addEventListener("click", () => openReader(b.name));
      row.querySelector(".book-del").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`把《${b.name}》下架？（会删掉文件、你们俩的进度和她的批注）`)) return;
        await fetch("/api/books/" + encodeURIComponent(b.name), { method: "DELETE" });
        loadBooks();
      });
      box.appendChild(row);
    });
    const reading = d.books.find((b) => !b.finished);
    const el = $("#subBookNow");
    if (el) el.textContent = reading ? `《${reading.name}》读到 ${Math.round(reading.her_pct * 100)}%` : (d.books.length ? "在架的书都读完啦" : "书架还空着");
  } catch (e) {}
};

async function openReader(name) {
  try {
    const d = await (await fetch("/api/books/" + encodeURIComponent(name) + "/read")).json();
    if (!d.text && d.text !== "") { toastTip(d.error || "打不开这本书"); return; }
    d.notes = (d.notes || d.her_notes || []).map((n) => ({ ...n, who: n.who || "her" }));
    readerState = { ...d, page: Math.floor((d.my_pos || 0) / BOOK_PAGE) };
    renderReader();
    $("#bookShelf").classList.add("hidden");
    $("#bookReader").classList.remove("hidden");
  } catch (e) { toastTip("打不开：" + e.message); }
}

function noteHtml(n) {
  const mine = n.who === "me";
  return `<div class="${mine ? "my-note" : "her-note"}" data-id="${n.id || ""}">` +
    `<span class="note-who">${mine ? "✎ 你" : "✎ 她"}在这里记：</span>${esc(n.text)}` +
    `<button class="note-del" title="删掉这条批注">✕</button></div>`;
}

function renderReader() {
  const rs = readerState;
  const scEl = $("#bookReader .reader-scroll");
  const sc = scEl ? scEl.scrollTop : 0;   // 重画不丢位置
  const total = rs.text.length;
  const start = rs.page * BOOK_PAGE;
  const chunk = rs.text.slice(start, start + BOOK_PAGE);
  const pages = Math.max(1, Math.ceil(total / BOOK_PAGE));

  // 批注（两人）+ 她读到这里，按位置排序插进正文
  const marks = [];
  rs.notes.forEach((n) => {
    if (n.pos > start && n.pos <= start + chunk.length) marks.push({ pos: n.pos, html: noteHtml(n) });
  });
  if (rs.her_pos > start && rs.her_pos <= start + chunk.length) {
    marks.push({ pos: rs.her_pos, html: `<div class="her-pos">◈ 她读到这里（${Math.round(rs.her_pos / Math.max(total, 1) * 100)}%）</div>` });
  }
  marks.sort((a, b) => a.pos - b.pos);
  let mi = 0;
  const flushMarks = (upto) => { while (mi < marks.length && marks[mi].pos <= upto) { html += marks[mi++].html; } };

  // 正文排版（书页化，位置零漂移）：按原始偏移逐段走——
  // 空白串里有空行=段落分隔；行首空格丢掉（改用首行缩进）；单独成行的数字=小节号
  let html = "";
  let paraOpen = false;
  let prevWsBreak = false;   // 当前 token 前面是不是空行（数字只有自成一段才算小节号）
  const openPara = () => { if (!paraOpen) { html += '<div class="para">'; paraOpen = true; } };
  const closePara = () => { if (paraOpen) { html += "</div>"; paraOpen = false; } };
  // 自动识别的"非正文"行：小节号 / 章标题 / 分隔线 —— 全部自动，不用管书是什么格式
  const kindOf = (t, atParaStart) => {
    const s = t.trim();
    if (atParaStart && /^[0-9０-９一二三四五六七八九十百]+[、.．]?$/.test(s) && s.length <= 6) return "sec";
    if (/^(第\s*[0-9０-９一二三四五六七八九十百千]+\s*[章节回卷部](\s+\S{0,20})?|序章|序言|楔子|尾声|后记|前言)$/i.test(s)) return "chapter";
    if (/^[-—_=*·•~～◇◆✦]{2,}$/.test(s)) return "sep";
    return "text";
  };
  let i = 0;
  const isPunc = (c) => "。！？!?!~…；;".includes(c);
  while (i < chunk.length) {
    let j = i;
    while (j < chunk.length && /\s/.test(chunk[j])) j++;
    if (j > i) {
      flushMarks(start + i);
      const ws = chunk.slice(i, j);
      if (/\n[ \t]*\n/.test(ws)) { closePara(); prevWsBreak = true; }   // 空行 → 段落间距
      else if (ws.includes("\n")) { openPara(); html += " "; prevWsBreak = false; }  // 硬换行 → 软接
      else prevWsBreak = false;
      i = j;
      continue;
    }
    j = i;
    while (j < chunk.length && !/\s/.test(chunk[j]) && !isPunc(chunk[j])) j++;
    if (j < chunk.length && isPunc(chunk[j])) j++;
    flushMarks(start + i);
    const t = chunk.slice(i, j);
    const kind = kindOf(t, prevWsBreak);
    prevWsBreak = false;
    if (kind === "sec") {
      closePara();
      html += `<div class="sec-mark">· ${esc(t.trim())} ·</div>`;
    } else if (kind === "chapter") {
      closePara();
      html += `<div class="chapter-mark">${esc(t.trim())}</div>`;
    } else if (kind === "sep") {
      closePara();
      html += `<div class="sep-mark">✦</div>`;
    } else {
      openPara();
      html += `<span class="sent" data-start="${start + i}">${esc(t)}</span>`;
    }
    i = j;
  }
  closePara();
  flushMarks(start + chunk.length);

  const herPct = Math.round(rs.her_pos / Math.max(total, 1) * 100);
  const myPct = Math.round(rs.my_pos / Math.max(total, 1) * 100);
  $("#bookReader").innerHTML = `
    <div class="reader-scroll">
      <div class="reader-head">
        <button class="mini-btn" id="readerBack">‹ 书架</button>
        <b class="serif" style="font-size:16px;letter-spacing:2px;">《${esc(rs.name)}》</b>
        <span class="reader-pct">她 ${herPct}% · 你 ${myPct}%</span>
      </div>
      <div class="reader-text">${html || "（翻开是空的）"}</div>
      <div id="noteBar" class="hidden">
        <input id="noteInput" placeholder="为选中的这句话写点什么（你们都看得见）" maxlength="200">
        <button class="mini-btn" id="noteSave">记下</button>
        <button class="mini-btn del" id="noteCancel">取消</button>
      </div>
    </div>
    <div class="reader-nav">
      <button class="mini-btn" id="pgPrev" ${rs.page <= 0 ? "disabled" : ""}>‹ 上一段</button>
      <span>${rs.page + 1} / ${pages} 段</span>
      <button class="mini-btn" id="pgNext" ${rs.page >= pages - 1 ? "disabled" : ""}>下一段 ›</button>
      <button class="mini-btn" id="pgHer">去她在的地方</button>
    </div>`;
  const save = () => fetch("/api/books/" + encodeURIComponent(rs.name) + "/progress", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chars: Math.min((rs.page + 1) * BOOK_PAGE, total) }),
  });
  $("#readerBack").addEventListener("click", () => {
    $("#bookReader").classList.add("hidden");
    $("#bookShelf").classList.remove("hidden");
    loadBooks();
  });
  $("#pgPrev").addEventListener("click", () => { if (rs.page > 0) { rs.page--; rs.scrollTo = ".her-pos"; renderReader(); save(); } });
  $("#pgNext").addEventListener("click", () => { if (rs.page < pages - 1) { rs.page++; rs.scrollTo = ".her-pos"; renderReader(); save(); } });
  $("#pgHer").addEventListener("click", () => { rs.page = Math.floor(rs.her_pos / BOOK_PAGE); rs.scrollTo = ".her-pos"; renderReader(); save(); });
  // 点句子写批注
  $("#bookReader").querySelectorAll(".sent").forEach((sp) => {
    sp.addEventListener("click", () => {
      document.querySelectorAll(".sent.sel").forEach((x) => x.classList.remove("sel"));
      sp.classList.add("sel");
      rs.sel = { start: +sp.dataset.start, end: +sp.dataset.start + sp.textContent.length };
      $("#noteBar").classList.remove("hidden");
      $("#noteInput").focus();
    });
  });
  $("#noteCancel").addEventListener("click", () => {
    document.querySelectorAll(".sent.sel").forEach((x) => x.classList.remove("sel"));
    rs.sel = null;
    $("#noteBar").classList.add("hidden");
    $("#noteInput").value = "";
  });
  $("#noteSave").addEventListener("click", async () => {
    const text = $("#noteInput").value.trim();
    if (!text || !rs.sel) return;
    const r = await fetch("/api/books/" + encodeURIComponent(rs.name) + "/note", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pos: rs.sel.end, text }),
    });
    if (r.ok) {
      const n = await r.json();
      rs.notes.push(n);
      $("#noteInput").value = "";
      rs.sel = null;
      rs.scrollTo = `[data-id="${n.id}"]`;
      renderReader();
    } else { toastTip("没记上，再试一次"); }
  });
  // 删批注（她的你的都能删，✕）
  $("#bookReader").querySelectorAll(".note-del").forEach((b) => {
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = b.closest("[data-id]").dataset.id;
      if (!id || !confirm("删掉这条批注？")) return;
      await fetch("/api/books/" + encodeURIComponent(rs.name) + "/note/" + id, { method: "DELETE" });
      rs.notes = rs.notes.filter((n) => n.id !== id);
      renderReader();
    });
  });
  const sc2 = $("#bookReader .reader-scroll");
  if (sc2) sc2.scrollTop = sc;
  // 定位：翻页/去她在的地方后，滚到她的书签或批注（而不是页顶）
  if (rs.scrollTo) {
    const el = $("#bookReader").querySelector(rs.scrollTo);
    if (el) setTimeout(() => el.scrollIntoView({ block: "center", behavior: "smooth" }), 60);
    rs.scrollTo = null;
  }
}

// ---------- 对话副窗口（✉）：会话列表，预览取最近一条 ----------
const loadChatSide = async () => {
  try {
    const h = await (await fetch("/api/history")).json();
    if (h.length) {
      const last = h[h.length - 1];
      const t = new Date((last.ts || 0) * 1000);
      $("#sessTime").textContent = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
      $("#sessPreview").textContent = (last.content || "").replace(/\s+/g, " ").slice(0, 32) || "（图片）";
    } else {
      $("#sessPreview").textContent = "说第一句话吧";
    }
  } catch (e) {}
};

// ---------- 日程 ----------
// 人话时间（与后端 schedule.humanize_due 同口径：今天/明天/M月D日 HH:MM）
function humanDue(ts) {
  const d = new Date(ts * 1000), n = new Date();   // 后端 due_ts 是秒，JS Date 吃毫秒
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, n)) return `今天 ${hm}`;
  const tom = new Date(n.getFullYear(), n.getMonth(), n.getDate() + 1);
  if (same(d, tom)) return `明天 ${hm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}
const schedEsc = (s) => String(s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const loadScheduleView = async () => {
  const d = await (await fetch("/api/schedule")).json();
  $("#scheduleText").value = d.text || "";
  renderScheduleItems(d.items || []);
};
function renderScheduleItems(items) {
  const box = $("#schedList");
  const DAY = 86400;
  const now = Date.now() / 1000;
  const active = items.filter((i) => i.status === "active");
  const todo = active.filter((i) => i.due_ts).sort((a, b) => a.due_ts - b.due_ts);
  const pend = active.filter((i) => !i.due_ts);                       // 挂账：时间没说清，她记下了但不会响
  const done = items.filter((i) => i.status === "done" && i.done_ts && now - i.done_ts < 30 * DAY).sort((a, b) => b.done_ts - a.done_ts);
  if (!todo.length && !pend.length && !done.length) {
    box.innerHTML = `<div class="paper-card"><div class="sched-empty">还没有日程。<br>跟她说"明天下午三点提醒我拿快递"，或跟她说"记一下，下周二要交报告"——她会记下来，到点前 15 分钟叫你。</div></div>`;
    return;
  }
  const row = (i, mode) => `
    <div class="sched-row ${mode || ""}" data-id="${i.id}">
      <span class="sched-when">${i.due_ts ? schedEsc(humanDue(i.due_ts)) : "挂账"}</span>
      <span class="sched-text">${schedEsc(i.text)}${i.when ? ` <span class="sched-src">（原话：${schedEsc(i.when)}）</span>` : ""}</span>
      ${mode === "done" ? `<span class="sched-src">办完 ✓</span>` : `
        <button class="sched-op" data-act="done" title="办完了，销账"><svg viewBox="0 0 24 24"><polyline points="4.5 12.5 10 18 19.5 6.5"/></svg></button>
        <button class="sched-op" data-act="del" title="删掉这条"><svg viewBox="0 0 24 24"><path d="M5 7 H19 M9.5 7 V5 H14.5 V7 M7 7 L8 19.5 H16 L17 7"/></svg></button>`}
    </div>`;
  let html = "";
  if (todo.length) html += `<div class="paper-card"><div class="sched-group-title">◆ 要办（到点前 15 分钟她会叫你）</div>${todo.map((i) => row(i)).join("")}</div>`;
  if (pend.length) html += `<div class="paper-card"><div class="sched-group-title">◆ 挂账（时间没说清，先记着不会响）</div>${pend.map((i) => row(i, "pend")).join("")}</div>`;
  if (done.length) html += `<div class="paper-card"><details><summary class="sched-group-title" style="cursor:pointer;">◆ 已办（${done.length} 条，留 30 天）</summary>${done.map((i) => row(i, "done")).join("")}</details></div>`;
  box.innerHTML = html;
}
// 打勾销账 / 删除（删除二次确认，星图 armConfirm 同思路）
(function initSchedOps() {
  $("#schedList").addEventListener("click", async (e) => {
    const btn = e.target.closest(".sched-op");
    if (!btn) return;
    const id = btn.closest(".sched-row").dataset.id;
    if (btn.dataset.act === "done") {
      await fetch(`/api/schedule/items/${id}/done`, { method: "POST" });
      loadScheduleView();
      toastTip("销账了，这事她不惦记了");
    } else if (btn.dataset.act === "del") {
      if (btn.dataset.armed) {
        await fetch(`/api/schedule/items/${id}`, { method: "DELETE" });
        loadScheduleView();
      } else {
        btn.dataset.armed = "1";
        btn.classList.add("confirm");
        btn.textContent = "确认删？";
        setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.classList.remove("confirm"); btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 7 H19 M9.5 7 V5 H14.5 V7 M7 7 L8 19.5 H16 L17 7"/></svg>'; } }, 3000);
      }
    }
  });
})();

// ---------- 多气泡回复渲染器（微信节奏：一条一条蹦，不一次性糊脸上） ----------
// 2026.9.11 连发模式：多个 renderer 跨轮排队——上一条回复没蹦完时，新回复的气泡
// 等它 waitDone 再上屏（数据照流进队列，只是上屏串行），不然两轮气泡交错乱蹦。
let lastRenderer = null;
function makeReplyRenderer() {
  const prev = lastRenderer;
  let queue = [];        // 已定格的完整段
  let shown = 0;         // 已上屏的段数
  let cur = "";          // 正在打的半句
  let streaming = true;
  let typingEl = null;   // 打字中的气泡（当前半句）
  let timer = null;
  let started = false;   // 首次上屏是否已放行（排队用）
  let thinkLabel = "她在想";   // 用工具时换："她看了眼天气…"
  let doneResolve = null;
  let mine = [];         // 这条回复创建的所有气泡行（revise时整个撤回重放）
  const donePromise = new Promise((r) => { doneResolve = r; });

  const delayFor = (s) => Math.min(1600, Math.round((300 + s.length * 55) * (0.75 + Math.random() * 0.5)));  // 像人在一条一条发，节奏带抖动

  function put(role, html, cls) {
    const el = addBubble(role, html, cls);
    if (el && el.closest) mine.push(el.closest(".msg"));
    return el;
  }

  function dropTyping() {
    if (typingEl) { typingEl.closest(".msg").remove(); typingEl = null; }
  }

  function kick() {   // 上屏入口：上一轮没蹦完先排队，排到时自己这轮可能已有货
    if (timer) return;
    if (!started) {
      started = true;
      (prev ? prev.waitDone() : Promise.resolve()).then(() => { if (!timer) step(); });
    } else {
      step();
    }
  }

  function step() {
    if (shown < queue.length) {
      dropTyping();
      const s = queue[shown++];
      put("her", md(s));
      chat.scrollTop = chat.scrollHeight;
      timer = setTimeout(step, delayFor(s));
    } else {
      timer = null;
      if (streaming) {
        if (!typingEl) typingEl = put("her", "");
        typingEl.innerHTML = (cur ? md(cur) : `<span class="thinking">${thinkLabel}</span>`) + '<span class="cursor">▌</span>';
        chat.scrollTop = chat.scrollHeight;
      } else {
        dropTyping();
        doneResolve();
      }
    }
  }

  const api = {
    update(acc, isStreaming) {
      streaming = isStreaming;
      const clean = cleanText(acc);
      if (!clean) {
        if (streaming) {
          dropTyping();
          kick();
        }
        return;
      }
      const segs = splitMsgs(clean);
      if (streaming) {
        cur = segs.length ? segs[segs.length - 1] : "";
        queue = segs.length > 1 ? segs.slice(0, -1) : [];
      } else {
        cur = "";
        queue = segs;
      }
      kick();
    },
    revise(text) {
      // 打码重说：像"撤回重发"——把这条回复的气泡全撤掉，用修正版重新逐条蹦
      clearTimeout(timer); timer = null;
      typingEl = null;
      mine.forEach(m => m.remove()); mine = [];
      shown = 0; queue = []; cur = ""; streaming = true;
      this.update(text, true);
    },
    error(msg) {
      streaming = false;
      clearTimeout(timer); timer = null;
      while (shown < queue.length) put("assistant", md(queue[shown++]));
      if (cur) { put("assistant", md(cur)); cur = ""; }
      dropTyping();
      put("assistant", md("⚠ " + msg), "error");
      doneResolve();
    },
    setThinking(l) {
      thinkLabel = l || "她在想";
      if (typingEl && !cur) typingEl.innerHTML = `<span class="thinking">${thinkLabel}…</span><span class="cursor">▌</span>`;
    },
    waitDone: () => donePromise,
  };
  lastRenderer = api;
  return api;
}

async function consumeSSE(resp, renderer) {
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "", acc = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const part of parts) {
      if (!part.startsWith("data:")) continue;
      let ev;
      try { ev = JSON.parse(part.slice(5).trim()); } catch (e) { continue; }
      if (ev.type === "delta") { acc += ev.text; renderer.update(acc, true); }
      else if (ev.type === "tool") { if (renderer.setThinking) renderer.setThinking(`她${ev.label}`); }
      else if (ev.type === "revise") { acc = ev.text; if (renderer.revise) renderer.revise(ev.text); }
      else if (ev.type === "error") { renderer.update(acc, false); renderer.error(ev.message); return; }
      else if (ev.type === "done") {
        renderer.update(acc || "（她没有说话）", false);
        if (ev.files && ev.files.length) ev.files.forEach((f) => addFileBubble("her", f));
        return;
      }
    }
  }
  renderer.update(acc, false);
}

let pendingImages = [];
let pendingFiles = [];   // 待发给她的文件（已上传，拿的是服务端文件名）[{name,size,kind,label,url}]
let lastMsgDate = "";
let lastMsgTs = 0;
function ensureDaySep(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  const key = d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
  // 日期分隔（跨天）
  if (key !== lastMsgDate) {
    const sep = document.createElement("div"); sep.className = "day-sep";
    const today = new Date();
    if (d.toDateString() === today.toDateString()) { sep.textContent = "今天"; }
    else { const y = new Date(today); y.setDate(y.getDate()-1);
      sep.textContent = d.toDateString()===y.toDateString() ? "昨天" : (d.getMonth()+1)+"月"+d.getDate()+"日"; }
    chat.appendChild(sep);
    lastMsgDate = key; lastMsgTs = ts || 0;
    return;
  }
  // 时间间隔（同一天内 >30 分钟插入时间戳）
  const gap = (ts || 0) - lastMsgTs;
  if (ts && gap > 1800) {
    const timeSep = document.createElement("div"); timeSep.className = "time-sep";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    timeSep.textContent = hh + ":" + mm;
    chat.appendChild(timeSep);
  }
  lastMsgTs = ts || lastMsgTs;
}

function renderPreview() {
  const box = $("#imgPreview");
  box.innerHTML = "";
  box.style.display = (pendingImages.length || pendingFiles.length) ? "flex" : "none";
  pendingImages.forEach((src, idx) => {
    const d = document.createElement("div");
    d.className = "pv";
    d.innerHTML = `<img src="${src}"><button data-i="${idx}" title="移除">×</button>`;
    d.querySelector("button").addEventListener("click", () => {
      pendingImages.splice(idx, 1);
      renderPreview();
    });
    box.appendChild(d);
  });
  pendingFiles.forEach((f, idx) => {
    const d = document.createElement("div");
    d.className = "pv file-pv";
    const s = document.createElement("span");
    s.textContent = f.name;
    d.appendChild(s);
    const x = document.createElement("button");
    x.title = "移除";
    x.textContent = "×";
    x.addEventListener("click", () => {
      pendingFiles.splice(idx, 1);
      renderPreview();
    });
    d.appendChild(x);
    box.appendChild(d);
  });
}

function fileToDataURL(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => {
      const img = new Image();
      img.onload = () => {
        const max = 1280;
        const scale = Math.min(1, max / Math.max(img.width, img.height));
        const cv = document.createElement("canvas");
        cv.width = Math.round(img.width * scale);
        cv.height = Math.round(img.height * scale);
        cv.getContext("2d").drawImage(img, 0, 0, cv.width, cv.height);
        resolve(cv.toDataURL("image/jpeg", 0.85));
      };
      img.onerror = reject;
      img.src = fr.result;
    };
    fr.onerror = reject;
    fr.readAsDataURL(file);
  });
}

async function addFiles(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/") || pendingImages.length >= 4) continue;
    try { pendingImages.push(await fileToDataURL(f)); } catch (e) {}
  }
  renderPreview();
}

// 发文件给她（2026.9.19）：选了就先传服务端（落 data/files/），拿回文件名挂待发列表
async function uploadForSend(fileList) {
  for (const f of fileList) {
    if (pendingFiles.length >= 3) { toastTip("一次最多带 3 个文件"); break; }
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok || !d.name) { toastTip("这个文件传不上：" + (d.error || r.status)); continue; }
      pendingFiles.push({ name: d.name, size: d.size, kind: d.kind, label: d.label, url: "/file/" });
      renderPreview();
    } catch (e) {
      toastTip("传文件失败了：" + e.message);
    }
  }
}

// ---------- 连发模式（2026.9.11）：他可以随手连发几条，她等他"说完"再一起看 ----------
// outbox 攒静默期内的消息，静默 2 秒（debounce）后多条合成一条多行消息一次调用
// （一次生成、上下文完整，像真人等对方说完这阵才接话）；她正在生成时他发的照攒，
// 她说完（SSE done）后立刻接力冲。输入框从此不锁。
const SEND_QUIET_MS = 2000;
let outbox = [];          // 待发：[{text, images}]
let lastUserMsgAt = 0;
let flushTimer = null;

function scheduleFlush() {
  clearTimeout(flushTimer);
  const wait = Math.max(0, SEND_QUIET_MS - (Date.now() - lastUserMsgAt));
  flushTimer = setTimeout(flushIfDue, wait);
}

function flushIfDue() {
  if (!outbox.length || streaming) return;   // 没货 / 她正在说（done 后 finally 接力）
  if (Date.now() - lastUserMsgAt < SEND_QUIET_MS) { scheduleFlush(); return; }
  flush();
}

async function flush() {
  const batch = outbox;
  outbox = [];
  const text = batch.map(m => m.text).filter(Boolean).join("\n");
  const images = batch.flatMap(m => m.images || []).slice(0, 8);   // 连发多批图也封顶，防识图请求爆
  const files = batch.flatMap(m => m.files || []).map(f => f.name).slice(0, 3);
  streaming = true;
  const renderer = makeReplyRenderer();
  renderer.update("", true);
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, images, files }),
    });
    await consumeSSE(resp, renderer);
  } catch (e) {
    renderer.error("发送失败：" + e.message);
  } finally {
    streaming = false;
    if (outbox.length) scheduleFlush();   // 她说话期间他又发了——接着冲
    input.focus();
    refreshMood();
    refreshJiwen();   // 她回完话：flash 分析刚把五轴推过，数值当场可见地动一下
  }
}

async function send() {
  const text = input.value.trim();
  const images = pendingImages.slice();
  const files = pendingFiles.slice();
  if (!text && !images.length && !files.length) return;
  input.value = "";
  input.style.height = "auto";
  pendingImages = [];
  pendingFiles = [];
  renderPreview();
  if (chat.querySelector(".empty-hint")) chat.innerHTML = "";
  ensureDaySep();
  addBubble("user", text ? md(text) : "", null, images);
  files.forEach((f) => addFileBubble("user", f));
  outbox.push({ text, images, files });
  lastUserMsgAt = Date.now();
  scheduleFlush();
}

async function boot() {
  // 初始状态：什么都不选，右边留白（点 ✉ 才进聊天）
  const cfgResp = await fetch("/api/config");
  if (cfgResp.status === 401) { location.reload(); return; }  // 通行证失效 → 回门锁页
  cfg = await cfgResp.json();
  if (!cfg.vision) cfg.vision = { model: "deepseek-v4-flash-vision-exp", base_url: "", api_key: "" };
  if (!cfg.search) cfg.search = { provider: "bocha", api_key: "" };
  $("#visModel").value = cfg.vision.model || "";
  $("#visKey").value = cfg.vision.api_key || "";
  $("#searchProvider").value = cfg.search.provider || "bocha";
  $("#searchKey").value = cfg.search.api_key || "";
  $("#sysPrompt").value = cfg.system_prompt || "";
  $("#visModel").addEventListener("input", () => { cfg.vision.model = $("#visModel").value; renderVisionTab(); });
  $("#visKey").addEventListener("input", () => { cfg.vision.api_key = $("#visKey").value; renderVisionTab(); });
  $("#searchProvider").addEventListener("change", () => { cfg.search.provider = $("#searchProvider").value; renderSearchTab(); });
  $("#searchKey").addEventListener("input", () => { cfg.search.api_key = $("#searchKey").value; renderSearchTab(); });
  $("#sysPrompt").addEventListener("input", () => { cfg.system_prompt = $("#sysPrompt").value; });

  // 门锁（2026.9.23 独立tab）：状态徽标 + 识别码 + 每次启动都输码（会话票）
  $("#accessCode").value = cfg.access_code || "";
  if (cfg.lock_every_launch === undefined) cfg.lock_every_launch = false;
  const syncLockFields = () => {
    $("#lockState").textContent = cfg.access_code ? "已设防 🔒" : "未设防（门敞着）";
    $("#lockEveryLaunch").checked = !!cfg.lock_every_launch;
    $("#lockLaunchState").textContent = cfg.lock_every_launch ? "每次启动都要输码" : "记住本机（365天）";
  };
  $("#accessCode").addEventListener("input", () => {
    cfg.access_code = $("#accessCode").value;
    $("#lockState").textContent = cfg.access_code ? "已填码（保存后设防）" : "未设防（保存后生效）";
  });
  $("#lockEveryLaunch").addEventListener("change", (e) => {
    cfg.lock_every_launch = e.target.checked;
    syncLockFields();
  });
  $("#btnLockOff").addEventListener("click", () => {
    $("#accessCode").value = "";
    cfg.access_code = "";
    $("#btnSave").click();
  });
  syncLockFields();

  // 书记员（2026.9.23）：状态行 + 立刻抄一批——服务器断档后手动追进度用
  const loadScribeStatus = async () => {
    try {
      const d = await (await fetch("/api/scribe/status")).json();
      $("#scribeState").textContent =
        `还有 ${d.pending} 条没抄 · 碎片 ${d.fragments_count} 条` + (d.running ? " · 正在跑" : "");
    } catch (e) { $("#scribeState").textContent = "状态读不到"; }
  };
  $("#btnScribeRun").addEventListener("click", async () => {
    const btn = $("#btnScribeRun");
    btn.disabled = true; btn.textContent = "抄写中（一批约半分钟到两分钟）…";
    let before = null, resp = null, err = false;
    try { before = (await (await fetch("/api/scribe/status")).json()).pending; } catch (e) {}
    try { resp = await (await fetch("/api/scribe/run", { method: "POST" })).json(); }
    catch (e) { err = true; }
    btn.disabled = false; btn.textContent = "立刻抄一批（45条）";
    let after = null;
    try { after = (await (await fetch("/api/scribe/status")).json()).pending; } catch (e) {}
    loadScribeStatus();
    // 结果明说（20260927m2：用户反馈"点了没反馈"——空跑/不够一批时之前毫无动静）
    if (err) { toastTip("抄写没跑成——看服务器黑窗口里的报错"); return; }
    if (before === null) { toastTip("这批抄完了（状态行已刷新）"); return; }
    if (before === 0) { toastTip(`没有要抄的：已经追平了 · 碎片共 ${resp ? resp.fragments_count : "?"} 条`); return; }
    if (after !== null && after < before) { toastTip(`抄完了：新抄 ${before - after} 条，还剩 ${after} 条没抄`); return; }
    toastTip("这批跑完了：攒的还不满 6 条，书记员会攒着等自动跑一起带");
  });
  loadScribeStatus();

  // 出门网址（今天的隧道地址，点一下复制）
  $("#outUrl").addEventListener("click", async () => {
    const u = $("#outUrl").textContent;
    if (u && u.startsWith("http")) {
      try { await navigator.clipboard.writeText(u); toastTip("网址已复制，去手机粘贴"); } catch (e) { toastTip(u); }
    }
  });
  fetch("/api/out-url").then((r) => r.json()).then((d) => {
    $("#outUrl").textContent = d.url || "隧道没在跑（重启 启动她.bat）";
  }).catch(() => {});

  // 主动消息（积温驱动，2026.9.2）：只剩总开关，什么时候开口由她的五轴心情决定
  const PA_DEF = { enabled: true, min_gap_h: 2, max_gap_h: 5, daily_max: 2, long_chat_cooldown_h: 5, long_chat_msgs: 120 };
  if (!cfg.proactive) cfg.proactive = { ...PA_DEF };
  const paStateText = () => cfg.proactive.enabled
    ? "已开启 · 由她的心情（积温）决定，想念攒够了才来找你"
    : "已关闭";
  const syncPaFields = () => {
    $("#paEnabled").checked = !!cfg.proactive.enabled;
    $("#paState").textContent = paStateText();
  };
  $("#paEnabled").addEventListener("change", (e) => {
    cfg.proactive.enabled = e.target.checked;
    $("#paState").textContent = paStateText();
  });
  syncPaFields();

  renderBrainTab();
  renderVisionTab();
  renderSearchTab();
  refreshChip();
  refreshMood();
  refreshJiwen();
  renderHistory(await (await fetch("/api/history")).json());
  $("#headSign").textContent = curSignature || "…";

  // 设置内部分页（20260925l 刀4：侧边菜单式；soul=灵魂三件套 20260929d；memory=她记得的事只读页）
  document.querySelectorAll("#setMenu .set-menu-item").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll("#setMenu .set-menu-item").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      ["soul", "brain", "vision", "search", "bgset", "memory", "lock", "scribe", "misc"].forEach((n) => {
        $("#tab-" + n).classList.toggle("hidden", n !== t.dataset.tab);
      });
      if (t.dataset.tab === "soul") loadSoulTab();       // 打开灵魂页拉最新（可能在文件夹里手改过）
      if (t.dataset.tab === "bgset") renderBgGrid();  // 每次打开背景页都拉最新壁纸库
      if (t.dataset.tab === "scribe") loadScribeStatus();
      if (t.dataset.tab === "memory") loadMemoryView();
    });
  });
  $("#btnSaveSoul").addEventListener("click", saveSoulFiles);
  $("#btnOpenSoulDir").addEventListener("click", openSoulDir);

  // 大脑：添加模板（默认收起）+ 删除
  const tplBox = $("#brainTemplates");
  tplBox.innerHTML = Object.entries(TEMPLATES).map(([k, t]) => `<button class="mini-btn" data-tpl="${k}">＋${esc(t.name)}</button>`).join("");
  $("#btnAddBrain").addEventListener("click", () => {
    tplBox.style.display = tplBox.style.display === "none" ? "flex" : "none";
  });
  tplBox.querySelectorAll("[data-tpl]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const t = TEMPLATES[btn.dataset.tpl];
      cfg.profiles.push({ id: "p" + Date.now(), name: t.name, base_url: t.base_url, model: t.model, api_key: "", temperature: "" });
      renderBrainTab();
    });
  });
  $("#btnDelBrain").addEventListener("click", () => {
    if (!confirm("删除当前大脑？")) return;
    cfg.profiles = cfg.profiles.filter((p) => p.id !== cfg.active_id);
    cfg.active_id = cfg.profiles[0]?.id ?? null;
    renderBrainTab();
    refreshChip();
  });

  $("#btnSave").addEventListener("click", async () => {
    cfg = await (await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    })).json();
    if (!cfg.vision) cfg.vision = { model: "deepseek-v4-flash-vision-exp", base_url: "", api_key: "" };
    if (!cfg.search) cfg.search = { provider: "bocha", api_key: "" };
    if (!cfg.proactive) cfg.proactive = { enabled: true, min_gap_h: 2, max_gap_h: 5, daily_max: 2, long_chat_cooldown_h: 5, long_chat_msgs: 120 };
    $("#sysPrompt").value = cfg.system_prompt || "";
    $("#visModel").value = cfg.vision.model || "";
    $("#visKey").value = cfg.vision.api_key || "";
    $("#searchProvider").value = cfg.search.provider || "bocha";
    $("#searchKey").value = cfg.search.api_key || "";
    $("#accessCode").value = cfg.access_code || "";
    if (cfg.lock_every_launch === undefined) cfg.lock_every_launch = false;
    syncLockFields();
    syncPaFields();
    renderBrainTab();
    renderVisionTab();
    renderSearchTab();
    refreshChip();
    $("#btnSave").textContent = "已保存 ✓";
    setTimeout(() => { $("#btnSave").textContent = "保存设置"; }, 1500);
  });

  // 日程备忘保存（20260925j 刀2：从设置页搬进日程视图）
  $("#btnSaveSchedule").addEventListener("click", async () => {
    await fetch("/api/schedule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: $("#scheduleText").value }) });
    $("#btnSaveSchedule").textContent = "已保存 ✓";
    setTimeout(() => { $("#btnSaveSchedule").textContent = "保存备忘"; }, 1500);
  });

  // 音乐
  $("#btnAddLink").addEventListener("click", async () => {
    const url = $("#linkInput").value.trim();
    if (!url) return;
    $("#btnAddLink").textContent = "…";
    try {
      const r = await fetch("/api/music/resolve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      if (r.ok) { $("#linkInput").value = ""; await loadSongs(); }
      else {
        const d = await r.json().catch(() => ({}));
        toastTip(d.error || "这个链接她还没法播放");
      }
    } catch (e) { toastTip("添加失败：" + e.message); }
    $("#btnAddLink").textContent = "＋";
  });
  $("#musicPlay").addEventListener("click", () => {
    if (audio.paused) { if (songIdx < 0) playSong(0); else audio.play(); }
    else { audio.pause(); }
  });
  audio.addEventListener("play", syncPlayState);
  audio.addEventListener("pause", syncPlayState);
  $("#musicVol").addEventListener("input", (e) => applyVol(Number(e.target.value)));
  $("#musicVolBtn").addEventListener("click", () => {
    const cur = Number(localStorage.getItem("musicVol") || 100);
    if (cur > 0) { localStorage.setItem("musicVolLast", String(cur)); applyVol(0); }
    else applyVol(Number(localStorage.getItem("musicVolLast") || 70));
  });
  applyVol(Number(localStorage.getItem("musicVol") || 100));
  $("#musicPrev").addEventListener("click", () => playSong(songIdx - 1));
  $("#musicNext").addEventListener("click", () => playSong(songIdx + 1));
  $("#musicLike").addEventListener("click", () => { if (songIdx >= 0) toggleLike(songIdx); });
  audio.addEventListener("ended", () => playSong(songIdx + 1));
  audio.addEventListener("error", () => {
    if (songIdx >= 0) $("#nowPlayingLabel").textContent = "⚠ 放不了（可能VIP/版权）";
  });

  // 动态：她自己发（随机时刻 + 心动时刻），没有手动按钮了

  // 书架：上架 txt / 下架（上传时自动认编码：先严格 UTF-8，不行就 GBK——中文小说 txt 多是 GBK）
  $("#btnAddBook").addEventListener("click", () => $("#bookInput").click());
  $("#bookInput").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    try {
      const buf = await f.arrayBuffer();
      let text;
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(buf);  // 真 UTF-8 才走这
      } catch (err) {
        text = new TextDecoder("gbk").decode(buf);                     // 否则按 GBK 读
      }
      text = text.replace(/^\uFEFF/, "");
      if (text.includes("\uFFFD")) { toastTip("这本书的编码认不出来（既不是UTF-8也不是GBK），转存成UTF-8再试"); return; }
      const r = await fetch("/api/books", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: f.name.replace(/\.txt$/i, ""), text }),
      });
      if (r.ok) {
        const d = await r.json();
        toastTip("上架好了，她会自己来读");
        loadBooks();
        openReader(d.name);   // 直接翻开给你看一眼，确认不乱码
      } else { const d = await r.json().catch(() => ({})); toastTip(d.error || "上架失败"); }
    } catch (err) { toastTip("读取文件失败：" + err.message); }
  });

  // 会话列表：占位的微信/电话给个提示，日常聊天点了就是当前窗口
  $("#sessWechat").addEventListener("click", () => toastTip("微信还没接上（v2 的活儿）"));
  $("#sessPhone").addEventListener("click", () => toastTip("电话还没接上（M3 语音时点亮）"));

  // 聊天输入
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + "px";
  });

  // 工具栏：图片/拍照真功能，其余占位提示（toastTip 已提全局，见文件顶部）
  $("#toolImage").addEventListener("click", () => $("#fileInput").click());
  $("#toolPhoto").addEventListener("click", () => $("#photoInput").click());
  $("#toolVoice").addEventListener("click", () => toastTip("录音还没准备好（M3 语音时点亮）"));
  $("#toolCall").addEventListener("click", () => toastTip("电话还没准备好（M3 全双工时点亮）"));
  $("#toolFile").addEventListener("click", () => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.multiple = true;
    picker.accept = ".txt,.md,.csv,.json,.log,.docx,.pdf";
    picker.onchange = () => { if (picker.files.length) uploadForSend(picker.files); };
    picker.click();
  });
  $("#toolMore").addEventListener("click", () => toastTip("还没想好放什么，先占个位"));
  $("#fileInput").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
  $("#photoInput").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
  input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  ["dragover", "drop"].forEach((ev) => {
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "drop") addFiles([...e.dataTransfer.files]);
    });
  });

  $("#lightbox").addEventListener("click", () => $("#lightbox").classList.add("hidden"));

  // ---------- 我的名片（⚡） ----------
  const loadMe = async () => {
    try {
      const m = await (await fetch("/api/me")).json();
      $("#meName").textContent = m.name;
      $("#meTitle").textContent = m.title;
      const myst = m.my_status || {};
      $("#myMood").value = myst.mood || "";
      $("#myDoing").value = myst.doing || "";
      $("#myNote").value = myst.note || "";
      $("#myListenChip").textContent = "🎵 " + (m.listening || "没在放歌");
    } catch (e) {}
    try {
      const s = await (await fetch("/api/stats")).json();
      $("#myDaysChip").textContent = s.days;
      $("#myTodayChip").textContent = s.today;
    } catch (e) {}
    const ava = $("#meAva");
    ava.innerHTML = '<img src="/me/avatar" onerror="this.remove()">';
    if (!ava.querySelector("img")) ava.textContent = "宝";
  };
  $("#btnMe").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#mePop").classList.toggle("hidden");
    if (!$("#mePop").classList.contains("hidden")) loadMe();
  });
  document.addEventListener("click", (e) => {
    const pop = $("#mePop");
    if (!pop.classList.contains("hidden") && !pop.contains(e.target) && !e.target.closest(".bolt")) pop.classList.add("hidden");
  });
  $("#meAva").addEventListener("click", () => $("#meAvaInput").click());
  $("#btnSaveMyStatus").addEventListener("click", async () => {
    const btn = $("#btnSaveMyStatus");
    btn.textContent = "…";
    try {
      await fetch("/api/me/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mood: $("#myMood").value, doing: $("#myDoing").value, note: $("#myNote").value }),
      });
      btn.textContent = "已更新，她看到了 ✓";
    } catch (e) { btn.textContent = "保存失败"; }
    setTimeout(() => { btn.textContent = "更新我的状态"; }, 1600);
  });
  $("#meAvaInput").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    try {
      const durl = await fileToDataURL(f);
      await fetch("/api/me/avatar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image: durl }) });
      $("#meAva").innerHTML = `<img src="/me/avatar?v=${Date.now()}">`;
      toastTip("头像换好了");
    } catch (err) { toastTip("换头像失败：" + err.message); }
  });

  // ---------- 她的头像更换（20260925k 刀3 起入口在悬浮卡里） ----------
  $("#herPopAva").addEventListener("click", () => $("#herAvaInput").click());
  $("#herAvaInput").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    try {
      const durl = await fileToDataURL(f);
      await fetch("/api/her/avatar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image: durl }) });
      const v = Date.now();
      const herImg = document.querySelector("#herPopAva img");
      if (herImg) herImg.src = `/avatar?v=${v}`;
      const headImg = document.querySelector("#headAva img");
      if (headImg) headImg.src = `/avatar?v=${v}`;
      toastTip("她的头像换好了");
    } catch (err) { toastTip("换头像失败：" + err.message); }
  });

  // ---------- 聊天背景（设置 → 背景）：纸为主，壁纸做衬；纯色走薄纱；夜间固定夜色不吃壁纸 ----------
  const applyBg = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    fetch("/api/backgrounds").then((r) => r.json()).then((d) => {
      if (d.active && !dark) {
        document.body.classList.add("with-bg");
        document.body.classList.toggle("solid-bg", d.active.startsWith("纯色_"));
        $("#bgLayer").style.backgroundImage = `url(/background?v=${Date.now()})`;
      } else {
        document.body.classList.remove("with-bg");
        document.body.classList.remove("solid-bg");
        $("#bgLayer").style.backgroundImage = "";
      }
    }).catch(() => {});
  };
  window.applyBg = applyBg;   // 暴露给 applyTheme，切日夜时联动
  applyBg();
  const renderBgGrid = async () => {    const d = await (await fetch("/api/backgrounds")).json();
    const grid = $("#bgGrid");
    grid.innerHTML = d.backgrounds.length ? "" : '<div class="desc" style="grid-column:1/3;">壁纸库里还没有图</div>';
    d.backgrounds.forEach((f) => {
      const cell = document.createElement("div");
      const active = f === d.active;
      cell.style.cssText = `position:relative;border:2px solid ${active ? "var(--gold)" : "var(--line)"};border-radius:10px;overflow:hidden;cursor:pointer;aspect-ratio:16/10;`;
      cell.innerHTML = `<img src="/backgrounds/${encodeURIComponent(f)}" style="width:100%;height:100%;object-fit:cover;display:block;" loading="lazy">` +
        (active ? '<div style="position:absolute;right:4px;top:4px;font-size:11px;background:var(--gold);color:#fff;border-radius:6px;padding:1px 6px;">在用</div>' : "");
      cell.addEventListener("click", async () => {
        await fetch("/api/background", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: f }) });
        applyBg();
        renderBgGrid();
        toastTip("壁纸换好了");
      });
      grid.appendChild(cell);
    });
  };
  $("#btnPickBg").addEventListener("click", () => $("#bgInput").click());
  $("#bgInput").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    if (!files.length) return;
    for (const f of files) {
      try {
        const durl = await fileToDataURL(f);
        await fetch("/api/background", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image: durl }) });
      } catch (err) { toastTip("上传失败：" + err.message); }
    }
    applyBg();
    renderBgGrid();
    toastTip("壁纸加好了");
  });
  $("#btnClearBg").addEventListener("click", async () => {
    await fetch("/api/background", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clear: true }) });
    applyBg();
    renderBgGrid();
    toastTip("已恢复默认背景");
  });

  // 主动冒泡已删（2026.9.2 积温拍板）：她想不想说话由五轴决定，不再有"静默2小时固定冒泡"。



  // 她主动发来的消息（随机刻）：页面开着时每分钟看一眼，切回页面立刻看一眼
  const checkProactive = async () => {
    if (streaming) return;
    try {
      const d = await (await fetch("/api/proactive-check")).json();
      if (d.pending && d.text) {
        // 页面关着时她发的消息，打开页面已被历史渲染过（时间戳≤已渲染最大值）——只取走不重画
        if (d.ts && d.ts <= lastRenderedTs) return;
        if (chat.querySelector(".empty-hint")) chat.innerHTML = "";
        ensureDaySep(d.ts);
        splitMsgs(cleanText(d.text)).forEach((s) => addBubble("her", md(s), null, null, d.ts));
        chat.scrollTop = chat.scrollHeight;
        refreshMood();
        if (activeView !== "chat" || document.hidden) bumpUnread(splitMsgs(cleanText(d.text)).length);
      }
    } catch (e) {}
  };
  checkProactive();
  setInterval(checkProactive, 60000);
  setInterval(refreshJiwen, 60000);   // 积温五轴随主动消息轮询一起慢刷新（她回完话另有立即刷）
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      checkProactive();
      if (activeView === "chat") {   // 回到页面且正在聊天页 → 滚到底 + 未读清零
        chat.scrollTop = chat.scrollHeight;
        clearUnread();
      }
    }
  });

  // 开机直接见她（20260925i 刀1）：桌面=聊天主区+对话副窗双开（keepCollapse 尊重上次收起偏好）；手机=直接进聊天
  openSub("chat", true);
  loadProfile();   // 顶栏签名+她的心情预载（20260925k：原靠点开她的主页才刷，开机直接见她就该亮真签名）

  // 她的名字（20260929d）：所有显示位开机填充（顶栏/会话头/悬浮卡/标签页标题）
  try {
    const sf = await (await fetch("/api/soulfiles")).json();
    herName = sf.her_name || "港口";
    applyHerName();
  } catch (e) {}

  // 首启引导（20260929d）：空房（没 Key 没聊过没引导过）弹一次塑造卡，填过/跳过永不弹
  try {
    const ob = await (await fetch("/api/onboarding")).json();
    if (ob.needed) showOnboard();
  } catch (e) {}
}

boot();
