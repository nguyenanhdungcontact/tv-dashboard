/* learn-app.js: phần chạy chung của trang iPad (ipad.html) và iPhone (iphone.html).
 * Hai trang chỉ khác bố cục (CSS + khung HTML). iPhone đặt <html data-layout="phone">: thẻ chi tiết cụm từ
 * mở ngay trong danh sách thay vì nằm dưới video.
 * Cần learn-core.js nạp trước (đồng bộ, danh mục, tiện ích).
 */
(function () {
  const LC = window.LearnCore, SYNC = LC.Sync;
  const PHONE = document.documentElement.dataset.layout === 'phone';
  if (LC.routeDevice(PHONE ? 'iphone' : 'ipad')) return;      // mở nhầm trang thì chuyển sang trang hợp màn hình

  const { esc, fmt, markQuote, getJSON } = LC;
  const $ = s => document.querySelector(s);
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };
  let toastT;
  function toast(m, ms = 2400) { const el = $('#toast'); el.textContent = m; el.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove('show'), ms); }

  const S = { catalog: null, index: {}, rows: [], history: [], histOpen: false, view: 'videos', v: null, phr: null, cap: null, sel: 0, now: -1, capIdx: -1, touchHold: 0, sheet: null };
  const allVideos = () => (S.catalog && S.catalog.videos) || [];
  const thumb = id => `https://i.ytimg.com/vi/${encodeURIComponent(id)}/hqdefault.jpg`;

  /* ===== Sáng / tối ===== */
  const THEMES = ['auto', 'day', 'night'], THEME_BTN = { auto: '◐', day: '☀', night: '☾' }, THEME_NAME = { auto: 'Tự động', day: 'Sáng', night: 'Tối' };
  function paintTheme() { document.querySelectorAll('[data-tool="theme"],#themeBtn').forEach(b => { b.textContent = THEME_BTN[themePref()] + (PHONE ? '' : ' ' + THEME_NAME[themePref()]); }); }
  function cycleTheme() {
    if (location.search.includes('theme=')) history.replaceState(null, '', location.pathname + location.search.replace(/[?&]theme=\w+/, '').replace(/^&/, '?'));
    const n = THEMES[(THEMES.indexOf(themePref()) + 1) % 3]; setThemePref(n); paintTheme(); toast(THEME_BTN[n] + ' ' + THEME_NAME[n]);
  }
  $('#themeBtn').onclick = cycleTheme;
  paintTheme();

  /* ===== Từ đã lưu (đồng bộ mọi thiết bị, cũng bay trên màn hình sao băng) ===== */
  function paintSavedCount() { const n = SYNC.words().length; document.querySelectorAll('.savedCount').forEach(e => e.textContent = n ? String(n) : ''); }
  function toggleSave(p) {
    if (SYNC.isSaved(p.phrase)) { SYNC.removeWord(p.phrase); toast(`Đã bỏ lưu “${p.phrase}”`); return; }
    SYNC.saveWord({ w: p.phrase, ipa: p.ipa || '', pos: 'từ video', vi: p.meaning_vi || '', ex: p.example_en || p.quote || '',
      col: p.quote ? [p.quote.length > 60 ? p.quote.slice(0, 57) + '…' : p.quote] : [], src: { id: S.v && S.v.id, t: p.t, title: S.v && S.v.title } });
    toast(`★ Đã lưu “${p.phrase}”`);
  }
  function speak(t) { if (!LC.speak(t)) toast('Thiết bị không hỗ trợ đọc'); }

  /* ===== Trang chủ ===== */
  function cardHTML(v) {
    const f = SYNC.flag(v.id), n = v.n || (S.index[v.id] && S.index[v.id].n);
    return `<button class="card${f === 'watched' ? ' watched' : ''}" data-id="${esc(v.id)}">
      <div class="thumb"><img loading="lazy" src="${thumb(v.id)}" alt="">
        ${v.dur ? `<span class="dur">${esc(v.dur)}</span>` : ''}${n ? `<span class="badge">Aa ${n}</span>` : ''}
        ${v.isNew && !f ? '<span class="newb">MỚI</span>' : ''}${f === 'watched' ? '<span class="wl">✓ Đã xem</span>' : ''}</div>
      <div class="t">${esc(v.title)}</div><div class="c">${esc(v.channel)}</div></button>`;
  }
  function renderHome() {
    const keep = {};
    document.querySelectorAll('#home .track[data-row]').forEach(t => keep[t.dataset.row] = t.scrollLeft);
    const b = LC.buildRows(S.catalog || { videos: [] });
    S.rows = b.rows; S.history = b.history;
    const hist = S.history.length ? `<section class="hist">
        <button class="hist-head" id="histToggle"><span>🗂 Lịch sử học</span><small>${S.history.length} video</small><em>${S.histOpen ? 'Thu gọn ▴' : 'Xem ▾'}</em></button>
        ${S.histOpen ? `<div class="track" data-row="history">${S.history.map(cardHTML).join('')}</div>` : ''}</section>` : '';
    $('#home').innerHTML = S.rows.map(r => `<section class="row"><h2>${esc(r.title)}<small>${r.items.length} video</small></h2>
        <div class="track" data-row="${esc(r.id)}">${r.items.map(cardHTML).join('')}</div></section>`).join('')
      + hist
      + `<p class="note">Huy hiệu <b>Aa</b>: video đã có cụm từ và phụ đề. Video <b>đã xem</b> xếp cuối mỗi hàng; video lưu vào <b>Lịch sử học</b> nằm ở cuối trang. Danh sách có thêm video mới mỗi ngày.</p>`;
    document.querySelectorAll('#home .track[data-row]').forEach(t => { if (keep[t.dataset.row]) t.scrollLeft = keep[t.dataset.row]; });
  }
  $('#home').addEventListener('click', e => {
    if (e.target.closest('#histToggle')) { S.histOpen = !S.histOpen; renderHome(); return; }
    const c = e.target.closest('.card'); if (c) openVideo(c.dataset.id);
  });

  async function load() {
    const [cat, idx] = await Promise.all([getJSON('video-data/catalog.json'), getJSON('video-data/phrases/index.json')]);
    S.catalog = cat || { videos: [] }; S.index = idx || {};
    renderHome(); paintSavedCount();
    const q = new URLSearchParams(location.search);
    if (q.get('v')) openVideo(q.get('v'), parseFloat(q.get('t')) || 0);
  }

  /* ===== Chuyển màn ===== */
  function setView(view) {
    S.view = view;
    $('#home').classList.toggle('hide', view !== 'videos');
    $('#watch').classList.toggle('hide', view !== 'watch');
    $('#savedView').classList.toggle('hide', view !== 'saved');
    document.body.classList.toggle('watching', view === 'watch');
    document.body.dataset.view = view;
    document.querySelectorAll('#tabs [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === view));
    if (view !== 'watch') { $('#title').textContent = PHONE ? (view === 'saved' ? 'Từ đã lưu' : 'Học qua video') : 'Học qua video'; try { player && player.pauseVideo && player.pauseVideo(); } catch (e) {} }
    if (view === 'saved') renderSaved();
    if (view === 'videos') renderHome();
    window.scrollTo(0, 0);
  }
  $('#tabs').addEventListener('click', e => { const b = e.target.closest('[data-tab]'); if (b) setView(b.dataset.tab); });
  $('#backBtn').onclick = requestBack;
  function goHome() { history.replaceState(null, '', location.pathname); closeSheet(); setView('videos'); }

  /* ===== Trình phát ===== */
  let player = null, ytReady = false, pending = null;
  window.onYouTubeIframeAPIReady = () => { ytReady = true; if (pending) { const p = pending; pending = null; makePlayer(p.id, p.t); } };
  (() => { const s = document.createElement('script'); s.src = 'https://www.youtube.com/iframe_api'; document.head.appendChild(s); })();

  function makePlayer(id, t) {
    if (player && player.loadVideoById) { player.loadVideoById({ videoId: id, startSeconds: t || 0 }); return; }
    player = new YT.Player('yt', {
      videoId: id,
      playerVars: { playsinline: 1, rel: 0, iv_load_policy: 3, cc_load_policy: 0, controls: 0, disablekb: 1, fs: 0, autoplay: 1, start: Math.floor(t || 0), hl: 'vi' },
      events: {
        onReady: e => { try { e.target.setPlaybackRate(S.speed); e.target.playVideo(); } catch (x) {} },
        onStateChange: e => {
          if (e.data === 1) { $('#vbox').classList.add('started'); ytCaptionsOff(); }
          if (e.data === 0) onEnded();
          paintPlay();
        },
        onApiChange: () => { try { player.unloadModule('captions'); player.unloadModule('cc'); } catch (x) {} },   // chỉ dùng phụ đề của trang
      },
    });
  }
  const playing = () => { try { return player.getPlayerState() === YT.PlayerState.PLAYING; } catch (e) { return false; } };
  const now = () => { try { return player.getCurrentTime() || 0; } catch (e) { return 0; } };
  const duration = () => { try { return player.getDuration() || 0; } catch (e) { return 0; } };
  function ytCaptionsOff() { try { player.setOption('captions', 'track', {}); player.unloadModule('captions'); player.unloadModule('cc'); } catch (e) {} }
  function togglePlay() { if (!player || !player.getPlayerState) return; playing() ? player.pauseVideo() : player.playVideo(); setTimeout(paintPlay, 150); }
  $('#tapLayer').onclick = togglePlay;
  function paintPlay() { const b = $('#bPlay'); const t = playing() ? '❚❚' + (PHONE ? '' : ' Dừng') : '▶' + (PHONE ? '' : ' Phát'); if (b.textContent !== t) b.textContent = t; }

  async function openVideo(id, t = 0) {
    const v = allVideos().find(x => x.id === id) || { id, title: '', channel: '' };
    $('#vbox').classList.remove('started');
    S.v = v; S.phr = null; S.cap = null; S.sel = 0; S.now = -1; S.capIdx = -1;
    $('#title').textContent = v.title || 'Video';
    $('#vTitle').textContent = v.title; $('#vSub').textContent = v.channel;
    $('#cap').textContent = '';
    history.replaceState(null, '', `?v=${encodeURIComponent(id)}`);
    setView('watch');
    paintMarks(); renderSide();
    if (ytReady) makePlayer(id, t); else pending = { id, t };
    const [phr, cap] = await Promise.all([getJSON(`video-data/phrases/${id}.json`), getJSON(`video-data/captions/${id}.json`)]);
    if (S.v !== v) return;
    S.phr = phr && phr.phrases && phr.phrases.length ? phr : null;
    S.cap = cap && cap.s ? cap.s : null;
    if (S.phr && t) { const i = S.phr.phrases.findIndex(p => Math.abs(p.t - t) < 1); if (i >= 0) S.sel = i; }
    renderSide();
  }

  /* ===== Đánh dấu: đã xem / Lịch sử học ===== */
  const inCatalog = v => !!(v && v.row);
  function setFlag(s, note = true) {
    const v = S.v; if (!v) return;
    SYNC.setFlag(v.id, s);
    paintMarks();
    if (note) toast(s === 'watched' ? '✓ Đã xem: video xuống cuối hàng' : s === 'archived' ? '🗂 Đã cất vào Lịch sử học' : 'Đã bỏ đánh dấu');
  }
  function paintMarks() {
    const f = S.v ? SYNC.flag(S.v.id) : '', show = inCatalog(S.v);
    const d = $('#bDone'), a = $('#bArch');
    d.classList.toggle('hide', !show); a.classList.toggle('hide', !show);
    d.classList.toggle('on', f === 'watched'); a.classList.toggle('on', f === 'archived');
    d.textContent = PHONE ? '✓' : f === 'watched' ? '✓ Đã xem' : '✓ Xem xong';
    a.textContent = PHONE ? '🗂' : f === 'archived' ? '🗂 Đã cất' : '🗂 Lưu trữ';
  }
  $('#bDone').onclick = () => setFlag(SYNC.flag(S.v.id) === 'watched' ? '' : 'watched');
  $('#bArch').onclick = () => setFlag(SYNC.flag(S.v.id) === 'archived' ? '' : 'archived');

  function nextVideo() {
    const row = S.rows.find(r => r.id === (S.v && S.v.row)); if (!row) return null;
    const i = row.items.findIndex(x => x.id === S.v.id);
    return row.items.slice(i + 1).find(x => !SYNC.flag(x.id)) || null;
  }
  /* Quay lại: đã xem khá nhiều mà chưa đánh dấu thì hỏi */
  function requestBack() {
    const v = S.v, t = now(), d = duration();
    if (S.view === 'watch' && inCatalog(v) && !SYNC.flag(v.id) && (t >= 180 || (d && t / d >= 0.5))) {
      try { player.pauseVideo(); } catch (e) {}
      openSheet({
        title: 'Bạn xem xong video này chưa?', sub: v.title,
        actions: [
          { label: '✓ Đã xem', cls: 'primary', fn: () => { setFlag('watched'); goHome(); } },
          { label: '🗂 Lưu vào Lịch sử học', fn: () => { setFlag('archived'); goHome(); } },
          { label: 'Chưa, để sau', fn: goHome },
        ],
        onDismiss: () => {},
      });
      return;
    }
    goHome();
  }
  function onEnded() {
    const v = S.v; if (!inCatalog(v) || SYNC.flag(v.id)) return;
    const next = nextVideo();
    openSheet({
      title: 'Xem xong rồi! Đánh dấu video này?', sub: v.title,
      actions: [
        ...(next ? [{ label: `✓ Đã xem, xem tiếp: ${next.title}`, cls: 'primary', fn: () => { setFlag('watched', false); openVideo(next.id); } }] : []),
        { label: '✓ Đã xem', cls: next ? '' : 'primary', fn: () => setFlag('watched') },
        { label: '🗂 Lưu vào Lịch sử học', fn: () => setFlag('archived') },
        { label: 'Để sau', fn: () => {} },
      ],
    });
  }

  /* ===== Bảng trượt từ dưới lên (hỏi đánh dấu, đồng bộ) ===== */
  function openSheet({ title, sub, body = '', actions = [], onDismiss }) {
    S.sheet = { actions, onDismiss };
    $('#sheet').innerHTML = `<div class="sheet-bg" data-close></div><div class="sheet-card" role="dialog">
      <div class="grab"></div><h3>${esc(title)}</h3>${sub ? `<p class="sub">${esc(sub)}</p>` : ''}${body}
      <div class="sheet-acts">${actions.map((a, i) => `<button class="${a.cls || ''}" data-a="${i}">${esc(a.label)}</button>`).join('')}</div></div>`;
    $('#sheet').classList.add('show');
  }
  function closeSheet() { S.sheet = null; $('#sheet').classList.remove('show'); }
  $('#sheet').addEventListener('click', e => {
    if (!S.sheet) return;
    const a = e.target.closest('[data-a]');
    if (a) { const f = S.sheet.actions[+a.dataset.a].fn; closeSheet(); f && f(); return; }
    if (e.target.closest('[data-close]')) { const f = S.sheet.onDismiss; closeSheet(); f && f(); }
  });

  /* ===== Đồng bộ TV · iPad · iPhone ===== */
  function openSync() {
    const link = location.href.replace(/[?#].*$/, '').replace(/[^/]*$/, '') + 'iphone.html';
    const age = SYNC.lastPull ? Math.round((Date.now() - SYNC.lastPull) / 60000) : null;
    openSheet({
      title: 'Đồng bộ TV · iPad · iPhone',
      sub: 'Mọi thiết bị mở trang học đều tự dùng chung một kho: đã xem, Lịch sử học, từ đã lưu. Không cần quét mã.',
      body: `<div class="sync">
        <div class="qr" id="qr"></div>
        <div class="sync-txt">
          <p><b>Mở trang học trên máy khác:</b> quét mã QR này bằng Camera, hoặc gửi link bên dưới.</p>
          <p class="small">Kho: <code>${esc(SYNC.code)}</code>${SYNC.isDefault ? ' (kho chung)' : ' · <a href="#" id="useDefault">dùng lại kho chung</a>'} · ${SYNC.words().length} từ đã lưu</p>
          <p class="small">${SYNC.online ? '● Đã kết nối' : '○ Chưa kết nối'}${age !== null ? ` · cập nhật ${age < 1 ? 'vừa xong' : age + ' phút trước'}` : ''}</p>
        </div></div>`,
      actions: [{ label: navigator.share ? 'Gửi link trang học' : 'Sao chép link', fn: () => shareLink(link) }, { label: 'Đóng', fn: () => {} }],
    });
    LC.qr($('#qr'), link, 180);
    const ud = $('#useDefault');
    if (ud) ud.onclick = async e => { e.preventDefault(); await SYNC.useDefault(); closeSheet(); toast('Đã chuyển về kho chung'); };
  }
  async function shareLink(link) {
    try { if (navigator.share) { await navigator.share({ title: 'Học qua video: đồng bộ', url: link }); return; } } catch (e) { return; }
    try { await navigator.clipboard.writeText(link); toast('Đã sao chép link đồng bộ'); } catch (e) { toast(link, 6000); }
  }
  $('#syncBtn').onclick = openSync;
  function paintSync() { $('#syncBtn').classList.toggle('off', !SYNC.online); }

  /* ===== Cụm từ ===== */
  function detailHTML(p, d) {
    const saved = SYNC.isSaved(p.phrase);
    return `<div class="detail">
      <div class="d-top"><span>Cụm từ ${S.sel + 1}/${d.phrases.length} · ${fmt(p.t)}</span><span class="live">${S.sel === S.now && playing() ? '● Đang nói câu này' : ''}</span></div>
      <div class="d-row"><div class="d-p">${esc(p.phrase)}</div>${p.ipa ? `<div class="d-ipa">${esc(p.ipa)}</div>` : ''}</div>
      <div class="d-m">${esc(p.meaning_vi)}</div>
      <div class="quote">“${markQuote(p.quote, p.phrase)}”</div>
      ${p.usage_vi ? `<div class="d-u">${esc(p.usage_vi)}</div>` : ''}
      ${p.example_en ? `<div class="d-ex">${esc(p.example_en)}<small>${esc(p.example_vi)}</small></div>` : ''}
      <div class="d-act">
        <button class="primary" data-act="play">▶ Nghe câu</button>
        <button data-act="speak">🔊 Đọc từ</button>
        <button data-act="save" class="${saved ? 'saved' : ''}">${saved ? '★ Đã lưu' : '☆ Lưu từ'}</button>
      </div></div>`;
  }
  function renderSide() { renderDetail(); renderList(); }
  function renderDetail() {
    const el = $('#detail'), d = S.phr;
    el.innerHTML = !d || PHONE ? '' : detailHTML(d.phrases[S.sel], d);
  }
  function sideTools() {
    return PHONE ? '' : `<div class="side-tools"><button class="icon-btn back" data-tool="back" aria-label="Quay lại">←</button>
      <span class="st-title">${esc(S.v ? S.v.title : '')}</span>
      <button class="icon-btn" data-tool="sync" aria-label="Đồng bộ">⇅</button>
      <button class="icon-btn" data-tool="theme"></button></div>`;
  }
  function renderList() {
    const el = $('#side'), d = S.phr;
    if (!d) {
      el.innerHTML = sideTools() + `<div class="empty">${S.v && (S.v.n || S.index[S.v.id]) ? 'Đang tải cụm từ…' : 'Video này chưa có cụm từ. Bạn vẫn xem bình thường; cụm từ sẽ có sau lần chạy hằng ngày.'}</div>`;
      paintTheme(); return;
    }
    const keep = el.scrollTop;
    el.innerHTML = `${sideTools()}
      <div class="lhead"><h3>Cụm từ hay</h3>${d.level ? `<span class="lvl">${esc(d.level)}</span>` : ''}<span>${d.phrases.length} cụm · chạm để nghe</span></div>
      ${d.summary_vi ? `<p class="sum">${esc(d.summary_vi)}</p>` : ''}
      <div id="plist">${d.phrases.map((x, i) => `
        <div class="ph-wrap${i === S.sel ? ' open' : ''}"><button class="ph${i === S.sel ? ' sel' : ''}${i === S.now ? ' now' : ''}" data-i="${i}">
          <span class="st">${SYNC.isSaved(x.phrase) ? '★' : ''}</span>
          <span class="tx"><div class="p">${esc(x.phrase)}</div><div class="m">${esc(x.meaning_vi)}</div></span>
          <span class="tm">${fmt(x.t)}</span>
        </button>${PHONE && i === S.sel ? detailHTML(x, d) : ''}</div>`).join('')}</div>`;
    el.scrollTop = keep;
    paintTheme();
  }
  /* Danh sách tự theo câu đang nói (trừ khi vừa chạm tay) */
  function followSel() {
    if (Date.now() < S.touchHold) return;
    const el = $('#side'), row = el.querySelector('.ph.sel'); if (!row) return;
    if (el.scrollHeight > el.clientHeight + 4 && getComputedStyle(el).overflowY !== 'visible') {
      el.scrollTo({ top: Math.max(0, row.offsetTop - el.clientHeight * 0.3), behavior: 'smooth' });
    } else {
      const top = $('.stage').getBoundingClientRect().bottom + 8, r = row.getBoundingClientRect();
      if (r.top < top || r.bottom > innerHeight - 80) window.scrollBy({ top: r.top - top - 12, behavior: 'smooth' });
    }
  }
  document.addEventListener('click', e => {
    const a = e.target.closest('#detail [data-act], #side [data-act]');
    if (!a || !S.phr) return;
    const p = S.phr.phrases[S.sel];
    if (a.dataset.act === 'play') playPhrase(S.sel);
    else if (a.dataset.act === 'speak') speak(p.phrase);
    else if (a.dataset.act === 'save') { toggleSave(p); renderSide(); }
  });
  $('#side').addEventListener('click', e => {
    const tool = e.target.closest('[data-tool]');
    if (tool) { const k = tool.dataset.tool; if (k === 'back') requestBack(); else if (k === 'sync') openSync(); else cycleTheme(); return; }
    const row = e.target.closest('.ph'); if (!row || !S.phr) return;
    S.sel = +row.dataset.i; S.touchHold = Date.now() + 10000; playPhrase(S.sel); renderSide();
    if (PHONE) { S.touchHold = 0; followSel(); S.touchHold = Date.now() + 10000; }
    else if (matchMedia('(orientation:portrait)').matches) { const dt = $('.detail'); if (dt) window.scrollTo({ top: Math.max(0, dt.getBoundingClientRect().top + scrollY - $('.stage').offsetHeight - 80), behavior: 'smooth' }); }
  });
  $('#side').addEventListener('touchstart', () => { S.touchHold = Date.now() + 8000; }, { passive: true });
  window.addEventListener('touchmove', () => { if (S.view === 'watch') S.touchHold = Date.now() + 8000; }, { passive: true });
  function playPhrase(i) {
    if (!player || !player.seekTo || !S.phr) return;
    player.seekTo(Math.max(0, S.phr.phrases[i].t - 0.3), true); player.playVideo();
    S.now = i;
  }

  /* ===== Nút điều khiển ===== */
  S.speed = parseFloat(store.get('ipad.speed')) || 1;
  const SPEEDS = [0.75, 1, 1.25];
  const paintSpeed = () => { $('#bSpeed').textContent = `${String(S.speed).replace('.', ',')}×`; };
  paintSpeed();
  $('#bPlay').onclick = togglePlay;
  $('#bBack').onclick = () => { if (player && player.seekTo) player.seekTo(Math.max(0, now() - 5), true); };
  $('#bFwd').onclick = () => { if (player && player.seekTo) player.seekTo(now() + 10, true); };
  $('#bSpeed').onclick = () => {
    S.speed = SPEEDS[(SPEEDS.indexOf(S.speed) + 1) % SPEEDS.length]; store.set('ipad.speed', S.speed); paintSpeed();
    try { player.setPlaybackRate(S.speed); } catch (e) {}
  };
  /* Mở đúng đoạn đang xem trong app YouTube: tài khoản Premium ở đó không có quảng cáo */
  $('#bYT').onclick = () => { if (!S.v) return; try { player.pauseVideo(); } catch (e) {} location.href = `https://www.youtube.com/watch?v=${encodeURIComponent(S.v.id)}&t=${Math.floor(now())}s`; };
  let ccOn = store.get('ipad.cc') !== '0';
  function paintCC() { $('#bCC').classList.toggle('on', ccOn); $('#cap').classList.toggle('off', !ccOn); }
  $('#bCC').onclick = () => { ccOn = !ccOn; store.set('ipad.cc', ccOn ? '1' : '0'); paintCC(); };
  paintCC();

  /* ===== Phụ đề tự vẽ + theo dõi cụm từ đang nói ===== */
  setInterval(() => {
    if (S.view !== 'watch' || !player || !player.getCurrentTime) return;
    paintPlay();
    const t = now();
    if (S.cap && ccOn) {
      const k = LC.capAt(S.cap, t);
      if (k !== S.capIdx) {
        S.capIdx = k;
        const seg = k >= 0 ? S.cap[k] : null;
        const ph = seg && S.phr && S.phr.phrases.find(p => Math.abs(p.t - seg[0]) < 0.05);
        const c = $('#cap');
        c.innerHTML = !seg ? '' : ph ? markQuote(seg[2], ph.phrase) : esc(seg[2]);
        c.style.fontSize = '';
        for (let f = parseFloat(getComputedStyle(c).fontSize); c.scrollWidth > c.clientWidth + 1 && f > 11; f -= 1) c.style.fontSize = f - 1 + 'px';
      }
    }
    if (S.phr && playing()) {
      let idx = -1; S.phr.phrases.forEach((p, i) => { if (p.t <= t + 0.5) idx = i; });
      if (idx !== S.now) {
        S.now = idx;
        if (idx >= 0 && Date.now() > S.touchHold) S.sel = idx;
        renderSide(); followSel();
      }
    }
  }, 250);

  /* ===== Màn từ đã lưu ===== */
  function renderSaved() {
    const l = SYNC.words();
    $('#savedView').innerHTML = !l.length
      ? `<div class="empty">Chưa có từ nào. Khi xem video, chạm <b>☆ Lưu từ</b> để thêm vào đây.<br><br>Từ đã lưu đồng bộ giữa TV, iPad, iPhone và bay trên màn hình sao băng.</div>`
      : `<div class="lhead"><h3>Từ đã lưu</h3><span>${l.length} từ · mới nhất ở trên</span></div>` + l.map((o, i) => `
        <div class="sw">
          <div class="h"><span class="w">${esc(o.w)}</span>${o.ipa ? `<span class="i">${esc(o.ipa)}</span>` : ''}</div>
          <div class="v">${esc(o.vi)}</div>
          ${o.ex ? `<div class="e">${esc(o.ex)}</div>` : ''}
          ${o.src && o.src.title ? `<div class="src">Từ video: ${esc(o.src.title)} · ${fmt(o.src.t)}${o.added ? ` · lưu ${esc(o.added)}` : ''}</div>` : ''}
          <div class="a">
            ${o.src && o.src.id ? `<button data-go="${i}">▶ Xem lại câu này</button>` : ''}
            <button data-say="${i}">🔊 Đọc</button>
            <button data-del="${i}">Bỏ lưu</button>
          </div>
        </div>`).join('');
  }
  $('#savedView').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const l = SYNC.words();
    if (b.dataset.go) { const o = l[+b.dataset.go]; openVideo(o.src.id, o.src.t); }
    else if (b.dataset.say) speak(l[+b.dataset.say].w);
    else if (b.dataset.del) { const o = l[+b.dataset.del]; SYNC.removeWord(o.w); toast(`Đã bỏ lưu “${o.w}”`); }
  });

  /* ===== Đồng bộ: cập nhật giao diện khi máy khác đổi ===== */
  SYNC.on(what => {
    paintSync();
    if (what !== 'data') return;
    paintSavedCount();
    if (S.view === 'videos') renderHome();
    else if (S.view === 'saved') renderSaved();
    else if (S.view === 'watch') { paintMarks(); renderList(); }
  });
  SYNC.consumeParam().then(ok => { if (ok) toast('Đã nối đồng bộ với TV và các máy khác'); });
  SYNC.start();
  paintSync();
  load();
})();
