  // ============ 记忆星图（preview-star.html 打样稿移植，2026.9.2 接真数据）============
  // 一个星座=一段叙事；星星纯装饰（数量=碎片数封顶7），碎片只在卡片清单里（2026.9.2 拍板）
  (() => {
    const $s = (id) => document.getElementById(id);
    // 星图是否正在展示（activeView 是主作用域 let，IIFE 里够不着，用 DOM 判断）
    const starActive = () => !$s("view-star").classList.contains("hidden");
    const box = $s("view-star"), canvas = $s("stCanvas");
    const ctx = canvas.getContext("2d");
    let W = 0, H = 0, dpr = 1, bgCache = null;
    let camX = 0, camY = 0, scale = 1;
    let hoveredCon = null, pinnedCon = null, hoveredGal = null;
    let mode = "universe", currentGal = null, anim = null;
    let CONS = [], VOIDED = [], RAW_FRAGS = [], TIDY_POOL = [], FRAG_TOTAL = 0, loading = false;

    const GALAXIES = [
      { id: "爱好",   hue: 0,   azimuth: -90 },
      { id: "社交",   hue: 22,  azimuth: -18 },
      { id: "关于我们", hue: 45,  azimuth: 70 },
      { id: "事件",   hue: 152, azimuth: 126 },
      { id: "地点",   hue: 215, azimuth: 198 },
    ];
    const RX = 430, RY = 300;

    function strHash(str) {
      let h = 2166136261;
      for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
      return h >>> 0;
    }
    const rand01 = (seed) => (strHash(seed) % 1000) / 1000;
    function hslToRgb(h, s, l) {
      s /= 100; l /= 100;
      const c = (1 - Math.abs(2 * l - 1)) * s;
      const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
      const m = l - c / 2;
      let r = 0, g = 0, b = 0;
      if (h < 60) [r, g, b] = [c, x, 0];
      else if (h < 120) [r, g, b] = [x, c, 0];
      else if (h < 180) [r, g, b] = [0, c, x];
      else if (h < 240) [r, g, b] = [0, x, c];
      else if (h < 300) [r, g, b] = [x, 0, c];
      else [r, g, b] = [c, 0, x];
      return `${Math.round((r + m) * 255)},${Math.round((g + m) * 255)},${Math.round((b + m) * 255)}`;
    }

    // 数据：/api/memory-graph 的 narratives[] → 星座（一颗星）；碎片只计数不图形化
    function buildData(api) {
      FRAG_TOTAL = (api.fragments || []).length;
      RAW_FRAGS = (api.fragments || []).filter(f => f.id && f.source !== "memory_md");
      CONS = (api.narratives || []).filter(n => !n.superseded).map(n => ({
        id: n.id, manual_stale: !!n.manual_stale,
        title: n.title, galaxy: n.galaxy || "事件", text: n.text || "",
        ew: typeof n.ew === "number" ? n.ew : 0.3,
        date: String(n.created || "").replace(/^\d{4}\./, "").replace(/^0(\d)/, "$1"),
        sig: n.significance || 5,
        fragments: (n.fragments || []).map(f => ({ id: f.id, text: f.text, date: f.date || "",
          ew: typeof f.ew === "number" ? f.ew : 0.3, retired: !!f.retired, edited: !!f.edited, corrected: !!f.corrected })),
      }));
      VOIDED = (api.voided || []);
      // 档案整理候选（单元C）：v1 时代老碎片（2026.9.2 前抄的）+ 孤儿（没编进任何在世星座）。
      // 2026.9.21 孤儿加"出生>7天"门槛：刚抄的碎片本来就还没轮到编故事，当晚就进清理
      // 列表=诱导误删新记忆（用户 9.21 反馈"为什么有今天晚上的聊天"）；给书记员一周时间
      const cited = new Set();
      CONS.forEach(c => c.fragments.forEach(f => f.id && cited.add(f.id)));
      const v1cut = new Date(2026, 8, 2).getTime() / 1000;   // 9.2 零点：记忆v2改造线
      const orphanCut = Date.now() / 1000 - 7 * 86400;        // 7 天内出生的不算孤儿
      TIDY_POOL = RAW_FRAGS
        .filter(f => !f.retired)
        .map(f => ({ id: f.id, text: f.text, ts: f.ts, type: f.type || "observation",
                     ew: f.ew, date: f.date || "",
                     isV1: (f.ts || 0) < v1cut,
                     isOrphan: !cited.has(f.id) && (f.ts || 0) < orphanCut }))
        .filter(f => f.isV1 || f.isOrphan);
    }

    function buildLayout() {
      GALAXIES.forEach(g => {
        const az = g.azimuth * Math.PI / 180;
        g.cx = Math.cos(az) * RX; g.cy = Math.sin(az) * RY;
        g.cons = CONS.filter(c => c.galaxy === g.id);
        g.count = g.cons.reduce((s, c) => s + c.fragments.length, 0);
        // 星系副标题口径（2026.9.25g）：在池碎片（未判废），与卡片清单一致
        g.fragLive = g.cons.reduce((s, c) => s + c.fragments.filter(f => !f.retired).length, 0);
        // 2026.9.2 用户定版：总览五个星系大小均等——谁的故事多只写在标签里，不体现在个头上
        g.blobR = 215;
        g.rgb = hslToRgb(g.hue, 55 + (strHash(g.id) % 15), 65 + (strHash(g.id + "x") % 10));
        g.label = g.id === "关于我们" ? "关于我们" : g.id + "星系";
        g.puffs = [];
        for (let p = 0; p < 7; p++) {
          const pa = rand01(g.id + "p" + p) * Math.PI * 2;
          const pd = g.blobR * (0.25 + 0.5 * rand01(g.id + "d" + p));
          g.puffs.push({ x: Math.cos(pa) * pd, y: Math.sin(pa) * pd * 0.85,
            r: g.blobR * (0.3 + 0.35 * rand01(g.id + "r" + p)), a: 0.035 + 0.04 * rand01(g.id + "a" + p) });
        }
        // 总览装饰星：每星系固定 12 颗（视觉密度均衡）；真实星座数点进去看
        g.stars = [];
        for (let k = 0; k < 12; k++) {
          const sa = rand01(g.id + "sa" + k) * Math.PI * 2;
          const sd = g.blobR * 0.78 * Math.sqrt(rand01(g.id + "sd" + k));
          g.stars.push({
            x: g.cx + Math.cos(sa) * sd, y: g.cy + Math.sin(sa) * sd * 0.88,
            r: 0.9 + rand01(g.id + "sr" + k) * 1.1,
            a: 0.3 + rand01(g.id + "sy" + k) * 0.35,
          });
        }
      });
      // 2026.9.2 用户定版：一颗星 = 一段叙事（不再多星连线拼"星座"图形）；碎片零图形化，只在卡片清单里
      CONS.forEach(c => {
        const g = GALAXIES.find(x => x.id === c.galaxy) || GALAXIES[3];
        c.galaxyDef = g;
        c.cr = 20 + c.sig * 3;   // 布局推开 & 点击命中的范围
        const ki = g.cons.indexOf(c);
        const kt = (ki + 0.5) / Math.max(1, g.cons.length);
        const rad = g.blobR * 0.62 * Math.pow(kt, 0.6);
        const ang = ki * 2.399963 + (strHash(g.id) % 628) / 100;
        c.x = g.cx + Math.cos(ang) * rad;
        c.y = g.cy + Math.sin(ang) * rad;
      });
      for (let iter = 0; iter < 60; iter++) {
        let moved = false;
        for (let i = 0; i < CONS.length; i++) for (let j = i + 1; j < CONS.length; j++) {
          const a = CONS[i], b = CONS[j];
          const dx = b.x - a.x, dy = b.y - a.y, dist = Math.hypot(dx, dy) || 0.01;
          const need = a.cr + b.cr + 18;
          if (dist < need) {
            const push = (need - dist) / 2, ux = dx / dist, uy = dy / dist;
            a.x -= ux * push; a.y -= uy * push; b.x += ux * push; b.y += uy * push; moved = true;
          }
        }
        if (!moved) break;
      }
    }

    function resize() {
      dpr = window.devicePixelRatio || 1;
      W = box.clientWidth || 600; H = box.clientHeight || 400;
      canvas.width = W * dpr; canvas.height = H * dpr;
      canvas.style.width = W + "px"; canvas.style.height = H + "px";
    }
    function prerenderBg() {
      bgCache = document.createElement("canvas");
      bgCache.width = W * dpr; bgCache.height = H * dpr;
      const b = bgCache.getContext("2d");
      b.scale(dpr, dpr);
      b.fillStyle = "#0a0e1a"; b.fillRect(0, 0, W, H);
      b.save(); b.globalAlpha = 0.03;
      const mg = b.createLinearGradient(0, H*0.3, W, H*0.7);
      mg.addColorStop(0, "transparent"); mg.addColorStop(0.45, "#c9a96e"); mg.addColorStop(0.55, "#c9a96e"); mg.addColorStop(1, "transparent");
      b.fillStyle = mg; b.fillRect(0, H*0.2, W, H*0.6);
      b.restore();
      for (let i = 0; i < 220; i++) {
        const sx = (Math.sin(i*127.1 + 0.7)*0.5+0.5)*W, sy = (Math.cos(i*311.7 + 0.3)*0.5+0.5)*H;
        b.beginPath(); b.arc(sx, sy, 0.3 + (i % 3) * 0.3, 0, Math.PI*2);
        b.fillStyle = `rgba(255,255,255,${0.06 + (i % 5) * 0.035})`; b.fill();
      }
    }
    function drawGlowDot(x, y, coreR, glowR, rgb, alpha, blurLayers) {
      for (let layer = blurLayers; layer >= 1; layer--) {
        const lr = coreR + (glowR - coreR) * (layer / blurLayers);
        const la = alpha / (layer * 1.5);
        const g = ctx.createRadialGradient(x, y, 0, x, y, lr);
        g.addColorStop(0, `rgba(${rgb},${la})`); g.addColorStop(0.5, `rgba(${rgb},${la * 0.4})`); g.addColorStop(1, "transparent");
        ctx.beginPath(); ctx.arc(x, y, lr, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();
      }
    }
    function drawNebula(g, boost) {
      const b = boost ? 1.6 : 1;
      ctx.save();
      ctx.translate(g.cx, g.cy); ctx.scale(1, 0.88); ctx.translate(-g.cx, -g.cy);
      const core = ctx.createRadialGradient(g.cx, g.cy, 0, g.cx, g.cy, g.blobR);
      core.addColorStop(0, `rgba(${g.rgb},${0.1 * b})`); core.addColorStop(0.45, `rgba(${g.rgb},${0.05 * b})`); core.addColorStop(1, "transparent");
      ctx.fillStyle = core; ctx.beginPath(); ctx.arc(g.cx, g.cy, g.blobR, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
      g.puffs.forEach(p => {
        const px = g.cx + p.x, py = g.cy + p.y;
        const pg = ctx.createRadialGradient(px, py, 0, px, py, p.r);
        pg.addColorStop(0, `rgba(${g.rgb},${p.a * b})`); pg.addColorStop(1, "transparent");
        ctx.fillStyle = pg; ctx.beginPath(); ctx.arc(px, py, p.r, 0, Math.PI * 2); ctx.fill();
      });
      if (g.id === "关于我们") {
        const wg = ctx.createRadialGradient(g.cx, g.cy, 0, g.cx, g.cy, g.blobR * 0.5);
        wg.addColorStop(0, "rgba(255,225,170,0.07)"); wg.addColorStop(1, "transparent");
        ctx.fillStyle = wg; ctx.beginPath(); ctx.arc(g.cx, g.cy, g.blobR * 0.5, 0, Math.PI * 2); ctx.fill();
      }
    }

    function draw() {
      if (!bgCache) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.drawImage(bgCache, 0, 0, W, H);
      const cx = W/2 + camX, cy = H/2 + camY;
      ctx.save(); ctx.translate(cx, cy); ctx.scale(scale, scale);
      if (mode === "universe") {
        ctx.globalCompositeOperation = "lighter";
        GALAXIES.forEach(g => drawNebula(g, hoveredGal === g));
        ctx.globalCompositeOperation = "source-over";
        // 总览：每星系等量装饰星，五个看着差不多；谁故事多只在标签里，点进去才见真实疏密
        GALAXIES.forEach(g => g.stars.forEach(s => drawGlowDot(s.x, s.y, s.r * 0.5, s.r * 3, g.rgb, s.a, 2)));
      } else {
        const g = currentGal;
        ctx.globalCompositeOperation = "lighter";
        const amb = ctx.createRadialGradient(g.cx, g.cy, 0, g.cx, g.cy, g.blobR * 2.2);
        amb.addColorStop(0, `rgba(${g.rgb},0.055)`); amb.addColorStop(0.55, `rgba(${g.rgb},0.022)`); amb.addColorStop(1, "transparent");
        ctx.fillStyle = amb; ctx.beginPath(); ctx.arc(g.cx, g.cy, g.blobR * 2.2, 0, Math.PI * 2); ctx.fill();
        ctx.globalCompositeOperation = "source-over";
        // 每颗星=一段叙事：大小看分量(significance)，亮度看情绪；不做多星连线图形
        g.cons.forEach(c => {
          const hot = hoveredCon === c || pinnedCon === c;
          const baseR = 2.2 + c.sig * 0.5;
          const sizeK = hot ? 1.9 : 1, glowK = hot ? 6.5 : 3.6;
          const drawA = hot ? 1 : 0.5 + c.ew * 0.35;
          drawGlowDot(c.x, c.y, baseR * sizeK * 0.4, baseR * sizeK * glowK, g.rgb, drawA, hot ? 4 : 3);
        });
      }
      ctx.restore();
      ctx.globalCompositeOperation = "source-over";
      if (mode === "universe") {
        GALAXIES.forEach(g => {
          const sx = cx + g.cx * scale, sy = cy + g.cy * scale;
          const hot = hoveredGal === g;
          ctx.textAlign = "center";
          ctx.font = "13px system-ui, sans-serif";
          ctx.fillStyle = `rgba(${g.rgb},${hot ? 1 : 0.7})`;
          ctx.fillText(g.label, sx, sy - g.blobR * scale - 16);
          ctx.font = "10px system-ui, sans-serif";
          ctx.fillStyle = `rgba(255,255,255,${hot ? 0.55 : 0.3})`;
          ctx.fillText(g.cons.length + " 个星座", sx, sy - g.blobR * scale - 3);
        });
        $s("stCount").textContent = FRAG_TOTAL + " 条记忆 · " + CONS.length + " 个星座";
        $s("stCountSub").style.display = "none";   // 副标题只在星系里出现（2026.9.25g）
      } else {
        const g = currentGal;
        g.cons.forEach(c => {
          const sx = cx + c.x * scale, sy = cy + c.y * scale;
          const hot = hoveredCon === c || pinnedCon === c;
          ctx.textAlign = "center";
          ctx.font = (hot ? "13px" : "12px") + " system-ui, sans-serif";
          ctx.fillStyle = `rgba(${g.rgb},${hot ? 1 : 0.55})`;
          ctx.fillText(c.title, sx, sy - c.cr * scale - 10);
          ctx.font = "9.5px system-ui, sans-serif";
          ctx.fillStyle = `rgba(255,255,255,${hot ? 0.5 : 0.28})`;
          ctx.fillText("分量 " + c.sig, sx, sy - c.cr * scale + 3);
        });
        if (hoveredCon && !pinnedCon) {
          const sx = cx + hoveredCon.x * scale, sy = cy + hoveredCon.y * scale;
          ctx.setLineDash([3, 5]);
          ctx.strokeStyle = `rgba(${g.rgb},0.25)`; ctx.lineWidth = 0.8;
          ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(W - 200, H * 0.5); ctx.stroke();
          ctx.setLineDash([]);
        }
        if (pinnedCon) {
          const sx = cx + pinnedCon.x * scale, sy = cy + pinnedCon.y * scale;
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = "rgba(201,169,110,0.5)"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.arc(sx, sy, 26 * scale, 0, Math.PI*2); ctx.stroke();
          ctx.setLineDash([]);
        }
        $s("stCount").textContent = g.label + " · " + g.cons.length + " 个星座";
        $s("stCountSub").textContent = g.fragLive + " 个碎片";   // 副标题小字（2026.9.25g）
        $s("stCountSub").style.display = "";
      }
    }

    function animateCam(tx, ty, ts, dur) {
      anim = { t0: performance.now(), dur: dur || 750, fx: camX, fy: camY, fs: scale, tx, ty, ts };
      hoveredCon = null; hoveredGal = null;
      $s("stTip").style.display = "none";
      requestAnimationFrame(stepAnim);
    }
    function stepAnim(now) {
      if (!anim) return;
      let p = (now - anim.t0) / anim.dur;
      if (p >= 1) { camX = anim.tx; camY = anim.ty; scale = anim.ts; anim = null; draw(); return; }
      const e = p < 0.5 ? 4*p*p*p : 1 - Math.pow(-2*p + 2, 3) / 2;
      camX = anim.fx + (anim.tx - anim.fx) * e;
      camY = anim.fy + (anim.ty - anim.fy) * e;
      scale = anim.fs + (anim.ts - anim.fs) * e;
      draw();
      requestAnimationFrame(stepAnim);
    }

    function fitScale() {
      let maxX = 0, maxY = 0;
      GALAXIES.forEach(g => { maxX = Math.max(maxX, Math.abs(g.cx) + g.blobR); maxY = Math.max(maxY, Math.abs(g.cy) + g.blobR); });
      return Math.max(0.3, Math.min(W * 0.5 / (maxX + 90), H * 0.5 / (maxY + 110), 1.05));
    }
    function clearCard() {
      pinnedCon = null; hoveredCon = null;
      $s("stCard").classList.remove("show", "pinned");
      $s("stPinBtn").classList.remove("active");
    }
    function setPill(name) {
      document.querySelectorAll(".st-fbtn").forEach(x => x.classList.toggle("on", x.textContent === name));
    }
    function enterGalaxy(g) {
      if (anim || (mode === "galaxy" && currentGal === g)) return;
      mode = "galaxy"; currentGal = g;
      clearCard();
      $s("stBack").style.display = "block";
      setPill(g.id);
      $s("stHint").textContent = "Esc 返回星海 · 点击星座看故事 · 滚轮缩放 · 拖拽平移";
      const gs = Math.max(2.4, Math.min(6, Math.min(W, H) / (2.3 * g.blobR)));
      g.gs = gs;
      animateCam(-g.cx * gs, -g.cy * gs, gs, 800);
    }
    function surface() {
      if (anim || mode === "universe") return;
      mode = "universe"; currentGal = null;
      clearCard();
      $s("stBack").style.display = "none";
      setPill("全部");
      $s("stHint").textContent = "点击星系潜入 · 滚轮缩放 · 拖拽平移";
      animateCam(0, 0, fitScale(), 800);
    }
    $s("stBack").addEventListener("click", surface);
    window.addEventListener("keydown", e => {
      if (e.key !== "Escape" || !starActive()) return;
      // 分层关闭：先编辑抽屉/选择浮层 → 各清单 → 才退回星海（2026.9.11 手编配套）
      if ($s("stEdit").classList.contains("show")) { closeFragEditor(); return; }
      if ($s("stMergeList").classList.contains("show")) { $s("stMergeList").classList.remove("show"); return; }
      if ($s("stTidy").classList.contains("show")) { $s("stTidy").classList.remove("show"); return; }
      if ($s("stStaleList").classList.contains("show")) { $s("stStaleList").classList.remove("show"); return; }
      if ($s("stVoidList").classList.contains("show")) { $s("stVoidList").classList.remove("show"); return; }
      surface();
    });

    function findGalaxy(mx, my) {
      const cx = W/2 + camX, cy = H/2 + camY;
      let best = null, bestD = Infinity;
      GALAXIES.forEach(g => {
        const d = Math.hypot(mx - (cx + g.cx * scale), my - (cy + g.cy * scale));
        if (d < g.blobR * scale * 0.85 && d < bestD) { best = g; bestD = d; }
      });
      return best;
    }
    function findConstellation(mx, my) {
      if (mode !== "galaxy") return null;
      const cx = W/2 + camX, cy = H/2 + camY;
      let bestC = null, bestCD = Infinity;
      currentGal.cons.forEach(c => {
        const d = Math.hypot(mx - (cx + c.x * scale), my - (cy + c.y * scale));
        if (d < Math.max(22, c.cr * 0.7) * scale && d < bestCD) { bestC = c; bestCD = d; }
      });
      return bestC;
    }

    canvas.addEventListener("mousemove", (e) => {
      if (anim) return;
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      const tip = $s("stTip");
      if (mode === "universe") {
        const g = findGalaxy(mx, my);
        canvas.style.cursor = g ? "pointer" : "crosshair";
        if (g) {
          $s("stTipDate").textContent = g.label;
          $s("stTipText").textContent = g.cons.length + " 个星座 · 点击潜入";
          tip.style.display = "block";
          tip.style.left = Math.min(mx + 16, W - 300) + "px";
          tip.style.top = (my - 10) + "px";
        } else tip.style.display = "none";
        if (g !== hoveredGal) { hoveredGal = g; draw(); }
        return;
      }
      const c = findConstellation(mx, my);
      if (c) {
        $s("stTipDate").textContent = c.title + " · 分量 " + c.sig;
        $s("stTipText").textContent = c.text.slice(0, 60) + (c.text.length > 60 ? "…" : "");
        tip.style.display = "block";
        tip.style.left = Math.min(mx + 16, W - 300) + "px";
        tip.style.top = (my - 10) + "px";
      } else tip.style.display = "none";
  if (c !== hoveredCon) {
    hoveredCon = c;
    draw();
    if (c && !pinnedCon) showCard(c);
    else if (!c && !pinnedCon) $s("stCard").classList.remove("show");
  }
});
    canvas.addEventListener("mouseleave", () => {
      $s("stTip").style.display = "none";
      hoveredGal = null;
      if (!pinnedCon) { hoveredCon = null; draw(); $s("stCard").classList.remove("show"); }
    });
    canvas.addEventListener("click", (e) => {
      if (anim) return;
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      if (mode === "universe") {
        const g = findGalaxy(mx, my);
        if (g) enterGalaxy(g);
        return;
      }
      const c = findConstellation(mx, my);
      if (c) {
        if (pinnedCon && pinnedCon === c) {
          pinnedCon = null;
          $s("stCard").classList.remove("show", "pinned");
          $s("stPinBtn").classList.remove("active");
          hoveredCon = null; draw(); return;
        }
        pinnedCon = c; hoveredCon = c;
        showCard(c);
        $s("stCard").classList.add("pinned");
        $s("stPinBtn").classList.add("active");
        draw();
      }
    });
    $s("stPinBtn").addEventListener("click", () => {
      pinnedCon = null;
      $s("stCard").classList.remove("show", "pinned");
      $s("stPinBtn").classList.remove("active");
      hoveredCon = null; draw();
    });

    function zoomAt(mx, my, ns) {
      if (ns === scale) return;
      const wx = (mx - (W/2 + camX)) / scale, wy = (my - (H/2 + camY)) / scale;
      camX = mx - W/2 - wx * ns; camY = my - H/2 - wy * ns; scale = ns;
    }
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (anim) return;
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      const f = e.deltaY < 0 ? 1.035 : 0.966;
      if (mode === "universe") {
        if (e.deltaY < 0) {
          const g = findGalaxy(mx, my);
          if (g) { enterGalaxy(g); return; }
        }
        const fs = fitScale();
        zoomAt(mx, my, Math.max(fs * 0.55, Math.min(fs * 1.9, scale * f)));
      } else {
        // 退出线压到 1.06、下限 1.0：从进入视野(约2.4)起要连缩 24 次左右才回星海——看全景随便缩
        if (e.deltaY > 0 && scale * f < 1.06) { surface(); return; }
        zoomAt(mx, my, Math.max(1.0, Math.min(9, scale * f)));
      }
      draw();
    }, { passive: false });

    let dragging = false, dragStart = null;
    canvas.addEventListener("mousedown", (e) => { dragging = true; dragStart = {x: e.clientX, y: e.clientY}; });
    window.addEventListener("mousemove", (e) => {
      if (!dragging || anim || !starActive()) return;
      camX += e.clientX - dragStart.x; camY += e.clientY - dragStart.y;
      dragStart = {x: e.clientX, y: e.clientY};
      draw();
    });
    window.addEventListener("mouseup", () => { dragging = false; });

    function showCard(c) {
      const ewText = c.ew >= 0.7 ? "高情绪" : c.ew >= 0.4 ? "中情绪" : "· 平淡";
      $s("stCDate").textContent = c.galaxy + "星系 · 分量 " + c.sig + "/10";
      $s("stCTitle").textContent = c.title;
      let ageLine = "叙事整合于 " + c.date + " · " + c.fragments.length + " 条碎片";
      if (c.manual_stale) ageLine = "⟳ 待重编（源碎片被手改过，旧故事已停用）· " + c.fragments.length + " 条碎片";
      $s("stCAge").textContent = ageLine;
      $s("stCText").textContent = c.text;
      $s("stCEw").textContent = ewText;
      $s("stCType").textContent = c.manual_stale ? "叙事 · 待重编" : "叙事";
      const fr = $s("stCFrags");
      fr.innerHTML = "";
      c.fragments.forEach(n => {
        const div = document.createElement("div");
        div.className = "st-frag" + (n.retired ? " gone" : "");
        div.title = "点击编辑这条碎片";
        const d = document.createElement("span"); d.className = "f-date"; d.textContent = n.date;
        const t = document.createElement("span"); t.textContent = n.text;
        div.appendChild(d); div.appendChild(t);
        if (n.edited || n.corrected) {
          const fl = document.createElement("span"); fl.className = "f-flag";
          fl.textContent = n.edited ? "·已手改" : "·已纠正";
          div.appendChild(fl);
        }
        if (n.retired) {
          const fl = document.createElement("span"); fl.className = "f-flag"; fl.textContent = "·已判废";
          div.appendChild(fl);
        }
        div.addEventListener("click", (e) => { e.stopPropagation(); openFragEditor(c, n); });
        fr.appendChild(div);
      });
      $s("stCard").classList.add("show");
    }

    // 筛选 = 导航
    const FILTERS = ["全部", "关于我们", "爱好", "社交", "事件", "地点"];
    const fEl = $s("stFilters");
    FILTERS.forEach(f => {
      const b = document.createElement("button");
      b.className = "st-fbtn" + (f === "全部" ? " on" : "");
      b.textContent = f;
      b.addEventListener("click", () => {
        if (f === "全部") surface();
        else { const g = GALAXIES.find(x => x.id === f); if (g) enterGalaxy(g); }
      });
      fEl.appendChild(b);
    });

    // ============ 星图手动编辑（2026.9.11 单元B）——他巡检账本的扫帚 ============
    // 后端：memory.py 手动编辑区块 + server.py 5 路由。操作后本地同步数据并重绘，
    // 不重拉不打断当前视图；改碎片 → 星座标待重编 → 书记员下轮（或手动送）重编。
    let editCtx = null;   // {con, frag} 正在编辑的碎片

    function flashHint(text) {
      const h = $s("stHint");
      const old = h.textContent;
      h.textContent = text;
      setTimeout(() => { if (h.textContent === text) h.textContent = old; }, 3000);
    }

    function updateStaleBadge() {
      const n = CONS.filter(c => c.manual_stale).length;
      $s("stStaleBadge").style.display = n ? "block" : "none";
      $s("stStaleNum").textContent = n;
      const v = VOIDED.length;
      $s("stVoidBadge").style.display = v ? "block" : "none";
      $s("stVoidNum").textContent = v;
    }

    async function apiJSON(url, method, body) {
      const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                   body: body !== undefined ? JSON.stringify(body) : null });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.reason || ("HTTP " + r.status));
      return d;
    }

    function openFragEditor(con, frag) {
      editCtx = { con, frag };
      $s("stEditSub").textContent = (con.title || "") + " 的碎片 · " + (frag.id || "") + (frag.date ? " · " + frag.date : "");
      $s("stEditOrig").textContent = frag.text;
      $s("stEditText").value = frag.text;
      $s("stEditNote").value = "";
      const rb = $s("stEditRetire");
      if (frag.retired) { rb.textContent = "↩ 恢复这条"; rb.classList.remove("danger"); }
      else { rb.textContent = "判废这条"; rb.classList.add("danger"); }
      $s("stEdit").classList.add("show");
      $s("stEditText").focus();
    }

    function closeFragEditor() { $s("stEdit").classList.remove("show"); editCtx = null; }

    function refreshCardIfVisible(con) {
      if (pinnedCon === con || hoveredCon === con) showCard(con);
    }

    async function saveFragEdit() {
      if (!editCtx) return;
      const { con, frag } = editCtx;
      const text = $s("stEditText").value.trim();
      const note = $s("stEditNote").value.trim();
      try {
        if (text && text !== frag.text) {
          await apiJSON("/api/fragments/" + frag.id, "PATCH", { text, note });
          frag.text = text; frag.edited = true; frag.retired = false;
          con.manual_stale = true;
          flashHint("碎片已改——引用它的星座标了「待重编」，书记员下轮会重新整理");
        } else {
          flashHint("正文没变化（判废/恢复不受影响）");
        }
        updateStaleBadge(); closeFragEditor(); refreshCardIfVisible(con);
      } catch (e) { flashHint("没改成：" + e.message); }
    }

    async function toggleFragRetire() {
      if (!editCtx) return;
      const { con, frag } = editCtx;
      try {
        if (frag.retired) {
          await apiJSON("/api/fragments/" + frag.id + "/restore", "POST");
          frag.retired = false;
          flashHint("碎片已放回池子");
        } else {
          await apiJSON("/api/fragments/" + frag.id + "/retire", "POST", {});
          frag.retired = true; con.manual_stale = true;
          flashHint("已判废（不注入不重编，数据保留可反悔）——星座标「待重编」");
        }
        updateStaleBadge(); closeFragEditor(); refreshCardIfVisible(con);
      } catch (e) { flashHint("没改成：" + e.message); }
    }

    async function removeConstellation(con, reweave) {
      try {
        await apiJSON("/api/narratives/" + con.id + (reweave ? "/reweave" : "/void"), "POST");
        VOIDED.push({ id: con.id, title: con.title, galaxy: con.galaxy,
                      voided_by: "manual", fragments: con.fragments.length });
        CONS = CONS.filter(c => c !== con);
        if (pinnedCon === con) clearCard();
        if (hoveredCon === con) hoveredCon = null;
        buildLayout(); updateStaleBadge(); draw();
        flashHint(reweave ? "已送书记员重编——过几分钟刷新星图看新故事" : "星座已作废，源碎片回池等书记员重编（顶部「已作废」可反悔）");
      } catch (e) { flashHint("没做成：" + e.message); }
    }

    // 已作废清单（2026.9.11 用户反馈补）：反悔入口——恢复走全量重拉（恢复是低频操作）
    function renderVoidList() {
      const box = $s("stVoidItems");
      box.innerHTML = "";
      if (!VOIDED.length) {
        box.innerHTML = '<div class="empty">没有作废过的星座。</div>';
        return;
      }
      VOIDED.forEach(v => {
        const item = document.createElement("div"); item.className = "item";
        const t = document.createElement("div"); t.className = "t";
        const b = document.createElement("b"); b.textContent = v.title;
        const sp = document.createElement("span");
        sp.textContent = (v.galaxy || "") + "星系 · " + (v.fragments || 0) + " 条碎片 · " +
                         (v.voided_by === "merge" ? "合并退役" : "手动作废");
        t.appendChild(b); t.appendChild(sp);
        const rs = document.createElement("button"); rs.textContent = "放回星图";
        rs.addEventListener("click", async () => {
          try {
            await apiJSON("/api/narratives/" + v.id + "/restore", "POST");
            flashHint("已放回星图");
            await window.initStarView();
            renderVoidList();
          } catch (e) { flashHint("没恢复成：" + e.message); }
        });
        item.appendChild(t); item.appendChild(rs);
        box.appendChild(item);
      });
    }

    // 并入（2026.9.11 用户反馈"同一件事分开记"）：选目标 → 两个都送书记员合并重编
    async function mergeConstellation(con, target) {
      try {
        await apiJSON("/api/narratives/merge", "POST", { ids: [con.id, target.id] });
        VOIDED.push({ id: con.id, title: con.title, galaxy: con.galaxy, voided_by: "merge", fragments: con.fragments.length },
                     { id: target.id, title: target.title, galaxy: target.galaxy, voided_by: "merge", fragments: target.fragments.length });
        CONS = CONS.filter(c => c !== con && c !== target);
        if (pinnedCon === con || pinnedCon === target) clearCard();
        if (hoveredCon === con || hoveredCon === target) hoveredCon = null;
        buildLayout(); updateStaleBadge(); draw();
        flashHint("已送书记员把两段故事合并重编——过几分钟刷新星图看整合版");
      } catch (e) { flashHint("没合并成：" + e.message); }
    }

    function openMergePicker(con) {
      $s("stMergeTitle").textContent = "⇄ 「" + con.title + "」并入哪个星座？";
      const box = $s("stMergeItems");
      box.innerHTML = "";
      const others = CONS.filter(c => c !== con)
        .sort((a, b) => (a.galaxy === con.galaxy ? 0 : 1) - (b.galaxy === con.galaxy ? 0 : 1));
      if (!others.length) {
        box.innerHTML = '<div class="empty">没有别的星座可以并。</div>';
      }
      others.forEach(c => {
        const item = document.createElement("div"); item.className = "item";
        const t = document.createElement("div"); t.className = "t";
        const b = document.createElement("b"); b.textContent = c.title;
        const sp = document.createElement("span");
        sp.textContent = c.galaxy + "星系 · " + c.fragments.length + " 条碎片 · " + c.text.slice(0, 30) + "…";
        t.appendChild(b); t.appendChild(sp);
        item.appendChild(t);
        item.addEventListener("click", () => {
          $s("stMergeList").classList.remove("show");
          mergeConstellation(con, c);
        });
        box.appendChild(item);
      });
      $s("stMergeList").classList.add("show");
    }

    // ============ 档案整理（单元C，2026.9.16；2026.9.21 改拉表打勾式）——清库存扫帚 ============
    // 用户拍板"太多了看不过来"：弃逐条过目，改滚轮滑全量列表+勾选+批量判废（软删可撤销）。
    // 不勾=留着（"跳过"概念消失）；9.16 的已清进度 localStorage 沿用，清过的不重列。
    let tidyQueue = [], tidyFilter = "all", tidyUndoStack = [], tidyPicked = new Set();
    const TIDY_DONE_KEY = "stTidyDone";

    function tidyDone() {
      try { return JSON.parse(localStorage.getItem(TIDY_DONE_KEY) || "[]"); }
      catch (e) { return []; }
    }
    function tidySaveDone(arr) { try { localStorage.setItem(TIDY_DONE_KEY, JSON.stringify(arr)); } catch (e) {} }

    function tidyBuildQueue() {
      const done = new Set(tidyDone());
      tidyQueue = TIDY_POOL.filter(f =>
        !done.has(f.id) &&
        (tidyFilter === "all" || (tidyFilter === "v1" && f.isV1) || (tidyFilter === "orphan" && f.isOrphan)));
      tidyPicked.clear();
    }

    function tidyRenderChips() {
      const chips = [["all", "全部"], ["v1", "v1 老货"], ["orphan", "孤儿"]];
      const el = $s("stTidyChips");
      el.innerHTML = "";
      chips.forEach(([k, label]) => {
        const b = document.createElement("button");
        b.className = k === tidyFilter ? "on" : "";
        const n = TIDY_POOL.filter(f => !tidyDone().includes(f.id) &&
          (k === "all" || (k === "v1" && f.isV1) || (k === "orphan" && f.isOrphan))).length;
        b.textContent = `${label} ${n}`;
        b.addEventListener("click", () => { tidyFilter = k; tidyBuildQueue(); tidyRenderChips(); tidyShow(); });
        el.appendChild(b);
      });
    }

    function tidyUpdateOps() {
      $s("stTidyRetire").textContent = `判废勾选的（${tidyPicked.size}）`;
      $s("stTidyRetire").disabled = !tidyPicked.size;
      $s("stTidyClear").style.display = tidyPicked.size ? "" : "none";
      $s("stTidyProgress").textContent = TIDY_POOL.length
        ? `待过 ${tidyQueue.filter(f => !f.gone).length} 条 · 已勾 ${tidyPicked.size}`
        : "这一档是空的";
    }

    function tidyShow() {
      const box = $s("stTidyList");
      box.innerHTML = "";
      if (!tidyQueue.length) {
        box.innerHTML = `<div class="tidy-done">这一档没有待过的了。<br>判废过的可以随时在这里撤销；收工后刷新星图看最新状态。</div>`;
      }
      tidyQueue.forEach(f => {
        const row = document.createElement("label"); row.className = "tidy-row";
        if (f.gone) row.classList.add("gone");
        const cb = document.createElement("input"); cb.type = "checkbox";
        cb.checked = tidyPicked.has(f.id);
        cb.disabled = !!f.gone;
        cb.addEventListener("change", () => {
          if (cb.checked) tidyPicked.add(f.id); else tidyPicked.delete(f.id);
          tidyUpdateOps();
        });
        const d = document.createElement("span"); d.className = "d"; d.textContent = (f.date || "").replace(/^2026\./, "");
        const t = document.createElement("span"); t.className = "t"; t.textContent = f.text; t.title = f.text;
        const g = document.createElement("span"); g.className = "g";
        g.textContent = f.isV1 ? "v1" : (f.isOrphan ? "孤儿" : "");
        row.appendChild(cb); row.appendChild(d); row.appendChild(t);
        if (g.textContent) row.appendChild(g);
        box.appendChild(row);
      });
      tidyUpdateOps();
      tidyRenderUndo();
    }

    function tidyRenderUndo() {
      const wrap = $s("stTidyUndoWrap");
      if (!tidyUndoStack.length) { wrap.style.display = "none"; return; }
      wrap.style.display = "block";
      $s("stTidyUndo").innerHTML = `↩ 撤销这轮判废（共 ${tidyUndoStack.length} 条，全部恢复）`;
    }

    async function tidyRetire() {
      if (!tidyPicked.size) return;
      const picks = tidyQueue.filter(f => tidyPicked.has(f.id) && !f.gone);
      let ok = 0;
      for (const f of picks) {
        try {
          await apiJSON("/api/fragments/" + f.id + "/retire", "POST", {});
          const done = tidyDone();
          if (!done.includes(f.id)) done.push(f.id);
          tidySaveDone(done);
          f.gone = true;
          ok++;
        } catch (e) { flashHint(`判废 ${f.id} 没成：${e.message}（已停，剩下的下次再试）`); break; }
      }
      if (ok) {
        tidyUndoStack.push(...picks.slice(0, ok));
        tidyPicked.clear();
        flashHint(`已判废 ${ok} 条（软删可撤销）——刷新星图生效`);
      }
      tidyShow();
    }

    async function tidyUndo() {
      const batch = tidyUndoStack.splice(0);
      if (!batch.length) return;
      for (const f of batch) {
        try {
          await apiJSON("/api/fragments/" + f.id + "/restore", "POST");
          tidySaveDone(tidyDone().filter(x => x !== f.id));
          f.gone = false;
        } catch (e) { flashHint("恢复" + f.id + "没成：" + e.message); }
      }
      tidyRenderChips(); tidyShow();
    }

    $s("stTidyBtn").addEventListener("click", () => {
      tidyBuildQueue(); tidyRenderChips(); tidyShow();
      $s("stTidy").classList.add("show");
    });
    $s("stTidyRetire").addEventListener("click", () => armConfirm($s("stTidyRetire"),
      `判废勾选的（${tidyPicked.size}）`, tidyRetire));
    $s("stTidyClear").addEventListener("click", () => { tidyPicked.clear(); tidyShow(); });
    $s("stTidyUndo").addEventListener("click", tidyUndo);
      $s("stTidyClose").addEventListener("click", () => $s("stTidy").classList.remove("show"));
      $s("stTidyX").addEventListener("click", () => $s("stTidy").classList.remove("show"));   // 右上角叉（2026.9.25h）

    // 危险操作的确认方式：第一次点变成"确定…？"，3 秒内再点才执行（不弹系统对话框）
    function armConfirm(btn, label, action) {
      if (btn.dataset.armed) { btn.dataset.armed = ""; btn.textContent = label; action(); return; }
      btn.dataset.armed = "1";
      btn.textContent = "确定" + label.replace(/^[^\u4e00-\u9fa5]+/, "") + "？";
      setTimeout(() => { if (btn.dataset.armed) { btn.dataset.armed = ""; btn.textContent = label; } }, 3000);
    }

    function renderStaleList() {
      const stale = CONS.filter(c => c.manual_stale);
      const box = $s("stStaleItems");
      box.innerHTML = "";
      if (!stale.length) {
        box.innerHTML = '<div class="empty">没有待重编的星座——都编好了。</div>';
        return;
      }
      stale.forEach(c => {
        const item = document.createElement("div"); item.className = "item";
        const t = document.createElement("div"); t.className = "t";
        const b = document.createElement("b"); b.textContent = c.title;
        const sp = document.createElement("span");
        sp.textContent = c.galaxy + "星系 · " + c.fragments.length + " 条碎片";
        t.appendChild(b); t.appendChild(sp);
        const go = document.createElement("button"); go.textContent = "去看看";
        go.addEventListener("click", () => {
          $s("stStaleList").classList.remove("show");
          const g = GALAXIES.find(x => x.id === c.galaxy);
          if (g) enterGalaxy(g);
        });
        const rw = document.createElement("button"); rw.textContent = "送重编";
        rw.addEventListener("click", async () => {
          await removeConstellation(c, true);
          renderStaleList();
        });
        item.appendChild(t); item.appendChild(go); item.appendChild(rw);
        box.appendChild(item);
      });
    }

    $s("stEditSave").addEventListener("click", saveFragEdit);
    $s("stEditRetire").addEventListener("click", toggleFragRetire);
    $s("stEditCancel").addEventListener("click", closeFragEditor);
    $s("stEditText").addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") saveFragEdit();
    });
    $s("stStaleBadge").addEventListener("click", () => { renderStaleList(); $s("stStaleList").classList.add("show"); });
    $s("stStaleClose").addEventListener("click", () => $s("stStaleList").classList.remove("show"));
    $s("stVoidBadge").addEventListener("click", () => { renderVoidList(); $s("stVoidList").classList.add("show"); });
    $s("stVoidClose").addEventListener("click", () => $s("stVoidList").classList.remove("show"));
    $s("stMergeClose").addEventListener("click", () => $s("stMergeList").classList.remove("show"));
    $s("stBtnMerge").addEventListener("click", () => { if (pinnedCon) openMergePicker(pinnedCon); });
    $s("stBtnVoid").addEventListener("click", () => {
      if (pinnedCon) armConfirm($s("stBtnVoid"), "✕ 作废星座", () => removeConstellation(pinnedCon, false));
    });
    $s("stBtnReweave").addEventListener("click", () => {
      if (pinnedCon) armConfirm($s("stBtnReweave"), "⟳ 立即重编", () => removeConstellation(pinnedCon, true));
    });

    window.addEventListener("resize", () => {
      if (!starActive()) return;
      resize(); prerenderBg();
      if (mode === "universe" && !anim) { camX = 0; camY = 0; scale = fitScale(); }
      draw();
    });

    // 对外：进入星图视图时调用（openSub('star') → showView 之后）
    // 20260925n 刀6：手机（≤720）走只读卡片流，画布交互（缩放/拖拽/手编辑）仍归桌面
    window.initStarView = async function () {
      if (loading) return;
      loading = true;
      try {
        const r = await fetch("/api/memory-graph");
        const api = await r.json();
        buildData(api);
        if (window.matchMedia("(max-width: 720px)").matches) { renderReadOnly(api); return; }
        document.getElementById("view-star").classList.remove("ro-mode");   // 桌面恢复画布（从手机转屏回来）
        $s("stReadOnly").classList.add("hidden");                           // 只读容器重新藏好，别盖画布
        buildLayout();
        updateStaleBadge();
        requestAnimationFrame(() => {
          resize(); prerenderBg();
          mode = "universe"; currentGal = null;
          clearCard();
          $s("stBack").style.display = "none";
          setPill("全部");
          $s("stHint").textContent = "点击星系潜入 · 滚轮缩放 · 拖拽平移";
          camX = 0; camY = 0; scale = fitScale();
          draw();
        });
      } catch (e) {
        $s("stCount").textContent = "星图数据没取到（账本可能还没准备好）";
        $s("stCountSub").style.display = "none";
      } finally { loading = false; }
    };

    // ---------- 手机只读版：星座卡片流（叙事正文+源碎片折叠），数据同一接口零后端改动 ----------
    function renderReadOnly(api) {
      const view = document.getElementById("view-star");
      view.classList.add("ro-mode");
      const ro = $s("stReadOnly");
      ro.classList.remove("hidden");
      const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
      const d8 = (s) => String(s || "").replace(/^\d{4}\./, "");
      const cons = (api.narratives || []).filter(n => !n.superseded)
        .sort((a, b) => String(b.created || "").localeCompare(String(a.created || "")));
      const voided = (api.voided || []);
      const fragLive = (api.fragments || []).filter(f => f.id && !f.retired).length;   // 在池口径，与星系副标题一致
      const card = (n) => `
        <div class="paper-card st-ro-card">
          <div class="st-ro-head"><span class="st-ro-gal">◆ ${esc(n.galaxy || "事件")}</span><span class="st-ro-date">${esc(d8(n.created))}</span></div>
          <div class="st-ro-title serif">${esc(n.title || "")}</div>
          <div class="st-ro-text">${esc(n.text || "")}</div>
          ${(n.fragments || []).length ? `<details class="st-ro-frags"><summary>源碎片 · ${n.fragments.length} 条</summary>${n.fragments.map(f => `<div class="st-ro-frag${f.retired ? " retired" : ""}"><span class="st-ro-fdate">${esc(d8(f.date))}</span>${esc(f.text || "")}</div>`).join("")}</details>` : ""}
        </div>`;
      ro.innerHTML =
        `<div class="feed-head" style="margin-bottom:10px;">✦ 记忆星图 · 手机版</div>` +
        `<div class="st-ro-count">${cons.length} 个星座 · ${fragLive} 个碎片（在池）${voided.length ? ` · 已作废 ${voided.length}` : ""}</div>` +
        (voided.length ? `<div class="st-ro-filter"><button class="st-ro-fbtn active" data-rof="live">在世</button><button class="st-ro-fbtn" data-rof="voided">已作废</button></div>` : "") +
        `<div id="stRoList">${cons.map(card).join("") || '<div class="sched-empty">还没有编成星座的记忆——多聊聊，书记员会攒出故事来。</div>'}</div>` +
        (voided.length ? `<div id="stRoVoid" class="hidden">${voided.map(v => `
          <div class="paper-card st-ro-card voided">
            <div class="st-ro-head"><span class="st-ro-gal">◆ ${esc(v.galaxy || "事件")}</span><span class="st-ro-date">${esc(d8(v.ts || v.created))}</span></div>
            <div class="st-ro-title serif">${esc(v.title || "")}</div>
            <div class="st-ro-text" style="color:var(--muted);font-size:12px;">已作废（${v.voided_by === "merge" ? "并入合并" : "手动作废"}）· 源碎片已回池等重编</div>
          </div>`).join("")}</div>` : "");
      if (voided.length) {
        ro.querySelectorAll(".st-ro-fbtn").forEach(b => b.addEventListener("click", () => {
          ro.querySelectorAll(".st-ro-fbtn").forEach(x => x.classList.remove("active"));
          b.classList.add("active");
          $s("stRoList").classList.toggle("hidden", b.dataset.rof !== "live");
          $s("stRoVoid").classList.toggle("hidden", b.dataset.rof !== "voided");
        }));
      }
    }
  })();
