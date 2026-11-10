/* learn-core.js: phần dùng chung cho trang TV (video.html), iPad (ipad.html), iPhone (iphone.html) và dashboard.
 *
 * 1. Đồng bộ giữa các thiết bị qua Firebase Realtime Database (REST, không cần thư viện):
 *    - cờ video: 'watched' (đã xem, xếp cuối hàng) | 'archived' (đưa vào Lịch sử học) | '' (bình thường)
 *    - từ đã lưu (★), cũng là từ bay trên màn hình sao băng
 *    Mỗi nhà dùng một MÃ ĐỒNG BỘ (≥ 20 ký tự, tự tạo ở thiết bị đầu tiên). Thiết bị khác tham gia bằng cách
 *    quét mã QR hoặc mở link có ?sync=MÃ. Dữ liệu nằm ở /sync/<MÃ>/{flags,words}; quy tắc bảo mật
 *    (video/firebase-rules.json) chỉ cho đọc/ghi khi biết mã. Hai thiết bị sửa cùng mục: bản mới hơn thắng.
 * 2. Danh mục video: xếp hàng theo chủ đề, video mới lên trước, đã xem xuống cuối, lưu trữ vào Lịch sử học.
 * 3. Tiện ích nhỏ: tải JSON, tô cụm từ trong câu, tìm câu phụ đề theo thời gian, đọc từ, mã QR.
 */
