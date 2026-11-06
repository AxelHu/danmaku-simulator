/* Danmaku Simulator preview. All remote and imported text is rendered as text. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const video = $('video');
  const canvas = $('danmakuCanvas');
  const ctx = canvas.getContext('2d');
  const FONT = '"Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif';
  const ROW_HEIGHT = 60;
  const LIFETIME = 7;
  const PREF_KEY = 'danmaku-simulator:preview:v1';
  const defaults = { enabled: true, density: 100, opacity: 85, fontSize: 24, area: 0.5 };
  const state = {
    settings: { ...defaults }, runs: [], runId: null, data: null,
    comments: [], filtered: [], layout: [], activeIndex: -1,
    follow: true, mediaReady: false, mediaUrl: null, mediaBlob: null,
    mediaTitle: '', mediaDuration: 0, width: 0, height: 0, dpr: 1,
    raf: 0, fetchController: null, fetchSerial: 0, localComments: false,
    renderStart: -1, renderEnd: -1, renderActive: -2,
  };

  function number(value) {
    if (value === undefined || value === null || value === '' || typeof value === 'boolean') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function clamp(value, lo, hi) { return Math.max(lo, Math.min(hi, value)); }
  function count(value) { return value === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value); }
  function timecode(value) {
    const seconds = Math.max(0, Math.floor(number(value) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const tail = String(seconds % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${tail}` : `${String(minutes).padStart(2, '0')}:${tail}`;
  }
  function money(value, currency = 'USD') {
    const n = number(value);
    if (n === null || n < 0) return '—';
    try {
      const formatter = new Intl.NumberFormat('en-US', { style: 'currency', currency, minimumFractionDigits: 4, maximumFractionDigits: 6 });
      return n > 0 && n < 0.000001 ? `<${formatter.format(0.000001)}` : formatter.format(n);
    } catch (_) { return `${n.toFixed(6)} ${currency}`; }
  }
  function textElement(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  function notify(message, kind = 'error') {
    $('notice').textContent = message || '';
    $('notice').hidden = !message;
    $('notice').classList.toggle('info', kind === 'info');
  }
  function setPill(text, kind = 'neutral') {
    $('runStatus').textContent = text;
    $('runStatus').className = `pill ${kind}`;
  }
  function hash(value) {
    let h = 2166136261;
    for (const c of String(value)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function upperBound(list, time) {
    let lo = 0, hi = list.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (list[mid].time <= time) lo = mid + 1; else hi = mid;
    }
    return lo;
  }
  function personaName(comment) {
    const base = comment.persona.split(':echo:')[0];
    const defaults = { casual: '随性观众', detail: '细节观察', joker: '玩梗观众', empathy: '共情观众' };
    const name = comment.persona_label || defaults[base] || base;
    if (comment.generation_method === 'interaction') return `${name} · 接话`;
    return comment.persona.includes(':echo:') ? `${name} · 复读` : name;
  }

  function loadPreferences() {
    try {
      const p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
      state.settings = {
        enabled: typeof p.enabled === 'boolean' ? p.enabled : true,
        density: clamp(number(p.density) ?? defaults.density, 10, 100),
        opacity: clamp(number(p.opacity) ?? defaults.opacity, 20, 100),
        fontSize: clamp(number(p.fontSize) ?? defaults.fontSize, 16, 40),
        area: [0.3, 0.5, 0.75, 1].includes(p.area) ? p.area : defaults.area,
      };
    } catch (_) { /* Private browsing may disable persistence. */ }
    $('density').value = state.settings.density;
    $('opacity').value = state.settings.opacity;
    $('fontSize').value = state.settings.fontSize;
    $('displayArea').value = state.settings.area;
    updateSettingsLabels();
  }
  function savePreferences() {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(state.settings)); } catch (_) { /* Optional. */ }
  }
  function fillRange(el) {
    const min = Number(el.min) || 0, max = Number(el.max) || 1;
    const fill = max > min ? clamp((Number(el.value) - min) / (max - min) * 100, 0, 100) : 0;
    el.style.setProperty('--fill', `${fill}%`);
  }
  function updateSettingsLabels() {
    $('densityValue').textContent = `${state.settings.density}%`;
    $('opacityValue').textContent = `${state.settings.opacity}%`;
    $('fontSizeValue').textContent = String(state.settings.fontSize);
    $('danmakuToggle').setAttribute('aria-pressed', String(state.settings.enabled));
    $('danmakuToggle').querySelector('span').textContent = state.settings.enabled ? '弹幕开启' : '弹幕关闭';
    ['density', 'opacity', 'fontSize', 'volume', 'seek'].forEach((id) => fillRange($(id)));
  }

  function normalizeComments(payload) {
    const source = Array.isArray(payload) ? payload : payload && payload.comments;
    if (!Array.isArray(source)) throw new Error('弹幕 JSON 需要是一个数组，或包含 comments 数组。');
    const isTrack = payload?.format === 'danmaku-track/v1';
    const scale = isTrack ? number(payload.timeline?.scale ?? 1) : 1;
    const offset = isTrack ? number(payload.timeline?.offset ?? 0) : 0;
    const duration = isTrack ? number(payload.timeline?.target_duration) ?? ((number(payload.video?.duration) || 0) * scale + offset) : null;
    if (isTrack && payload.timeline?.target_duration != null && number(payload.timeline.target_duration) === null) throw new Error('弹幕轨目标时长无效。');
    if (scale === null || scale <= 0 || offset === null || (isTrack && !(duration > 0))) throw new Error('弹幕轨的时间比例、偏移或目标时长无效。');
    let rejected = 0;
    const comments = [];
    source.forEach((raw, index) => {
      if (!raw || typeof raw !== 'object') { rejected++; return; }
      const sourceTime = number(raw.time ?? raw.timestamp ?? raw.at);
      const at = sourceTime === null ? null : sourceTime * scale + offset;
      if (sourceTime === null || sourceTime < 0 || at < 0 || (duration !== null && at >= duration) || typeof raw.text !== 'string' || !raw.text.trim()) { rejected++; return; }
      let color = number(raw.color);
      if (typeof raw.color === 'string' && /^#[\da-f]{6}$/i.test(raw.color)) color = parseInt(raw.color.slice(1), 16);
      const mode = Number(raw.mode);
      const c = {
        id: String(raw.id ?? `comment-${index}`), time: at,
        text: raw.text.replace(/[\u0000-\u001f\u007f]/g, ' ').trim(),
        mode: [1, 4, 5].includes(mode) ? mode : 1,
        color: color === null ? 16777215 : clamp(Math.round(color), 0, 16777215),
        size: clamp(number(raw.size) ?? 25, 8, 72),
        persona: String(raw.persona ?? '观众'),
        provider: String(raw.provider ?? ''), model: String(raw.model ?? ''),
        event_id: String(raw.event_id ?? ''), kind: String(raw.kind ?? ''),
        generation_method: String(raw.generation_method ?? ''),
        reply_to: String(raw.reply_to ?? ''), parent_text: String(raw.parent_text ?? ''),
        knowledge_cutoff: number(raw.knowledge_cutoff), synthetic: raw.synthetic === true,
        _index: index,
      };
      if (raw.persona_label !== undefined) c.persona_label = String(raw.persona_label);
      if (raw.parent_id !== undefined) c.parent_id = String(raw.parent_id);
      comments.push(c);
    });
    comments.sort((a, b) => a.time - b.time || a._index - b._index);
    if (source.length && !comments.length) throw new Error('没有读到有效弹幕。每条记录至少需要非负的 time（秒）与 text。');
    return { comments, rejected };
  }

  // Assign from the beginning, so a seek produces the same positions as normal playback.
  // Both the initial gap and the possible later catch-up are checked for each lane.
  function makeLayout(comments, width, height, options) {
    if (!(width > 0 && height > 0)) return { items: [], overflow: 0, densityDropped: 0 };
    const base = options.fontSize;
    const line = Math.ceil(base * 1.4 + 7);
    const padding = Math.max(8, base * 0.4);
    const lanes = Math.max(1, Math.floor((height * options.area - padding * 2) / line));
    const occupied = Array(lanes).fill(null);
    const items = [];
    let overflow = 0, densityDropped = 0;
    for (const c of comments) {
      if (options.density < 100 && hash(`${c.id}|${c.time}|${c._index}`) % 100 >= options.density) { densityDropped++; continue; }
      const font = clamp(base * c.size / 25, base * 0.65, base * 1.4);
      ctx.font = `600 ${font}px ${FONT}`;
      const textWidth = ctx.measureText(c.text).width;
      const duration = c.mode === 1 ? LIFETIME : 4.5;
      const speed = (width + textWidth) / duration;
      const gap = Math.max(18, base * 1.2);
      let chosen = -1;
      for (let n = 0; n < lanes; n++) {
        const lane = c.mode === 4 ? lanes - 1 - n : n;
        const p = occupied[lane];
        let safe = !p || c.time >= p.end;
        if (!safe && c.mode === 1 && p.mode === 1) {
          const delta = c.time - p.time;
          const enteringGap = delta * p.speed - p.textWidth;
          const exitGap = width - speed * (p.end - c.time);
          safe = enteringGap >= gap && (speed <= p.speed || exitGap >= gap);
        }
        if (safe) { chosen = lane; break; }
      }
      if (chosen < 0) { overflow++; continue; }
      const item = { ...c, font, textWidth, speed, end: c.time + duration, lane: chosen,
        y: padding + line * (chosen + 0.5) };
      occupied[chosen] = item;
      items.push(item);
    }
    return { items, overflow, densityDropped };
  }
  function rebuildLayout() {
    const scale = clamp(state.width / 960, 0.67, 1.5);
    const result = makeLayout(state.comments, state.width, state.height, {
      fontSize: state.settings.fontSize * scale, area: state.settings.area, density: state.settings.density,
    });
    state.layout = result.items;
    $('layoutHint').textContent = result.overflow ? `拥挤时自动避让 · ${result.items.length} 条可见` : '拥挤时自动避让';
    $('layoutHint').title = '密度与避让只改变当前预览；XML 与 JSON 保留全部弹幕。';
    drawFrame();
  }
  function resizeCanvas() {
    const stage = $('videoStage');
    let width = stage.clientWidth, height = stage.clientHeight;
    if (video.videoWidth && video.videoHeight) {
      const scale = Math.min(width / video.videoWidth, height / video.videoHeight);
      width = video.videoWidth * scale;
      height = video.videoHeight * scale;
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.style.left = `${(stage.clientWidth - width) / 2}px`;
    canvas.style.top = `${(stage.clientHeight - height) / 2}px`;
    if (Math.abs(state.width - width) > 0.1 || Math.abs(state.height - height) > 0.1 || state.dpr !== dpr) {
      state.width = width; state.height = height; state.dpr = dpr;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      rebuildLayout();
    } else drawFrame();
  }
  function drawFrame() {
    ctx.clearRect(0, 0, state.width, state.height);
    if (!state.settings.enabled || !state.mediaReady) return;
    const now = video.currentTime || 0;
    const start = upperBound(state.layout, now - LIFETIME);
    ctx.save();
    ctx.globalAlpha = state.settings.opacity / 100;
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(0,0,0,.85)';
    ctx.shadowColor = 'rgba(0,0,0,.4)';
    ctx.shadowBlur = 2;
    for (let i = start; i < state.layout.length; i++) {
      const c = state.layout[i];
      if (c.time > now) break;
      if (c.end <= now) continue;
      ctx.font = `600 ${c.font}px ${FONT}`;
      ctx.lineWidth = Math.max(2.2, c.font / 10);
      ctx.fillStyle = `#${c.color.toString(16).padStart(6, '0')}`;
      ctx.textAlign = c.mode === 1 ? 'left' : 'center';
      const x = c.mode === 1 ? state.width - c.speed * (now - c.time) : state.width / 2;
      ctx.strokeText(c.text, x, c.y);
      ctx.fillText(c.text, x, c.y);
    }
    ctx.restore();
  }
  function animate() {
    state.raf = 0;
    drawFrame();
    updateTransport();
    if (!video.paused && !video.ended) state.raf = requestAnimationFrame(animate);
  }
  function startAnimation() { if (!state.raf) state.raf = requestAnimationFrame(animate); }
  function stopAnimation() { if (state.raf) cancelAnimationFrame(state.raf); state.raf = 0; drawFrame(); }

  function setFollow(enabled) {
    state.follow = enabled;
    $('follow').setAttribute('aria-pressed', String(enabled));
    if (enabled) updateCommentFocus(true);
  }
  function applyCommentFilter() {
    const query = $('commentSearch').value.trim().toLocaleLowerCase();
    state.filtered = query ? state.comments.filter((c) => `${c.text} ${c.persona} ${personaName(c)} ${c.model}`.toLocaleLowerCase().includes(query)) : state.comments;
    $('commentCount').textContent = query ? `${state.filtered.length} / ${state.comments.length}` : String(state.comments.length);
    $('commentsEmpty').hidden = state.filtered.length > 0;
    $('commentsEmpty').querySelector('p').textContent = query ? '没有找到这句弹幕' : '座位已经准备好了';
    $('commentsEmpty').querySelector('span').textContent = query ? '换个词试试，或清空搜索看全部。' : '载入弹幕后，每一条反应都会出现在这里。';
    state.activeIndex = upperBound(state.filtered, video.currentTime || 0) - 1;
    state.renderStart = -1;
    $('commentsScroll').scrollTop = 0;
    renderRows(true);
  }
  function renderRows(force = false) {
    const scroll = $('commentsScroll');
    const start = Math.max(0, Math.floor(scroll.scrollTop / ROW_HEIGHT) - 4);
    const end = Math.min(state.filtered.length, start + Math.ceil((scroll.clientHeight || 360) / ROW_HEIGHT) + 9);
    if (!force && start === state.renderStart && end === state.renderEnd && state.renderActive === state.activeIndex) return;
    state.renderStart = start; state.renderEnd = end; state.renderActive = state.activeIndex;
    const fragment = document.createDocumentFragment();
    const before = textElement('div', 'comments-list-spacer');
    before.style.height = `${start * ROW_HEIGHT}px`;
    fragment.append(before);
    for (let i = start; i < end; i++) {
      const c = state.filtered[i];
      const row = textElement('button', `comment-row${i === state.activeIndex ? ' current' : ''}`);
      row.type = 'button'; row.title = `${timecode(c.time)}  ${c.text}`;
      if (c.reply_to && c.parent_text) row.title += `\n接话：${c.parent_text}`;
      row.setAttribute('aria-label', `跳转到 ${timecode(c.time)}：${c.text}`);
      const stamp = textElement('span', 'comment-time', timecode(c.time));
      const copy = textElement('span', 'comment-copy');
      const body = textElement('span', 'comment-text', c.text);
      const origin = textElement('span', 'comment-origin', [personaName(c), c.model || c.provider].filter(Boolean).join(' · '));
      copy.append(body, origin); row.append(stamp, copy);
      row.addEventListener('click', () => { seekTo(Math.max(0, c.time - 0.15)); });
      fragment.append(row);
    }
    const after = textElement('div', 'comments-list-spacer');
    after.style.height = `${Math.max(0, state.filtered.length - end) * ROW_HEIGHT}px`;
    fragment.append(after);
    $('commentsList').replaceChildren(fragment);
  }
  function updateCommentFocus(force = false) {
    const index = upperBound(state.filtered, video.currentTime || 0) - 1;
    if (!force && index === state.activeIndex) return;
    state.activeIndex = index;
    if (state.follow && index >= 0) {
      const scroll = $('commentsScroll');
      const target = Math.max(0, index * ROW_HEIGHT - scroll.clientHeight * 0.4);
      if (Math.abs(scroll.scrollTop - target) > 1) scroll.scrollTop = target;
    }
    renderRows(force);
  }
  function installComments(payload) {
    const normalized = normalizeComments(payload);
    state.comments = normalized.comments;
    $('commentSearch').value = '';
    applyCommentFilter();
    const personas = new Set(state.comments.map((c) => c.persona.split(':echo:')[0]));
    const models = new Set(state.comments.map((c) => c.model || c.provider).filter(Boolean));
    $('audienceCount').textContent = state.comments.length ? `${personas.size} 类观众${models.size ? ` · ${models.size} 个模型` : ''}` : '等待观众入场';
    ['exportXml', 'exportAss', 'exportJson'].forEach((id) => { $(id).disabled = !state.comments.length; });
    rebuildLayout();
    return normalized.rejected;
  }

  function metric(source, ...keys) {
    for (const key of keys) {
      const n = number(source && source[key]);
      if (n !== null && n >= 0) return n;
    }
    return null;
  }
  function renderUsage(payload, diagnostics) {
    const u = payload && (payload.summary || payload);
    const available = u && typeof u === 'object' && Object.keys(u).length > 0;
    const input = available ? metric(u, 'input_tokens', 'total_input_tokens', 'prompt_tokens') : null;
    const output = available ? metric(u, 'output_tokens', 'total_output_tokens', 'completion_tokens') : null;
    const cached = available ? metric(u, 'cached_input_tokens', 'cache_read_tokens', 'cached_tokens') : null;
    const writes = available ? metric(u, 'cache_write_tokens', 'cache_creation_input_tokens') : null;
    const requests = available ? metric(u, 'request_count', 'api_requests', 'requests') : null;
    const local = available ? metric(u, 'local_cache_hits', 'result_cache_hits') : null;
    const cost = available ? metric(u, 'estimated_cost', 'estimated_cost_usd', 'cost_usd') : null;
    const unpriced = available ? metric(u, 'unpriced_requests') : null;
    const audio = available ? metric(u, 'audio_seconds') : null;
    const currency = typeof u?.currency === 'string' ? u.currency : 'USD';
    $('metricCost').textContent = money(cost, currency);
    $('metricCostNote').textContent = cost === null ? '尚未提供计价数据' : `${currency} · ${diagnostics?.base_comments_reused ? '本次新增互动参考费' : '按配置价格折算'}`;
    $('metricInput').textContent = count(input);
    $('metricInputNote').textContent = cached !== null ? `其中缓存读取 ${count(cached)}` : '含缓存读取';
    $('metricOutput').textContent = count(output);
    $('metricCache').textContent = input !== null && cached !== null ? `${input > 0 ? (Math.min(cached / input, 1) * 100).toFixed(1) : '0'}%` : '—';
    $('metricCacheNote').textContent = local !== null ? `本地结果复用 ${count(local)} 次` : '以供应商返回用量为准';
    const callParts = [];
    if (requests !== null) callParts.push(`${count(requests)} 次模型请求`);
    if (audio !== null && audio > 0) callParts.push(`STT ${timecode(audio)}`);
    if (writes !== null && writes > 0) callParts.push(`缓存写入 ${count(writes)}`);
    $('usageCalls').textContent = callParts.length ? callParts.join(' · ') : '该文件没有附带用量记录';
    let billing = '参考成本由用量与价格配置折算；实际扣费以供应商账单为准。';
    if (u?.billing_mode === 'subscription' || u?.billing_mode === 'mixed') billing = '套餐部分为参考折算，非额外扣费。实际扣费以供应商账单为准。';
    if (unpriced && unpriced > 0) billing += ` ${count(unpriced)} 次调用尚未计价，成本合计不完整。`;
    if (diagnostics?.base_comments_reused) billing = '此处只统计本次新增互动，原弹幕的生成费用未重复计入。' + billing;
    if (diagnostics?.recorded_sample) billing = '这是已完成实验的历史用量，回放没有新模型请求。' + billing;
    $('billingNote').textContent = billing;
    $('billingNote').classList.toggle('incomplete', !!unpriced);
    const rawRows = u?.by_model || payload?.by_model || [];
    const rows = Array.isArray(rawRows) ? rawRows : Object.entries(rawRows).map(([name, value]) => ({ model: name, ...value }));
    $('modelTableWrap').hidden = rows.length === 0;
    const fragment = document.createDocumentFragment();
    rows.forEach((r) => {
      const row = document.createElement('tr');
      const name = document.createElement('td');
      name.append(textElement('span', 'model-name', r.model || r.name || r.provider || '未标注模型'));
      const detail = [r.provider, r.stage].filter(Boolean).join(' · ');
      if (detail) name.append(textElement('span', 'model-detail', detail));
      row.append(name,
        textElement('td', '', count(metric(r, 'input_tokens', 'total_input_tokens', 'prompt_tokens'))),
        textElement('td', '', count(metric(r, 'cached_input_tokens', 'cache_read_tokens', 'cached_tokens'))),
        textElement('td', '', count(metric(r, 'output_tokens', 'total_output_tokens', 'completion_tokens'))),
        textElement('td', '', money(metric(r, 'estimated_cost', 'estimated_cost_usd', 'cost_usd'), r.currency || currency)));
      fragment.append(row);
    });
    $('modelTableBody').replaceChildren(fragment);
  }

  function getDuration() {
    return Number.isFinite(video.duration) && video.duration > 0 ? video.duration : state.mediaDuration;
  }
  function updateTransport() {
    const duration = getDuration();
    const now = video.currentTime || 0;
    $('currentTime').textContent = timecode(now);
    $('durationTime').textContent = timecode(duration);
    $('seek').max = duration > 0 ? String(duration) : '1';
    $('seek').value = String(now);
    $('seek').setAttribute('aria-valuetext', `${timecode(now)} / ${timecode(duration)}`);
    fillRange($('seek'));
    $('playIcon').setAttribute('href', video.paused ? '#i-play' : '#i-pause');
    $('play').setAttribute('aria-label', video.paused ? '播放' : '暂停');
  }
  function updateVolume() {
    $('volumeIcon').setAttribute('href', video.muted || video.volume === 0 ? '#i-muted' : '#i-volume');
    $('mute').setAttribute('aria-label', video.muted ? '取消静音' : '静音');
    $('volume').value = String(video.muted ? 0 : video.volume);
    fillRange($('volume'));
  }
  async function togglePlay() {
    if (!state.mediaReady) return;
    if (!video.paused) video.pause();
    else {
      try { await video.play(); }
      catch (_) { notify('浏览器未能开始播放。请检查视频格式，或再次点击播放。'); }
    }
  }
  function seekTo(value) {
    if (!state.mediaReady) { notify('请先为这组弹幕选择一个可播放的视频。', 'info'); return; }
    video.currentTime = clamp(value, 0, Math.max(0, getDuration()));
    updateTransport(); drawFrame(); updateCommentFocus(true);
  }
  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if ($('playerShell').requestFullscreen) await $('playerShell').requestFullscreen();
      else notify('当前浏览器不支持此播放器的全屏模式。', 'info');
    } catch (_) { notify('未能进入全屏，请在浏览器中重试。', 'info'); }
  }
  function releaseMediaBlob() {
    if (state.mediaBlob) URL.revokeObjectURL(state.mediaBlob);
    state.mediaBlob = null;
  }
  function placeholder(title, body) {
    $('playerPlaceholder').hidden = false;
    $('placeholderTitle').textContent = title;
    $('placeholderText').textContent = body;
  }
  function setMedia(url, title, duration = 0, local = false) {
    video.pause(); stopAnimation();
    state.mediaReady = false;
    if (!local) releaseMediaBlob();
    state.mediaUrl = url || null;
    state.mediaTitle = title || '未命名视频';
    state.mediaDuration = number(duration) || 0;
    ['play', 'rewind', 'seek'].forEach((id) => { $(id).disabled = true; });
    $('videoCornerLabel').hidden = true;
    if (url) {
      $('playerPlaceholder').hidden = true;
      $('mediaBusy').hidden = false;
      video.src = url;
      video.load();
    } else {
      video.removeAttribute('src'); video.load();
      $('mediaBusy').hidden = true;
      placeholder('弹幕已就位，等你选片', '为这组弹幕选择对应的本地视频，就能同步预览。');
    }
    updateTransport(); resizeCanvas();
  }
  function safeServerVideoUrl(value) {
    if (typeof value !== 'string' || !value) return null;
    try {
      const parsed = new URL(value, location.href);
      if (parsed.origin !== location.origin || !['http:', 'https:'].includes(parsed.protocol)) return null;
      return parsed.href;
    } catch (_) { return null; }
  }

  async function readJson(url, signal) {
    const r = await fetch(url, { signal, cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`读取失败（HTTP ${r.status}）`);
    return r.json();
  }
  function populateRuns() {
    const select = $('runSelect');
    const fragment = document.createDocumentFragment();
    const empty = textElement('option', '', state.localComments || state.mediaBlob ? '本地预览' : '选择一个生成结果');
    empty.value = ''; fragment.append(empty);
    state.runs.forEach((run) => {
      const option = textElement('option', '', `${run.title || run.id}${number(run.comment_count) !== null ? ` · ${count(Number(run.comment_count))} 条` : ''}`);
      option.value = String(run.id); fragment.append(option);
    });
    select.replaceChildren(fragment);
    select.value = state.runId || '';
    select.disabled = state.runs.length === 0;
  }
  async function loadRuns(initial = false) {
    $('refreshRuns').disabled = true; $('refreshRuns').classList.add('busy');
    try {
      const result = await readJson('/api/runs');
      if (!Array.isArray(result.runs)) throw new Error('运行列表的格式不正确。');
      state.runs = result.runs.filter((r) => r && typeof r.id === 'string');
      populateRuns();
      if (initial && state.runs.length && !state.mediaBlob && !state.localComments) {
        // A query parameter selects a known run ID, never a media or request URL.
        const requested = new URLSearchParams(location.search).get('run');
        const selected = state.runs.find((r) => r.id === requested) || state.runs[0];
        await loadRun(selected.id);
      }
      else if (!initial && state.runs.length && !state.mediaBlob && !state.localComments) {
        const selected = state.runs.find((r) => r.id === state.runId) || state.runs[0];
        await loadRun(selected.id);
      }
      else if (!state.runs.length && !state.mediaUrl && !state.comments.length) {
        $('runSelect').options[0].textContent = '还没有生成结果';
        $('runInfo').textContent = '可以直接导入视频与弹幕';
      } else if (!initial) notify('运行列表已刷新。', 'info');
    } catch (e) {
      populateRuns();
      $('runSelect').options[0].textContent = '本地文件预览';
      notify(`暂时无法读取运行列表。仍可导入本地视频与弹幕。${e.message ? ` ${e.message}` : ''}`, 'info');
    } finally {
      $('refreshRuns').disabled = false; $('refreshRuns').classList.remove('busy');
    }
  }
  async function loadRun(id) {
    if (!id) return;
    if (state.fetchController) state.fetchController.abort();
    const controller = new AbortController(); state.fetchController = controller;
    const serial = ++state.fetchSerial;
    setPill('读取中'); notify('');
    try {
      const data = await readJson(`/api/runs/${encodeURIComponent(id)}`, controller.signal);
      if (serial !== state.fetchSerial) return;
      // Validate before replacing the previous visible run.
      normalizeComments(data);
      state.runId = id; state.data = data; state.localComments = false;
      const rejected = installComments(data);
      renderUsage(data.usage, data.diagnostics);
      const title = data.video?.title || data.title || id;
      const duration = number(data.video?.duration) || 0;
      const url = safeServerVideoUrl(data.video?.url);
      setMedia(url, title, duration);
      $('runSelect').value = id;
      $('runInfo').textContent = `${timecode(duration)} · ${count(state.comments.length)} 条弹幕`;
      $('runInfo').title = title;
      const status = data.status || data.diagnostics?.status || 'completed';
      if (status === 'partial' || data.diagnostics?.partial) setPill('部分完成', 'partial');
      else if (status === 'demo' || data.demo) setPill('演示数据', 'neutral');
      else setPill('已就绪', '');
      $('localHint').textContent = '切换生成结果不会重新调用模型；预览设置可随时调整。';
      if (rejected) notify(`已略过 ${rejected} 条缺少有效时间或文字的记录。`, 'info');
      else if (status === 'partial' || data.diagnostics?.partial) notify('这次生成有部分调用未完成，当前显示已成功生成的弹幕。', 'info');
      setFollow(true);
    } catch (e) {
      if (e.name === 'AbortError') return;
      setPill('读取失败', 'partial');
      $('runSelect').value = state.runId || '';
      notify(`无法打开这次运行。${e.message ? ` ${e.message}` : ''}`);
    }
  }
  async function importVideo(file) {
    if (!file) return;
    if (state.fetchController) state.fetchController.abort(); state.fetchSerial++;
    releaseMediaBlob();
    const blob = URL.createObjectURL(file); state.mediaBlob = blob;
    setMedia(blob, file.name, 0, true);
    $('runInfo').textContent = file.name;
    $('runInfo').title = file.name;
    $('localHint').textContent = `正在预览「${file.name}」。本地文件不会上传，当前弹幕会保留。`;
    setPill('本地视频', ''); notify(''); populateRuns();
  }
  async function importComments(file) {
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      normalizeComments(payload);
      if (state.fetchController) state.fetchController.abort(); state.fetchSerial++;
      state.runId = null; state.data = payload; state.localComments = true;
      const rejected = installComments(payload);
      renderUsage(!Array.isArray(payload) ? payload.usage : null, payload?.diagnostics);
      populateRuns();
      setPill('本地弹幕', '');
      $('runInfo').textContent = `${count(state.comments.length)} 条 · ${file.name}`;
      if (!state.mediaUrl) {
        state.mediaDuration = payload?.format === 'danmaku-track/v1' ? number(payload.timeline?.target_duration) ?? ((number(payload.video?.duration) || 0) * (number(payload.timeline?.scale) ?? 1) + (number(payload.timeline?.offset) ?? 0)) : number(payload?.video?.duration) || 0;
        updateTransport();
        placeholder('弹幕已就位，等你选片', '选择对应的视频文件，点击列表中的时间即可跳转。');
      }
      $('localHint').textContent = '本地选择的视频与弹幕仅在浏览器中预览。';
      notify(rejected ? `已载入 ${state.comments.length} 条弹幕，略过 ${rejected} 条无效记录。` : '', 'info');
    } catch (e) { notify(`无法导入弹幕。${e.message ? ` ${e.message}` : '请检查 JSON 格式。'}`); }
  }

  function xmlEscape(text) { return String(text).replace(/[<>&"']/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' }[c])); }
  function cleanComment(c) {
    const { _index, ...exported } = c;
    return exported;
  }
  function localXml() {
    const lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<i>', '<chatserver>local</chatserver><chatid>0</chatid><state>0</state>'];
    state.comments.forEach((c, i) => {
      lines.push(`<d p="${c.time.toFixed(3)},${c.mode},${Math.round(c.size)},${c.color},0,0,simulator,${i + 1}">${xmlEscape(c.text)}</d>`);
    });
    lines.push('</i>'); return lines.join('\n');
  }
  function assTime(value) {
    const cs = Math.max(0, Math.round(value * 100));
    return `${Math.floor(cs / 360000)}:${String(Math.floor(cs % 360000 / 6000)).padStart(2, '0')}:${String(Math.floor(cs % 6000 / 100)).padStart(2, '0')}.${String(cs % 100).padStart(2, '0')}`;
  }
  function assText(value) { return value.replace(/\\/g, '＼').replace(/\{/g, '｛').replace(/\}/g, '｝').replace(/[\r\n]/g, ' '); }
  function assColor(rgb) { return `&H${(rgb & 255).toString(16).padStart(2, '0')}${((rgb >> 8) & 255).toString(16).padStart(2, '0')}${((rgb >> 16) & 255).toString(16).padStart(2, '0')}&`; }
  function localAss() {
    const layout = makeLayout(state.comments, 1920, 1080, { fontSize: 42, area: 0.5, density: 100 });
    const lines = ['[Script Info]', 'Title: Danmaku Simulator', 'ScriptType: v4.00+', 'PlayResX: 1920', 'PlayResY: 1080', 'WrapStyle: 2', 'ScaledBorderAndShadow: yes', '', '[V4+ Styles]', 'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding', 'Style: Danmaku,Noto Sans CJK SC,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,4,0,0,0,1', '', '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text'];
    layout.items.forEach((c) => {
      const pos = c.mode === 1 ? `\\an4\\move(1920,${Math.round(c.y)},${-Math.ceil(c.textWidth)},${Math.round(c.y)})` : `\\an5\\pos(960,${Math.round(c.y)})`;
      const tags = `{${pos}\\fs${Math.round(c.font)}\\c${assColor(c.color)}}`;
      lines.push(`Dialogue: 0,${assTime(c.time)},${assTime(c.end)},Danmaku,,0,0,0,,${tags}${assText(c.text)}`);
    });
    if (layout.overflow) notify(`ASS 已按轨道避让，略过 ${layout.overflow} 条拥挤弹幕；XML 与 JSON 可保留全部时间记录。`, 'info');
    return lines.join('\n');
  }
  function download(blob, name) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 15000);
  }
  async function exportFile(format) {
    if (!state.comments.length) return;
    const button = $(`export${format.charAt(0).toUpperCase()}${format.slice(1)}`);
    if (button) button.disabled = true;
    try {
      let blob;
      if (state.runId && !state.localComments) {
        const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/export.${format}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        blob = await response.blob();
      } else {
        const content = format === 'xml' ? localXml() : format === 'ass' ? localAss() : JSON.stringify({
          schema_version: 1, video: { title: state.mediaTitle || state.data?.video?.title || '本地预览', duration: getDuration() },
          comments: state.comments.map(cleanComment),
          ...(state.data?.usage ? { usage: state.data.usage } : {}),
          ...(state.data?.diagnostics ? { diagnostics: state.data.diagnostics } : {}),
        }, null, 2);
        blob = new Blob([content], { type: format === 'xml' ? 'application/xml;charset=utf-8' : format === 'json' ? 'application/json;charset=utf-8' : 'text/plain;charset=utf-8' });
      }
      const name = (state.runId || 'danmaku').replace(/[^\p{L}\p{N}_-]/gu, '-').slice(0, 100);
      download(blob, `${name}.${format}`);
    } catch (e) { notify(`导出失败。${e.message ? ` ${e.message}` : ''}`); }
    finally { if (button) button.disabled = !state.comments.length; }
  }

  // UI events.
  $('importVideo').addEventListener('click', () => $('videoFile').click());
  $('emptyChooseVideo').addEventListener('click', (e) => { e.stopPropagation(); $('videoFile').click(); });
  $('importComments').addEventListener('click', () => $('commentsFile').click());
  $('videoFile').addEventListener('change', (e) => { importVideo(e.target.files[0]); e.target.value = ''; });
  $('commentsFile').addEventListener('change', (e) => { importComments(e.target.files[0]); e.target.value = ''; });
  $('runSelect').addEventListener('change', (e) => {
    if (e.target.value) loadRun(e.target.value);
    else e.target.value = state.runId || '';
  });
  $('refreshRuns').addEventListener('click', () => loadRuns(false));
  $('play').addEventListener('click', togglePlay);
  $('rewind').addEventListener('click', () => seekTo((video.currentTime || 0) - 10));
  $('seek').addEventListener('input', (e) => seekTo(Number(e.target.value)));
  $('playbackRate').addEventListener('change', (e) => { video.playbackRate = Number(e.target.value); });
  $('volume').addEventListener('input', (e) => { video.muted = false; video.volume = Number(e.target.value); });
  $('mute').addEventListener('click', () => { video.muted = !video.muted; });
  $('fullscreen').addEventListener('click', toggleFullscreen);
  $('videoStage').addEventListener('click', (e) => { if (!e.target.closest('button') && state.mediaReady) togglePlay(); });
  $('videoStage').addEventListener('dblclick', (e) => { if (!e.target.closest('button')) toggleFullscreen(); });
  $('danmakuToggle').addEventListener('click', () => {
    state.settings.enabled = !state.settings.enabled; updateSettingsLabels(); savePreferences(); drawFrame();
  });
  ['density', 'opacity', 'fontSize'].forEach((id) => {
    $(id).addEventListener('input', (e) => {
      state.settings[id] = Number(e.target.value); updateSettingsLabels(); savePreferences();
      if (id === 'opacity') drawFrame(); else rebuildLayout();
    });
  });
  $('displayArea').addEventListener('change', (e) => { state.settings.area = Number(e.target.value); savePreferences(); rebuildLayout(); });
  $('follow').addEventListener('click', () => setFollow(!state.follow));
  $('commentSearch').addEventListener('input', () => { setFollow(false); applyCommentFilter(); });
  $('commentsScroll').addEventListener('scroll', () => renderRows());
  $('commentsScroll').addEventListener('wheel', () => setFollow(false), { passive: true });
  $('commentsScroll').addEventListener('touchstart', () => setFollow(false), { passive: true });
  ['xml', 'ass', 'json'].forEach((type) => {
    $(`export${type.charAt(0).toUpperCase()}${type.slice(1)}`).addEventListener('click', () => exportFile(type));
  });
  video.addEventListener('loadedmetadata', () => {
    state.mediaReady = true;
    ['play', 'rewind', 'seek'].forEach((id) => { $(id).disabled = false; });
    $('playerPlaceholder').hidden = true;
    $('videoCornerLabel').hidden = false;
    updateTransport(); resizeCanvas(); updateCommentFocus(true);
  });
  video.addEventListener('loadeddata', () => { $('mediaBusy').hidden = true; drawFrame(); });
  video.addEventListener('canplay', () => { $('mediaBusy').hidden = true; });
  video.addEventListener('waiting', () => { if (!video.paused) $('mediaBusy').hidden = false; });
  video.addEventListener('playing', () => { $('mediaBusy').hidden = true; startAnimation(); });
  video.addEventListener('play', () => { updateTransport(); startAnimation(); });
  video.addEventListener('pause', () => { stopAnimation(); updateTransport(); updateCommentFocus(); });
  video.addEventListener('ended', () => { stopAnimation(); updateTransport(); updateCommentFocus(); });
  video.addEventListener('timeupdate', () => { updateTransport(); updateCommentFocus(); if (video.paused) drawFrame(); });
  video.addEventListener('seeking', () => { drawFrame(); updateCommentFocus(true); });
  video.addEventListener('seeked', () => { $('mediaBusy').hidden = true; drawFrame(); updateTransport(); updateCommentFocus(true); });
  video.addEventListener('ratechange', () => { $('playbackRate').value = String(video.playbackRate); drawFrame(); });
  video.addEventListener('volumechange', updateVolume);
  video.addEventListener('error', () => {
    if (!state.mediaUrl) return;
    state.mediaReady = false; stopAnimation(); $('mediaBusy').hidden = true;
    ['play', 'rewind', 'seek'].forEach((id) => { $(id).disabled = true; });
    placeholder('这段视频暂时无法播放', '检查文件或浏览器格式支持，也可选择另一份本地视频。');
    notify('视频未能载入。弹幕与费用数据仍然可查看、导出。', 'info');
  });
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.altKey || e.metaKey || e.target.closest('input,textarea,select,button,a,[contenteditable="true"]')) return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); if (state.mediaReady) seekTo(video.currentTime - 5); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); if (state.mediaReady) seekTo(video.currentTime + 5); }
    else if (e.key.toLowerCase() === 'f') toggleFullscreen();
    else if (e.key.toLowerCase() === 'm') video.muted = !video.muted;
    else if (e.key.toLowerCase() === 'd') $('danmakuToggle').click();
  });
  document.addEventListener('fullscreenchange', resizeCanvas);
  if (window.ResizeObserver) {
    new ResizeObserver(resizeCanvas).observe($('videoStage'));
    new ResizeObserver(() => renderRows(true)).observe($('commentsScroll'));
  } else window.addEventListener('resize', () => { resizeCanvas(); renderRows(true); });
  window.addEventListener('pagehide', () => { stopAnimation(); releaseMediaBlob(); state.fetchController?.abort(); });

  loadPreferences(); updateVolume(); resizeCanvas(); loadRuns(true);
})();
