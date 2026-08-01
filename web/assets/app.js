/* Krishna Netra — shared UI runtime: header, badges, WebSocket, helpers */
(function () {
  const KN = {
    ws: null,
    handlers: {},
    state: "active",
    on(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    emit(type, data) { (this.handlers[type] || []).forEach(fn => fn(data)); },

    async api(path, opts = {}) {
      if (opts.json) {
        opts.body = JSON.stringify(opts.json);
        opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
        delete opts.json;
      }
      const r = await fetch(path, opts);
      if (!r.ok) {
        let msg = r.statusText;
        try { msg = (await r.json()).detail || msg; } catch (e) {}
        throw new Error(msg);
      }
      return r.json();
    },

    media(path) { return path ? "/media/" + path : ""; },

    toast(msg, kind = "info", ms = 3000) {
      let el = document.getElementById("kn-toast");
      if (!el) {
        el = document.createElement("div");
        el.id = "kn-toast";
        el.className = "fixed bottom-4 left-1/2 -translate-x-1/2 z-50 px-4 py-2 " +
          "rounded-lg text-sm shadow-lg transition-opacity gujarati";
        document.body.appendChild(el);
      }
      el.style.background = kind === "error" ? "#7f1d1d" :
        kind === "ok" ? "#14532d" : "#1E88A8";
      el.style.color = "#fff";
      el.textContent = msg;
      el.style.opacity = "1";
      clearTimeout(el._t);
      el._t = setTimeout(() => { el.style.opacity = "0"; }, ms);
    },

    /* ── header ─────────────────────────────────────────────────── */
    renderHeader(active) {
      const nav = [
        ["index.html", "ડેશબોર્ડ"], ["live.html", "લાઈવ"],
        ["people.html", "લોકો"], ["visits.html", "મુલાકાત"],
        ["settings.html", "સેટિંગ્સ"],
      ];
      const el = document.getElementById("kn-header");
      if (!el) return;
      el.innerHTML = `
      <header class="sticky top-0 z-40 border-b border-slate-800"
              style="background:#0B1220ee;backdrop-filter:blur(6px)">
        <div class="max-w-6xl mx-auto px-3 py-2 flex flex-wrap items-center gap-x-4 gap-y-2">
          <a href="index.html" class="flex items-center gap-2 shrink-0">
            <span class="text-xl">🦚</span>
            <span class="font-bold text-lg gujarati" style="color:#D4A017">કૃષ્ણ નેત્ર</span>
          </a>
          <nav class="flex gap-1 text-sm gujarati overflow-x-auto">
            ${nav.map(([href, label]) =>
              `<a href="${href}" class="px-2.5 py-1 rounded-md whitespace-nowrap ${
                href === active ? "text-white" : "text-slate-400 hover:text-white"}"
                style="${href === active ? "background:#1E88A8" : ""}">${label}</a>`).join("")}
          </nav>
          <div class="flex items-center gap-2 ml-auto text-xs gujarati">
            <span id="badge-camera" class="kn-badge" title="કેમેરા">🔴 કેમેરા</span>
            <span id="badge-mic" class="kn-badge" title="માઇક">🔴 માઇક</span>
            <span id="badge-ai" class="kn-badge" title="AI મોડેલ">🔴 AI</span>
            <span class="kn-badge opacity-40" title="WhatsApp — Phase 3">⚪ WhatsApp</span>
            <span id="kn-voice" class="kn-badge" style="color:#7dd3fc">🎧 સાંભળી રહ્યો છું</span>
            <button id="kn-pause" class="px-2.5 py-1 rounded-md font-semibold"
                    style="background:#D4A017;color:#0B1220">⏸ થોભાવો</button>
          </div>
        </div>
      </header>`;
      document.getElementById("kn-pause").onclick = () => KN.togglePause();
    },

    setBadge(id, ok, label, detail) {
      const el = document.getElementById(id);
      if (!el) return;
      const icon = ok === "ok" ? "🟢" : ok === "muted" ? "🟡" : "🔴";
      el.textContent = `${icon} ${label}`;
      el.title = detail || label;
    },

    applyStatus(st) {
      // camera: any online camera -> green
      const cams = st.cameras || {};
      const anyOk = Object.values(cams).some(c => c.status === "ok");
      const camDetail = Object.values(cams).map(c => `${c.name}: ${c.status} ${c.detail || ""}`).join("\n");
      KN.setBadge("badge-camera", anyOk ? "ok" : "down",
        anyOk ? "કેમેરા ચાલુ" : "કેમેરા બંધ", camDetail);
      const mic = st.mic || {};
      KN.setBadge("badge-mic", mic.status,
        mic.status === "ok" ? "માઇક ચાલુ છે" :
        mic.status === "muted" ? "માઇક: અવાજ નથી" : "માઇક કનેક્ટ નથી", mic.detail);
      KN.setBadge("badge-ai", st.detector_ready ? "ok" : "down",
        st.detector_ready ? "AI તૈયાર" : "AI લોડ થાય છે…", st.detector_error || "");
      if (st.state) KN.setPauseButton(st.state);
    },

    setVoice(mode, text) {
      const el = document.getElementById("kn-voice");
      if (!el) return;
      if (mode === "speaking") {
        el.textContent = "🔊 બોલી રહ્યો છે";
        el.style.color = "#fbbf24";
        el.title = text || "";
      } else if (mode === "paused") {
        el.textContent = "⏸ થોભાવેલ";
        el.style.color = "#94a3b8";
      } else {
        el.textContent = "🎧 સાંભળી રહ્યો છું";
        el.style.color = "#7dd3fc";
      }
    },

    setPauseButton(state) {
      KN.state = state;
      KN.setVoice(state === "paused" ? "paused" : "idle");
      const b = document.getElementById("kn-pause");
      if (!b) return;
      if (state === "paused") {
        b.textContent = "▶ ચાલુ કરો";
        b.style.background = "#16a34a"; b.style.color = "#fff";
      } else {
        b.textContent = "⏸ થોભાવો";
        b.style.background = "#D4A017"; b.style.color = "#0B1220";
      }
    },

    async togglePause() {
      const next = KN.state === "paused" ? "active" : "paused";
      try {
        await KN.api("/api/system/state", { method: "POST", json: { state: next } });
        KN.setPauseButton(next);
        KN.toast(next === "paused" ? "સિસ્ટમ થોભાવી છે" : "સિસ્ટમ ચાલુ છે", "ok");
      } catch (e) { KN.toast("ભૂલ: " + e.message, "error"); }
    },

    /* ── websocket ──────────────────────────────────────────────── */
    connectWS() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      KN.ws = ws;
      ws.onopen = () => { KN._retry = 1; };
      ws.onmessage = (e) => {
        let msg; try { msg = JSON.parse(e.data); } catch (err) { return; }
        if (msg.type === "hello") { KN.applyStatus(msg.data); }
        if (msg.type === "camera.status" || msg.type === "mic.status") {
          KN.refreshStatusSoon();
        }
        if (msg.type === "agent.speaking") KN.setVoice("speaking", msg.data.text);
        if (msg.type === "agent.done")
          KN.setVoice(KN.state === "paused" ? "paused" : "idle");
        KN.emit(msg.type, msg.data);
      };
      ws.onclose = () => {
        KN.toast("ફરી જોડાઈ રહ્યું છે…", "error", 1800);
        setTimeout(() => KN.connectWS(), Math.min((KN._retry = (KN._retry || 1) * 1.5), 10) * 1000);
      };
      // keepalive
      clearInterval(KN._ka);
      KN._ka = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
    },

    refreshStatusSoon() {
      clearTimeout(KN._rs);
      KN._rs = setTimeout(async () => {
        try { KN.applyStatus(await KN.api("/api/system/status")); } catch (e) {}
      }, 300);
    },

    async init(activePage) {
      KN.renderHeader(activePage);
      KN.connectWS();
      try { KN.applyStatus(await KN.api("/api/system/status")); } catch (e) {}
      setInterval(() => KN.refreshStatusSoon(), 15000);
    },

    fmtUptime(sec) {
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
      if (h > 0) return `${h} કલાક ${m} મિનિટ`;
      return `${m} મિનિટ`;
    },

    relationLabel(r) {
      return ({ admin: "એડમિન", staff: "સ્ટાફ", visitor: "મુલાકાતી", vip: "VIP",
                vendor: "વેન્ડર", blacklist: "બ્લેકલિસ્ટ", unknown: "અજાણ્યા" })[r] || r;
    },
  };
  window.KN = KN;
})();