(function () {
  const SYNC_DB = 'https://tv-video-daily-default-rtdb.asia-southeast1.firebasedatabase.app';
  const K = { code: 'learn.sync.code', flags: 'learn.flags.v1', words: 'learn.words.v1', saved: 'meteor-saved-v1', pulled: 'learn.sync.pulled' };
  const POLL_MS = 45000;

  const ls = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    raw(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    rawSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };
  const now = () => Date.now();
  const validCode = c => typeof c === 'string' && /^[a-z0-9-]{20,64}$/.test(c);
  function newCode() {
    const abc = 'abcdefghjkmnpqrstuvwxyz23456789', b = new Uint8Array(24);
    (window.crypto || window.msCrypto).getRandomValues(b);
    let s = ''; for (let i = 0; i < 24; i++) s += abc[b[i] % abc.length];
    return s.replace(/(.{6})(?=.)/g, '$1-');          // ví dụ: k3m9qx-2hfw8z-...
  }
  const wordKey = w => String(w || '').toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 80) || 'w';

  /* ---------- Trạng thái cục bộ ---------- */
  let flags = ls.get(K.flags, {});            // { videoId: { s, t } }
  let words = ls.get(K.words, null);          // { key: { w, ipa, pos, vi, ex, col, added, src, t, del } }
  if (!words) {                               // lần đầu: chuyển từ đã lưu kiểu cũ (mảng) sang dạng có thời gian
    words = {};
    const old = ls.get(K.saved, []);
    (Array.isArray(old) ? old : []).forEach((o, i) => { if (o && o.w) words[wordKey(o.w)] = Object.assign({}, o, { t: now() - i, del: false }); });
    ls.set(K.words, words);
  }
  let code = ls.raw(K.code);
  if (!validCode(code)) { code = newCode(); ls.rawSet(K.code, code); }

  const listeners = new Set();
  const emit = what => listeners.forEach(fn => { try { fn(what); } catch (e) {} });

  function writeSavedArray() {               // dashboard sao băng đọc mảng này
    const arr = Object.values(words).filter(o => !o.del).sort((a, b) => b.t - a.t)
      .map(o => ({ w: o.w, ipa: o.ipa || '', pos: o.pos || 'từ video', vi: o.vi || '', ex: o.ex || '', col: o.col || [], saved: true, added: o.added || '', src: o.src || null }));
    ls.set(K.saved, arr);
  }
  writeSavedArray();

  /* ---------- Firebase REST ---------- */
  const base = () => `${SYNC_DB}/sync/${code}`;
  let online = !!SYNC_DB, lastErr = '', pushTimer = null;
  const pending = {};                         // "flags/xxx" | "words/yyy" → giá trị chờ đẩy

  function queue(path, value) {
    pending[path] = value;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(flush, 300);
  }
  async function flush() {
    if (!SYNC_DB) return;
    const body = Object.assign({}, pending);
    if (!Object.keys(body).length) return;
    Object.keys(body).forEach(k => delete pending[k]);
    try {
      const r = await fetch(`${base()}.json`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      online = true; lastErr = '';
    } catch (e) {
      const newer = Object.assign({}, pending);
      Object.assign(pending, body, newer);    // giữ lại, lần sau đẩy tiếp (mục mới hơn không bị ghi đè)
      online = false; lastErr = String(e.message || e);
    }
    emit('status');
  }

  let pulling = null;
  async function pull() {
    if (!SYNC_DB) return false;
    if (pulling) return pulling;
    pulling = (async () => {
      let changed = false;
      try {
        const r = await fetch(`${base()}.json`, { cache: 'no-store' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const remote = (await r.json()) || {};
        const rf = remote.flags || {}, rw = remote.words || {};
        // Lấy bản mới hơn từ máy chủ
        for (const [id, v] of Object.entries(rf)) {
          if (v && typeof v.t === 'number' && (!flags[id] || v.t > flags[id].t)) { flags[id] = { s: v.s || '', t: v.t }; changed = true; }
        }
        for (const [k, v] of Object.entries(rw)) {
          if (v && typeof v.t === 'number' && (!words[k] || v.t > words[k].t)) { words[k] = v; changed = true; }
        }
        // Đẩy lên những gì máy này mới hơn (kể cả khi vừa tham gia mã mới)
        for (const [id, v] of Object.entries(flags)) if (!rf[id] || rf[id].t < v.t) pending['flags/' + id] = v;
        for (const [k, v] of Object.entries(words)) if (!rw[k] || rw[k].t < v.t) pending['words/' + k] = v;
        if (changed) { ls.set(K.flags, flags); ls.set(K.words, words); writeSavedArray(); }
        online = true; lastErr = '';
        ls.rawSet(K.pulled, String(now()));
        await flush();
      } catch (e) { online = false; lastErr = String(e.message || e); }
      pulling = null;
      emit(changed ? 'data' : 'status');
      return changed;
    })();
    return pulling;
  }

  let pollTimer = null;
  function startPolling() {
    if (pollTimer || !SYNC_DB) return;
    pull();
    pollTimer = setInterval(() => { if (document.visibilityState === 'visible') pull(); }, POLL_MS);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') pull(); });
    window.addEventListener('online', pull);
    window.addEventListener('focus', pull);
  }

  /* ---------- API đồng bộ ---------- */
  const Sync = {
    db: SYNC_DB,
    get code() { return code; },
    get online() { return online; },
    get error() { return lastErr; },
    get lastPull() { return +ls.raw(K.pulled) || 0; },
    on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    start: startPolling,
    pull,
    /* Cờ video */
    flag(id) { const f = flags[id]; return f && f.s ? f.s : ''; },
    flagTime(id) { return flags[id] ? flags[id].t : 0; },
    setFlag(id, s) {
      if (!id) return;
      flags[id] = { s: s || '', t: now() };
      ls.set(K.flags, flags);
      queue('flags/' + id, flags[id]);
      emit('data');
    },
    /* Từ đã lưu */
    words() { return Object.values(words).filter(o => !o.del).sort((a, b) => b.t - a.t); },
    isSaved(w) { const o = words[wordKey(w)]; return !!(o && !o.del); },
    saveWord(o) {
      const k = wordKey(o.w);
      words[k] = Object.assign({}, o, { t: now(), del: false, added: o.added || new Date().toISOString().slice(0, 10) });
      ls.set(K.words, words); writeSavedArray(); queue('words/' + k, words[k]); emit('data');
    },
    removeWord(w) {
      const k = wordKey(w); if (!words[k]) return;
      words[k] = Object.assign({}, words[k], { t: now(), del: true });
      ls.set(K.words, words); writeSavedArray(); queue('words/' + k, words[k]); emit('data');
    },
    /* Mã đồng bộ */
    async join(newC) {
      newC = String(newC || '').trim().toLowerCase();
      if (!validCode(newC)) return false;
      if (newC === code) { await pull(); return true; }
      code = newC; ls.rawSet(K.code, code);
      await pull();                           // gộp dữ liệu máy này vào mã mới
      return true;
    },
    joinLink(page = 'iphone.html') {
      const dir = location.href.replace(/[?#].*$/, '').replace(/[^/]*$/, '');
      return `${dir}${page}?sync=${code}`;
    },
    /* Đọc ?sync=MÃ trên địa chỉ trang (quét QR / mở link), tham gia rồi xoá khỏi địa chỉ */
    async consumeParam() {
      const p = new URLSearchParams(location.search);
      const c = p.get('sync');
      if (!c) return false;
      p.delete('sync');
      const q = p.toString();
      history.replaceState(null, '', location.pathname + (q ? '?' + q : '') + location.hash);
      return Sync.join(c);
    },
  };

  /* ---------- Danh mục video ---------- */
  const getJSON = u => fetch(u, { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null);
  const daysAgo = d => { const t = Date.parse(d || ''); return isNaN(t) ? 9999 : (now() - t) / 86400000; };

  /* Trả về { rows:[{id,title,items}], history:[...], total }. Mỗi item có thêm: flag, isNew, ready.
     Trong hàng: video chưa xem (có cụm từ trước, mới thêm trước) → video đã xem (xếp cuối).
     Video lưu trữ không nằm trong hàng nào mà nằm ở history. newRow: hàng "Mới thêm" ở đầu. */
  function buildRows(catalog, opts = {}) {
    const newDays = opts.newDays ?? 3;
    const vids = (catalog && catalog.videos) || [];
    const titles = Object.fromEntries(((catalog && catalog.rows) || []).map(r => [r.id, r.title]));
    const order = ((catalog && catalog.rows) || []).map(r => r.id);
    const items = vids.map(v => Object.assign({}, v, {
      flag: Sync.flag(v.id), flagT: Sync.flagTime(v.id), ready: (v.n || 0) > 0, isNew: daysAgo(v.added) < newDays,
    }));
    const byRow = {};
    items.forEach(v => { if (v.flag !== 'archived') (byRow[v.row] = byRow[v.row] || []).push(v); });
    const active = (a, b) => (b.ready - a.ready) || String(b.added).localeCompare(String(a.added)) || String(b.published).localeCompare(String(a.published));
    const sortRow = list => list.filter(v => v.flag !== 'watched').sort(active)
      .concat(list.filter(v => v.flag === 'watched').sort((a, b) => b.flagT - a.flagT));
    const ids = order.concat(Object.keys(byRow).filter(id => !order.includes(id)));
    const rows = ids.filter(id => byRow[id] && byRow[id].length)
      .map(id => ({ id, title: titles[id] || id, items: sortRow(byRow[id]) }));
    if (opts.newRow !== false) {
      const fresh = items.filter(v => v.isNew && v.ready && !v.flag).sort(active).slice(0, 20);
      if (fresh.length) rows.unshift({ id: 'new', title: opts.newTitle || 'Mới thêm', items: fresh, isNewRow: true });
    }
    const history = items.filter(v => v.flag === 'archived').sort((a, b) => b.flagT - a.flagT);
    return { rows, history, total: vids.length };
  }

  /* ---------- Tiện ích ---------- */
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = s => { s = Math.max(0, Math.floor(s || 0)); const m = Math.floor(s / 60), x = String(s % 60).padStart(2, '0'); return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${x}` : `${m}:${x}`; };
  const STOP = ['one', "one's", 'someone', "someone's", 'something', 'sth', 'sb', 'the', 'and', 'for', 'your', 'with', 'from', 'oneself'];
  function markQuote(q, phrase) {
    const words = String(phrase || '').toLowerCase().split(/\s+/).filter(w => w.length > 2 && !STOP.includes(w));
    const qs = esc(q || '');
    if (!words.length) return qs;
    return qs.replace(/[A-Za-zÀ-ỹ'’-]+/g, w => words.some(x => w.toLowerCase().startsWith(x.slice(0, Math.max(3, x.length - 2)))) ? `<mark>${w}</mark>` : w);
  }
  function capAt(segs, t) {                  // segs: [[start, dur, text], ...]
    if (!segs || !segs.length) return -1;
    let lo = 0, hi = segs.length - 1, k = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (segs[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
    if (k < 0) return -1;
    return (t <= segs[k][0] + Math.max(segs[k][1], 1.2) + 0.4 || (segs[k + 1] && t < segs[k + 1][0])) ? k : -1;
  }
  function speak(text) {
    if (!('speechSynthesis' in window)) return false;
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(String(text).replace(/\b(someone|something|sb|sth|one's|oneself)\b/gi, '').replace(/\s+/g, ' ').trim() || text);
    u.lang = 'en-US'; u.rate = 0.9;
    const vs = speechSynthesis.getVoices();
    const v = vs.find(x => /en-US/i.test(x.lang) && /Samantha|Ava|Allison|Google US/i.test(x.name)) || vs.find(x => /^en/i.test(x.lang));
    if (v) u.voice = v;
    speechSynthesis.speak(u);
    return true;
  }
  let qrLib = null;
  function qr(el, text, size = 220) {
    qrLib = qrLib || new Promise((res, rej) => {
      if (window.QRCode) return res();
      const s = document.createElement('script');
      s.src = 'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js';
      s.onload = res; s.onerror = rej; document.head.appendChild(s);
    });
    return qrLib.then(() => {
      el.innerHTML = '';
      new window.QRCode(el, { text, width: size, height: size, colorDark: '#111111', colorLight: '#ffffff', correctLevel: window.QRCode.CorrectLevel.M });
    }).catch(() => { el.textContent = 'Không tải được mã QR. Dùng link bên dưới.'; });
  }
  /* iPad mở link iPhone (hoặc ngược lại) thì chuyển sang trang hợp màn hình, giữ nguyên ?v=, ?sync= */
  function routeDevice(page) {
    const short = Math.min(screen.width, screen.height);
    const want = short >= 700 ? 'ipad' : 'iphone';
    if (page !== want && !/[?&]stay=1/.test(location.search)) {
      location.replace(location.pathname.replace(/[^/]*$/, `${want}.html`) + location.search + location.hash);
      return true;
    }
    return false;
  }

  window.LearnCore = { Sync, SYNC_DB, getJSON, buildRows, esc, fmt, markQuote, capAt, speak, qr, routeDevice, wordKey };
})();
