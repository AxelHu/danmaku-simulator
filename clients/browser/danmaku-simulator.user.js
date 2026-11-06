// ==UserScript==
// @name         Danmaku Simulator · 网页弹幕
// @namespace    https://github.com/AxelHu/danmaku-simulator
// @version      0.2.0
// @description  给 HTML5 视频叠加用户选择的本地或远端弹幕轨道。
// @match        https://*/*
// @match        http://*/*
// @run-at       document-idle
// @noframes
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM.registerMenuCommand
// @connect      localhost
// @connect      127.0.0.1
// @connect      *
// @license      MIT
// ==/UserScript==

/* No site account, cookie, token, comment-posting or player-control API is used. */
(() => {
  'use strict';

  const VERSION = '0.2.0';
  const LIFETIME = 7;
  const MAX_BYTES = 32 * 1024 * 1024;
  const FONT = '"Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif';
  const finite = (x) => x !== null && x !== undefined && x !== '' && typeof x !== 'boolean' && Number.isFinite(Number(x)) ? Number(x) : null;
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

  function httpUrl(value, base) {
    let url;
    try { url = base ? new URL(String(value), base) : new URL(String(value)); }
    catch (_) { throw new Error('请输入完整的 HTTP 或 HTTPS 地址。'); }
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('轨道来源需要 HTTP 或 HTTPS 地址。');
    if (url.username || url.password) throw new Error('请使用不含用户名和密码的来源地址。');
    url.hash = '';
    return url.href;
  }

  function canonicalWebUrl(value) {
    const u = new URL(value);
    if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error('页面绑定需要不含凭据的 HTTP(S) 地址。');
    const pairs = [...u.searchParams].filter(([key]) => !key.toLowerCase().startsWith('utm_') && !['spm_id_from', 'share_source'].includes(key));
    pairs.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0);
    u.search = new URLSearchParams(pairs).toString();
    // Fragment routing can identify an episode, so keep it in the local key.
    return u.href;
  }

  function pageIdentity(value, cid = '') {
    const u = new URL(value);
    if (/(^|\.)bilibili\.com$/i.test(u.hostname)) {
      const match = u.pathname.match(/\/video\/(BV[\da-z]{10})(?:\/|$)/i);
      const bvid = match ? 'BV' + match[1].slice(2) : (u.searchParams.get('bvid') || '');
      if (/^BV[\da-z]{10}$/i.test(bvid)) {
        const page = Math.max(1, Math.floor(finite(u.searchParams.get('p')) || 1));
        const rawCid = u.searchParams.get('cid') || cid;
        const knownCid = /^\d+$/.test(String(rawCid)) ? String(rawCid) : '';
        return { site: 'bilibili', bvid, page, cid: knownCid,
          key: `bilibili:${bvid}:p${page}${knownCid ? `:cid${knownCid}` : ''}`,
          label: `B站 ${bvid} · P${page}${knownCid ? ` · cid ${knownCid}` : ''}` };
      }
    }
    // URL state is compared locally. It is never sent to a track server.
    const canonical = canonicalWebUrl(value);
    return { site: 'web', url: canonical, key: canonical, label: `${u.hostname}${u.pathname}` };
  }

  function bindingCheck(bindings, identity) {
    if (!Array.isArray(bindings) || !bindings.length) return { state: 'manual', reason: '手动选择的轨道' };
    let definite = false;
    for (const b of bindings) {
      if (!b || typeof b !== 'object') continue;
      if (b.site === 'bilibili' && identity.site === 'bilibili' && b.bvid) {
        definite = true;
        const sameVideo = String(b.bvid) === identity.bvid && Number(b.page ?? 1) === identity.page;
        const sameCid = !b.cid || !identity.cid || String(b.cid) === identity.cid;
        if (sameVideo && sameCid) return { state: 'match', reason: identity.cid ? 'BV / 分P / cid 已匹配' : 'BV / 分P 已匹配' };
      } else if (b.site === 'web' && b.url) {
        let expected;
        try { expected = canonicalWebUrl(String(b.url)); } catch (_) { continue; }
        definite = true;
        if (expected === identity.url || expected === identity.key) return { state: 'match', reason: '页面地址已匹配' };
        if (identity.site === 'bilibili') {
          try {
            const p = pageIdentity(expected);
            if (p.site === 'bilibili' && p.bvid === identity.bvid && p.page === identity.page && (!p.cid || !identity.cid || p.cid === identity.cid)) {
              return { state: 'match', reason: 'B站页面已匹配' };
            }
          } catch (_) { /* Invalid external metadata is not a match. */ }
        }
      }
    }
    return definite
      ? { state: 'mismatch', reason: '轨道绑定与当前视频不符；请换成对应的视频或轨道。' }
      : { state: 'unknown', reason: '当前页面无法核对这条轨道的绑定；请确认内容后点击“匹配当前视频”。' };
  }

  function normalizeTrack(data) {
    const raw = Array.isArray(data) ? data : data && data.comments;
    if (!Array.isArray(raw)) throw new Error('JSON 需要包含 comments 数组，或直接提供弹幕数组。');
    const timeline = !Array.isArray(data) && data.timeline || {};
    const scale = finite(timeline.scale ?? 1), offset = finite(timeline.offset ?? 0);
    if (scale === null || scale <= 0 || offset === null) throw new Error('时间倍率须为正数，偏移须为有限秒数。');
    const targetDuration = finite(timeline.target_duration);
    if (timeline.target_duration !== undefined && timeline.target_duration !== null && (targetDuration === null || targetDuration <= 0)) throw new Error('目标时长须为正数。');
    const comments = []; let rejected = 0, unsupported = 0;
    raw.forEach((c, i) => {
      if (!c || typeof c !== 'object') { rejected++; return; }
      const time = finite(c.time ?? c.timestamp ?? c.at);
      if (time === null || time < 0 || typeof c.text !== 'string' || !c.text.trim()) { rejected++; return; }
      const mode = Number(c.mode ?? 1);
      if (![1, 4, 5].includes(mode)) { unsupported++; return; }
      let color = finite(c.color);
      if (typeof c.color === 'string' && /^#[\da-f]{6}$/i.test(c.color)) color = parseInt(c.color.slice(1), 16);
      comments.push({ id: String(c.id ?? i), time, text: c.text.replace(/[\u0000-\u001f\u007f]/g, ' ').trim(),
        mode, color: color === null ? 16777215 : clamp(Math.round(color), 0, 16777215),
        size: clamp(finite(c.size) ?? 25, 8, 72), index: i, synthetic: c.synthetic === true });
    });
    comments.sort((a, b) => a.time - b.time || a.index - b.index);
    if (raw.length && !comments.length) throw new Error('没有可显示的普通、顶部或底部弹幕。');
    const meta = !Array.isArray(data) && data.metadata || {};
    return { comments, timeline: { scale, offset, ...(targetDuration === null ? {} : { target_duration: targetDuration }) },
      sourceDuration: finite(data?.video?.duration), bindings: !Array.isArray(data) && Array.isArray(data.bindings) ? data.bindings : [],
      label: String(meta.label || data?.video?.title || data?.title || '弹幕轨道'),
      synthetic: meta.synthetic === true || comments.some((c) => c.synthetic),
      revision: typeof meta.revision === 'string' ? meta.revision : '', rejected, unsupported };
  }

  function parsePayload(bytes) {
    if (bytes.byteLength > MAX_BYTES) throw new Error('文件超过当前客户端的 32 MiB 单轨容量。');
    const text = new TextDecoder('utf-8').decode(bytes).replace(/^\uFEFF/, '').trim();
    if (!text) throw new Error('来源返回了空内容。');
    if (text.startsWith('<')) {
      if (/<!DOCTYPE|<!ENTITY/i.test(text)) throw new Error('此客户端仅读取不含 DTD 的普通 B站 XML。');
      const doc = new DOMParser().parseFromString(text, 'application/xml');
      if (doc.querySelector('parsererror') || doc.documentElement.nodeName !== 'i') throw new Error('不是有效的 B站弹幕 XML。');
      const comments = [...doc.querySelectorAll('d[p]')].map((node, i) => {
        const p = node.getAttribute('p').split(',');
        return { id: p[7] || String(i), time: p[0], mode: p[1], size: p[2], color: p[3], text: node.textContent || '' };
      });
      return { kind: 'track', track: normalizeTrack({ comments, metadata: { label: 'B站 XML 弹幕' } }), format: 'XML' };
    }
    let data;
    try { data = JSON.parse(text); } catch (_) { throw new Error('无法解析 JSON；请确认链接直达轨道或目录文件。'); }
    if (data && data.format === 'danmaku-catalog/v1') return { kind: 'catalog', data };
    if (data && data.format && data.format !== 'danmaku-track/v1') throw new Error(`暂不支持此文件格式：${String(data.format).slice(0, 80)}`);
    return { kind: 'track', track: normalizeTrack(data), format: 'JSON' };
  }

  function catalogEntries(data, base) {
    if (!data || !Array.isArray(data.items)) throw new Error('弹幕目录缺少 items 列表。');
    const entries = [];
    for (const item of data.items) for (const track of item?.tracks || []) {
      const f = track?.formats?.json || track?.formats?.xml;
      if (!f || typeof f.href !== 'string') continue;
      let url;
      try { url = httpUrl(f.href, base || undefined); } catch (_) { continue; }
      entries.push({ url, label: `${item.video?.title || item.media_key || '视频'} — ${track.label || track.id || '轨道'}${finite(track.comment_count) !== null ? ` · ${track.comment_count} 条` : ''}`,
        sha256: typeof f.sha256 === 'string' && /^[\da-f]{64}$/i.test(f.sha256) ? f.sha256.toLowerCase() : null,
        bytes: finite(f.bytes), bindings: Array.isArray(track.bindings) ? track.bindings : [], synthetic: track.synthetic === true });
    }
    if (!entries.length) throw new Error('目录没有可读取的 JSON/XML 轨道。相对地址的目录请通过 URL 打开。');
    return entries;
  }

  function hash(s) { let h = 2166136261; for (const c of String(s)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
  function upperBound(items, time) {
    let lo = 0, hi = items.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (items[mid].at <= time) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function planLanes(comments, width, height, settings, measure) {
    if (!(width > 0 && height > 0)) return { items: [], overflow: 0 };
    const font = settings.fontSize * clamp(width / 960, 0.67, 1.5);
    const line = Math.ceil(font * 1.4 + 7), padding = Math.max(8, font * 0.4);
    const lanes = Math.max(1, Math.floor((height * settings.area - 2 * padding) / line));
    const occupied = Array(lanes).fill(null), items = []; let overflow = 0;
    for (const c of comments) {
      if (settings.density < 100 && hash(`${c.id}|${c.time}|${c.index}`) % 100 >= settings.density) continue;
      const at = c.time * settings.scale + settings.offset;
      if (!Number.isFinite(at) || at < 0 || settings.targetDuration !== null && settings.targetDuration !== undefined && at >= settings.targetDuration) continue;
      const size = clamp(font * c.size / 25, font * 0.65, font * 1.4);
      const textWidth = measure(c.text, size), duration = c.mode === 1 ? LIFETIME : 4.5;
      const speed = (width + textWidth) / duration, gap = Math.max(18, font * 1.2);
      let lane = -1;
      for (let n = 0; n < lanes; n++) {
        const index = c.mode === 4 ? lanes - n - 1 : n, p = occupied[index];
        let safe = !p || at >= p.end;
        if (!safe && c.mode === 1 && p.mode === 1) safe = (at - p.at) * p.speed - p.textWidth >= gap && (speed <= p.speed || p.speed * (p.end - p.at) - p.textWidth - speed * (p.end - at) >= gap);
        if (safe) { lane = index; break; }
      }
      if (lane < 0) { overflow++; continue; }
      const item = { ...c, at, end: Math.min(at + duration, settings.targetDuration ?? Infinity), font: size, textWidth, speed, y: padding + line * (lane + 0.5), lane };
      occupied[lane] = item; items.push(item);
    }
    return { items, overflow };
  }

  // Pure functions are available to the repository's Node tests, never on a webpage global.
  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined' && module.exports) module.exports = { httpUrl, canonicalWebUrl, pageIdentity, bindingCheck, normalizeTrack, catalogEntries, planLanes, upperBound };
    return;
  }
  if (window.top !== window.self || document.querySelector('[data-dms-client]')) return;

  const state = {
    videos: [], discovered: new Set(), watchedRoots: new WeakSet(), ids: new WeakMap(), nextId: 1, selected: null, listeners: [], bound: null,
    track: null, source: null, catalog: [], identity: pageIdentity(location.href),
    settings: { enabled: true, density: 75, opacity: 85, fontSize: 24, area: 0.5, offset: 0, scale: 1 },
    layout: [], width: 0, height: 0, dpr: 1, raf: 0, scanTimer: 0, epoch: 0,
    serial: 0, request: null, busy: false, reason: '', reasonKind: '', destroyed: false, suspended: false,
    fullscreenUnavailable: false, toastTimer: 0, videoListKey: '', needsLayout: true, filePickArmed: false,
  };

  const host = document.createElement('div');
  host.setAttribute('data-dms-client', VERSION);
  host.style.cssText = 'all:initial!important;display:block!important;font-family:"Microsoft YaHei","PingFang SC",sans-serif!important;font-size:13px!important;color:#edf0f7!important;color-scheme:dark!important;position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;max-width:none!important;max-height:none!important;margin:0!important;padding:0!important;border:0!important;background:transparent!important;overflow:hidden!important;pointer-events:none!important;z-index:2147483647!important;';
  // A closed panel also keeps page scripts away from the URL field and imported metadata.
  const shadow = host.attachShadow({ mode: 'closed' });
  const css = document.createElement('style');
  css.textContent = `
    :host{color-scheme:dark;display:block;font-family:Inter,"Microsoft YaHei","PingFang SC",sans-serif;font-size:13px;color:#edf0f7} :host::backdrop{background:transparent;pointer-events:none}
    *{box-sizing:border-box} [hidden]{display:none!important} button,input,select{font:inherit} button{cursor:pointer} button:disabled{cursor:default;opacity:.4} button,input,select{color:#eef0f7} button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #fd91a3;outline-offset:2px}
    canvas{position:absolute;pointer-events:none;display:block} #launcher{position:absolute;right:20px;bottom:24px;border:1px solid #ffb1c166;border-radius:23px;background:#292333ed;color:#ffd2da;box-shadow:0 4px 20px #0006;padding:10px 14px;font-size:12px;pointer-events:auto} #launcher.pending{color:#f7cb83;border-color:#f7cb8366} #launcher .dot{display:inline-block;width:6px;height:6px;margin-right:6px;border-radius:50%;background:#f28ca1} #launcher.pending .dot{background:#f7cb83}
    #panel{position:absolute;right:18px;bottom:74px;width:342px;max-width:calc(100vw - 32px);max-height:calc(100vh - 98px);overflow:auto;overscroll-behavior:contain;border:1px solid #454253;border-radius:13px;background:#171923f7;box-shadow:0 12px 50px #0008;padding:16px;pointer-events:auto;line-height:1.5;scrollbar-width:thin} :host(.fullscreen) #launcher{bottom:88px} :host(.fullscreen) #panel{bottom:138px;max-height:calc(100vh - 160px)}
    .heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.heading strong{font-size:15px;font-weight:650}.heading small{font-size:10px;color:#f49aad;margin-left:7px}.close{border:0;background:transparent;color:#a6adbf;padding:1px 5px;font-size:20px;line-height:1.2}.muted{color:#929caf;font-size:11px}.row{display:flex;align-items:center;gap:7px;margin-top:9px}.row>*:first-child:not(button){flex:1;min-width:0}.row input[type=url]{width:0}.field{width:100%;min-width:0;border:1px solid #373c4f;border-radius:6px;background:#202432;padding:7px 8px;font-size:12px}.field::placeholder{color:#778298}select.field{padding-right:4px}.button{border:1px solid #43485b;border-radius:6px;background:#292d3d;padding:7px 10px;font-size:11px;white-space:nowrap}.button:hover{background:#35384a}.button.accent{color:#211b26;background:#f48ca2;border-color:#f48ca2}.button.wide{width:100%;margin-top:9px}.divider{border:0;border-top:1px solid #303545;margin:14px 0}.caption{display:block;font-size:11px;color:#a9b2c5;margin-bottom:5px}#identity{font-size:10px;color:#9aa6bb;margin-top:6px;overflow-wrap:anywhere}#status{font-size:11px;line-height:1.6;margin:10px 0 0;padding:8px 9px;border-radius:6px;background:#222836;color:#bdc8d9}#status.error{color:#ffc0cc;background:#402431}#status.pending{color:#f0ce98;background:#382e23}#status.ready{color:#a1dcc9;background:#21352e}
    #track{margin-top:10px;padding:9px;border:1px solid #37394a;border-radius:7px;font-size:11px}#trackLabel{font-weight:600;overflow-wrap:anywhere}#trackInfo,#source{color:#94a2b9;font-size:10px;margin-top:4px;overflow-wrap:anywhere}.synthetic{display:inline-block;color:#ffb3c2;font-size:10px;padding:1px 5px;border-radius:4px;background:#f488a016;margin-left:6px} .switch{display:flex;align-items:center;justify-content:space-between;font-size:12px;margin-bottom:10px}.switch input{accent-color:#ef8ca2;width:16px;height:16px}.settings{display:grid;grid-template-columns:1fr 1fr;gap:12px 15px}.setting{font-size:11px;color:#a5b0c4;display:block}.setting>span{display:flex;justify-content:space-between;gap:4px}.setting output{color:#d6ddeb;font-size:10px}.setting input[type=range]{width:100%;accent-color:#ed8da1;margin:7px 0 0;height:12px}.setting .field{margin-top:5px;font-size:11px}.hint{font-size:10px;color:#8391a7;margin:8px 0 0;line-height:1.6}.offset{display:flex;gap:4px;margin-top:5px}.offset .field{margin-top:0;min-width:0;width:80px;flex:1}.offset button{padding:5px 8px;font-size:12px}.footer{font-size:9px;color:#7e8ba3;margin-top:10px}
    #badge{position:absolute;pointer-events:none;font-size:10px;color:#ffe0e7;background:#1c1823b8;border:1px solid #f8bdcb35;border-radius:4px;padding:3px 6px;white-space:nowrap}#toast{position:absolute;right:20px;bottom:72px;max-width:310px;padding:9px 12px;border:1px solid #f4ca8044;border-radius:7px;background:#332b22ee;color:#f4d19d;font-size:11px;line-height:1.6;pointer-events:none}#catalog{margin-top:9px}#catalog .field{font-size:11px} @media(max-width:480px){#panel{right:12px;bottom:70px;padding:13px}#launcher{right:13px;bottom:20px}}
  `;
  shadow.append(css);

  function el(tag, attrs = {}, ...children) {
    const n = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'text') n.textContent = value;
      else if (key === 'hidden') n.hidden = value;
      else if (key === 'className') n.className = value;
      else n.setAttribute(key, String(value));
    }
    for (const child of children) n.append(typeof child === 'string' ? document.createTextNode(child) : child);
    return n;
  }
  const canvas = el('canvas', { id: 'overlay', hidden: true, 'aria-hidden': 'true' });
  const ctx = canvas.getContext('2d');
  const ui = {
    launcher: el('button', { id: 'launcher', type: 'button', hidden: true, title: '打开弹幕模拟器' }, el('span', { className: 'dot' }), '弹幕'),
    panel: el('section', { id: 'panel', hidden: true, 'aria-label': '弹幕模拟器' }),
    close: el('button', { type: 'button', className: 'close', 'aria-label': '关闭面板', text: '×' }),
    videoSelect: el('select', { id: 'video', className: 'field', 'aria-label': '选择页面视频' }),
    rescan: el('button', { id: 'rescan', type: 'button', className: 'button', title: '重新寻找视频', text: '↻' }),
    identity: el('div', { id: 'identity' }),
    status: el('div', { id: 'status', role: 'status', 'aria-live': 'polite' }),
    file: el('input', { id: 'file', type: 'file', accept: '.json,.xml,application/json,application/xml,text/xml', hidden: true }),
    fileButton: el('button', { id: 'openFile', type: 'button', className: 'button wide', text: '打开本地 JSON / XML' }),
    url: el('input', { id: 'url', className: 'field', type: 'url', placeholder: '粘贴轨道或目录 URL', 'aria-label': '弹幕来源 URL', autocomplete: 'off' }),
    remote: el('button', { id: 'loadUrl', type: 'button', className: 'button accent', text: '读取' }),
    catalog: el('div', { id: 'catalog', hidden: true }),
    catalogSelect: el('select', { id: 'catalogTrack', className: 'field', 'aria-label': '选择目录轨道' }),
    catalogLoad: el('button', { id: 'loadCatalogTrack', type: 'button', className: 'button wide', text: '载入所选轨道' }),
    track: el('div', { id: 'track', hidden: true }),
    trackLabel: el('span', { id: 'trackLabel' }),
    trackType: el('span', { className: 'synthetic' }),
    trackInfo: el('div', { id: 'trackInfo' }),
    alignment: el('div', { id: 'alignment', className: 'hint', hidden: true }),
    source: el('div', { id: 'source' }),
    bind: el('button', { id: 'bind', type: 'button', className: 'button wide', text: '匹配当前视频', hidden: true }),
    enabled: el('input', { id: 'enabled', type: 'checkbox', checked: '', 'aria-label': '显示附加弹幕' }),
    offset: el('input', { id: 'offset', className: 'field', type: 'number', step: '0.1', value: '0', 'aria-label': '时间偏移秒数' }),
    earlier: el('button', { id: 'earlier', type: 'button', className: 'button', text: '−', title: '提前半秒' }),
    later: el('button', { id: 'later', type: 'button', className: 'button', text: '+', title: '延后半秒' }),
    scale: el('input', { id: 'scale', className: 'field', type: 'number', min: '0.0001', step: '0.0001', value: '1', 'aria-label': '时间倍率' }),
    density: el('input', { id: 'density', type: 'range', min: '10', max: '100', step: '5', value: '75' }),
    opacity: el('input', { id: 'opacity', type: 'range', min: '20', max: '100', step: '5', value: '85' }),
    fontSize: el('input', { id: 'fontSize', type: 'range', min: '14', max: '44', step: '1', value: '24' }),
    area: el('select', { id: 'area', className: 'field' }, el('option', { value: '0.3', text: '顶部三分之一' }), el('option', { value: '0.5', selected: '', text: '上半屏' }), el('option', { value: '0.75', text: '四分之三屏' }), el('option', { value: '1', text: '全屏' })),
    fullscreen: el('button', { id: 'fullscreen', type: 'button', className: 'button wide', text: '带弹幕全屏' }),
    footer: el('p', { className: 'footer', text: '附加弹幕层 · 原站播放与弹幕保持原样' }),
    badge: el('span', { id: 'badge', hidden: true }), toast: el('div', { id: 'toast', hidden: true }),
  };
  ui.catalog.append(ui.catalogSelect, ui.catalogLoad);
  ui.track.append(el('div', {}, ui.trackLabel, ui.trackType), ui.trackInfo, ui.source, ui.alignment);
  const settings = el('div', { className: 'settings' });
  settings.append(el('label', { className: 'setting', for: 'offset' }, '偏移（秒）', el('div', { className: 'offset' }, ui.earlier, ui.offset, ui.later)),
    el('label', { className: 'setting', for: 'scale' }, '时间倍率', ui.scale));
  for (const [key, name] of [['density', '密度'], ['opacity', '透明度'], ['fontSize', '字号']]) {
    ui[`${key}Value`] = el('output', { for: key, text: String(state.settings[key]) + (key === 'fontSize' ? '' : '%') });
    settings.append(el('label', { className: 'setting', for: key }, el('span', {}, name, ui[`${key}Value`]), ui[key]));
  }
  settings.append(el('label', { className: 'setting', for: 'area' }, '显示范围', ui.area));
  ui.panel.append(el('div', { className: 'heading' }, el('div', {}, el('strong', { text: '弹幕模拟器' }), el('small', { text: 'WEB' })), ui.close),
    el('span', { className: 'caption', text: '当前页面视频' }), el('div', { className: 'row' }, ui.videoSelect, ui.rescan), ui.identity, ui.status,
    ui.fileButton, ui.file, el('div', { className: 'row' }, ui.url, ui.remote), ui.catalog, ui.track, ui.bind,
    el('hr', { className: 'divider' }), el('label', { className: 'switch', for: 'enabled' }, '显示附加弹幕', ui.enabled), settings,
    el('p', { className: 'hint', text: '正偏移让弹幕延后。视频时间 = 弹幕时间 × 倍率 + 偏移。' }), ui.fullscreen, ui.footer);
  shadow.append(canvas, ui.badge, ui.launcher, ui.panel, ui.toast);
  document.documentElement.append(host);

  function panel(show = true) { ui.panel.hidden = !show; if (show) { ui.toast.hidden = true; discover(); scan(); } }
  function notify(message, kind = 'pending', toast = false) {
    state.reason = message; state.reasonKind = kind; refreshStatus();
    if (toast && ui.panel.hidden) {
      ui.toast.textContent = message; ui.toast.hidden = false;
      clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 6000);
    }
  }
  function refreshStatus() {
    const mismatch = state.track && state.selected ? bindingCheck(state.track.bindings, readIdentity()) : null;
    const alignment = alignmentInfo();
    ui.alignment.hidden = !alignment;
    ui.alignment.textContent = alignment || '';
    ui.bind.hidden = !state.track;
    ui.bind.disabled = !state.selected || state.busy || mismatch?.state === 'mismatch';
    ui.remote.disabled = ui.catalogLoad.disabled = state.busy;
    ui.fullscreen.disabled = !state.selected;
    ui.launcher.classList.toggle('pending', !!state.track && !state.bound);
    host.setAttribute('data-state', state.bound && state.settings.enabled ? 'active' : state.track ? 'pending' : 'idle');
    if (state.busy) { ui.status.textContent = '正在读取所选来源…'; ui.status.className = ''; }
    else if (state.reason) { ui.status.textContent = state.reason; ui.status.className = state.reasonKind; }
    else if (state.bound) { ui.status.textContent = state.settings.enabled ? state.bound.allowDurationMismatch ? '已手动匹配；请用时间偏移校准此版本。' : '轨道已匹配，弹幕跟随视频播放。' : '轨道已匹配，附加弹幕已关闭。'; ui.status.className = 'ready'; }
    else { ui.status.textContent = state.selected ? '选择一个本地文件或远端弹幕来源。' : '此页尚未发现可用的 HTML5 视频。'; ui.status.className = ''; }
  }
  function trusted(event) {
    if (event?.isTrusted) return true;
    notify('请在面板中亲自点击读取或选择文件。', 'pending'); return false;
  }
  function publicSource(source) {
    if (source.kind === 'file') return `本地文件：${source.name}`;
    try { const u = new URL(source.url); return `${u.origin}${u.pathname}`; } catch (_) { return '用户选择的来源'; }
  }
  function readIdentity() {
    const v = state.selected;
    const cid = v?.getAttribute('data-cid') || v?.closest('[data-cid]')?.getAttribute('data-cid') || '';
    return pageIdentity(location.href, cid);
  }
  function fingerprint() {
    const v = state.selected;
    return { element: v, page: readIdentity().key, source: v?.currentSrc || v?.getAttribute('src') || '', srcObject: v?.srcObject || null };
  }
  function sameContext(a, b) {
    return a.element === b.element && a.page === b.page && a.srcObject === b.srcObject && (!a.source || a.source === b.source);
  }
  function expectedDuration() {
    return state.track?.timeline.target_duration ?? (state.track?.sourceDuration > 0 ? state.track.sourceDuration * state.settings.scale + state.settings.offset : null);
  }
  function alignmentInfo() {
    const expected = expectedDuration(), actual = finite(state.selected?.duration);
    if (!(expected > 0 && actual > 0) || Math.abs(actual - expected) <= Math.max(2, expected * 0.01)) return '';
    return `轨道预计 ${expected.toFixed(1)} 秒，视频 ${actual.toFixed(1)} 秒，相差 ${Math.abs(actual - expected).toFixed(1)} 秒。请调整时间轴或确认当前版本后手动匹配。`;
  }
  function invalidate(reason) {
    if (!state.bound) return;
    state.bound = null; state.epoch++; clearCanvas();
    notify(reason || '视频发生变化，旧轨道已停用；请重新匹配。', 'pending', true);
  }
  function checkContext() {
    if (!state.bound) return;
    const next = fingerprint(), old = state.bound;
    if (!state.selected?.isConnected || next.element !== old.element || next.page !== old.page || next.srcObject !== old.srcObject || old.source && next.source && old.source !== next.source) {
      invalidate('视频或分集发生变化，旧轨道已停用；请重新匹配。');
    } else if (!old.allowDurationMismatch && alignmentInfo()) {
      invalidate(alignmentInfo());
    } else if (!old.source && next.source) old.source = next.source;
  }
  function applyTrack(manual = false) {
    if (!state.selected?.isConnected) {
      if (manual) { discover(); scan(); }
      else { state.bound = null; clearCanvas(); notify('播放器已变化，请重新选择视频并匹配轨道。', 'pending'); return; }
    }
    if (!state.track || !state.selected) { notify('先选择页面视频与弹幕轨道。'); return; }
    const match = bindingCheck(state.track.bindings, readIdentity());
    if (match.state === 'mismatch' || match.state === 'unknown' && !manual) {
      state.bound = null; clearCanvas(); notify(match.reason, 'pending'); return;
    }
    const alignment = alignmentInfo();
    if (alignment && !manual) { state.bound = null; clearCanvas(); notify(alignment, 'pending'); return; }
    state.bound = { ...fingerprint(), allowDurationMismatch: !!alignment && manual }; state.reason = ''; state.reasonKind = '';
    refreshStatus(); rebuild(); startDrawing();
  }

  function collectVideos(root = document, depth = 0, out = []) {
    if (depth > 8) return out;
    if (root.tagName === 'VIDEO') out.push(root);
    if (root.shadowRoot) {
      watchRoot(root.shadowRoot);
      collectVideos(root.shadowRoot, depth + 1, out);
    }
    for (const node of root.querySelectorAll('*')) {
      if (node === host) continue;
      if (node.tagName === 'VIDEO') out.push(node);
      if (node.shadowRoot) {
        watchRoot(node.shadowRoot);
        collectVideos(node.shadowRoot, depth + 1, out);
      }
    }
    return out;
  }
  function watchRoot(root) {
    if (!state.watchedRoots.has(root)) {
      state.watchedRoots.add(root);
      observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });
    }
  }
  function discover(root = document) {
    let changed = false;
    for (const video of collectVideos(root)) {
      if (!state.discovered.has(video)) { state.discovered.add(video); changed = true; }
    }
    return changed;
  }
  function visibleArea(video) {
    const r = video.getBoundingClientRect(), c = getComputedStyle(video);
    if (c.display === 'none' || c.visibility === 'hidden' || Number(c.opacity) === 0 || r.width < 20 || r.height < 20) return 0;
    return Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
  }
  function videoId(v) { if (!state.ids.has(v)) state.ids.set(v, state.nextId++); return state.ids.get(v); }
  function duration(v) { const n = Math.floor(v.duration); return Number.isFinite(n) ? `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}` : '待载入'; }
  function listenVideo(video, type, callback) { video.addEventListener(type, callback); state.listeners.push(() => video.removeEventListener(type, callback)); }
  const resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { position(); draw(); startDrawing(); }) : null;
  function selectVideo(video, userChoice = false) {
    if (video === state.selected) return;
    invalidate(userChoice ? '已切换视频；请确认对应轨道后重新匹配。' : '页面已更换视频；旧轨道已停用，请重新匹配。');
    state.listeners.forEach((remove) => remove()); state.listeners = [];
    if (state.selected) resizeObserver?.unobserve(state.selected);
    state.selected = video || null; state.epoch++; state.needsLayout = true;
    if (video) {
      for (const event of ['play', 'playing', 'timeupdate', 'seeking', 'seeked', 'ratechange', 'pause', 'ended', 'durationchange']) {
        listenVideo(video, event, () => { checkContext(); startDrawing(); });
      }
      listenVideo(video, 'loadedmetadata', () => { checkContext(); requestScan(); position(); startDrawing(); });
      listenVideo(video, 'emptied', () => { if (state.bound?.source) invalidate('视频媒体已重新载入，请确认轨道后重新匹配。'); });
      resizeObserver?.observe(video);
    }
    refreshStatus(); fullscreenChanged(); position(); draw();
  }
  function scan() {
    if (state.destroyed || state.suspended || document.hidden) return;
    state.scanTimer = 0;
    checkContext();
    for (const video of state.discovered) if (!video.isConnected) state.discovered.delete(video);
    const videos = [...state.discovered];
    videos.sort((a, b) => visibleArea(b) - visibleArea(a));
    state.videos = videos;
    if (!state.selected || !videos.includes(state.selected)) selectVideo(videos.find((v) => visibleArea(v) > 0) || null);
    const key = videos.map((v) => `${videoId(v)}:${duration(v)}:${v.videoWidth}x${v.videoHeight}`).join('|');
    if (key !== state.videoListKey) {
      state.videoListKey = key; ui.videoSelect.replaceChildren();
      if (!videos.length) ui.videoSelect.append(el('option', { value: '', text: '尚未发现 HTML5 视频' }));
      videos.forEach((v, i) => ui.videoSelect.append(el('option', { value: videoId(v), text: `视频 ${i + 1} · ${duration(v)} · ${v.videoWidth || Math.round(v.clientWidth)}×${v.videoHeight || Math.round(v.clientHeight)}` })));
    }
    ui.videoSelect.value = state.selected ? String(videoId(state.selected)) : '';
    ui.videoSelect.disabled = !videos.length;
    ui.identity.textContent = readIdentity().label;
    ui.launcher.hidden = !videos.length && !state.track && ui.panel.hidden;
    if (!host.isConnected) document.documentElement.append(host);
    if (state.track && !state.bound) refreshStatus();
    position(); draw();
    if (!canvas.hidden && state.selected && !state.selected.paused) startDrawing();
  }
  function requestScan() {
    if (!state.scanTimer && !state.destroyed && !document.hidden) state.scanTimer = setTimeout(scan, 200);
  }

  function composedContains(parent, node) {
    for (let n = node; n; n = n.parentNode || n.getRootNode?.().host) if (n === parent) return true;
    return false;
  }
  function fullscreenChanged() {
    const full = document.fullscreenElement;
    state.fullscreenUnavailable = false;
    host.classList.toggle('fullscreen', !!full);
    try {
      if (host.matches(':popover-open')) host.hidePopover();
    } catch (_) { /* Popover is optional on older browsers. */ }
    host.removeAttribute('popover');
    if (full === state.selected) {
      // A fullscreen <video> cannot render an ordinary sibling/child overlay.
      // A manual popover joins the top layer above it without replacing the player.
      if (typeof host.showPopover === 'function') {
        document.documentElement.append(host);
        host.setAttribute('popover', 'manual');
        try { host.showPopover(); } catch (_) { state.fullscreenUnavailable = true; }
      } else state.fullscreenUnavailable = true;
    } else if (full && state.selected && composedContains(full, state.selected)) {
      full.append(host);
    } else document.documentElement.append(host);
    if (state.fullscreenUnavailable) notify('浏览器的原生视频全屏暂不能显示叠加层；退出后可使用容器全屏。', 'pending');
    position(); draw();
  }
  function contentBox(video) {
    const r = video.getBoundingClientRect(), css = getComputedStyle(video);
    const sx = video.offsetWidth ? r.width / video.offsetWidth : 1, sy = video.offsetHeight ? r.height / video.offsetHeight : 1;
    const leftInset = (parseFloat(css.borderLeftWidth) + parseFloat(css.paddingLeft) || 0) * sx;
    const topInset = (parseFloat(css.borderTopWidth) + parseFloat(css.paddingTop) || 0) * sy;
    const horizontal = (parseFloat(css.borderLeftWidth) + parseFloat(css.borderRightWidth) + parseFloat(css.paddingLeft) + parseFloat(css.paddingRight) || 0) * sx;
    const vertical = (parseFloat(css.borderTopWidth) + parseFloat(css.borderBottomWidth) + parseFloat(css.paddingTop) + parseFloat(css.paddingBottom) || 0) * sy;
    const box = { left: r.left + leftInset, top: r.top + topInset, width: Math.max(0, r.width - horizontal), height: Math.max(0, r.height - vertical) };
    if (video.videoWidth && video.videoHeight && ['contain', 'scale-down'].includes(css.objectFit)) {
      let scale = Math.min(box.width / video.videoWidth, box.height / video.videoHeight);
      if (css.objectFit === 'scale-down') scale = Math.min(1, scale);
      const w = video.videoWidth * scale, h = video.videoHeight * scale;
      const parts = css.objectPosition.split(/\s+/);
      const pos = (s, free) => s?.endsWith('%') ? parseFloat(s) / 100 * free : s?.endsWith('px') ? parseFloat(s) : s === 'left' || s === 'top' ? 0 : s === 'right' || s === 'bottom' ? free : free / 2;
      box.left += pos(parts[0], box.width - w); box.top += pos(parts[1], box.height - h); box.width = w; box.height = h;
    }
    return box;
  }
  function position() {
    const video = state.selected, full = document.fullscreenElement;
    if (!video?.isConnected || state.fullscreenUnavailable || full && !composedContains(full, video)) { canvas.hidden = ui.badge.hidden = true; return; }
    const box = contentBox(video), parent = host.getBoundingClientRect();
    if (box.width < 1 || box.height < 1 || visibleArea(video) === 0) { canvas.hidden = ui.badge.hidden = true; return; }
    canvas.style.left = `${box.left - parent.left}px`; canvas.style.top = `${box.top - parent.top}px`;
    canvas.style.width = `${box.width}px`; canvas.style.height = `${box.height}px`;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    if (Math.abs(box.width - state.width) > 0.5 || Math.abs(box.height - state.height) > 0.5 || dpr !== state.dpr) {
      state.width = box.width; state.height = box.height; state.dpr = dpr;
      canvas.width = Math.max(1, Math.round(box.width * dpr)); canvas.height = Math.max(1, Math.round(box.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); state.needsLayout = true;
    }
    canvas.hidden = !(state.bound && state.settings.enabled);
    ui.badge.hidden = canvas.hidden;
    ui.badge.style.left = `${Math.max(4, box.left - parent.left + 8)}px`;
    ui.badge.style.top = `${Math.max(4, box.top - parent.top + box.height - 25)}px`;
    if (state.needsLayout) rebuild();
  }
  function rebuild() {
    state.needsLayout = false;
    const targetDuration = expectedDuration();
    state.layout = state.track ? planLanes(state.track.comments, state.width, state.height, { ...state.settings, targetDuration }, (text, size) => { ctx.font = `600 ${size}px ${FONT}`; return ctx.measureText(text).width; }).items : [];
  }
  function clearCanvas() { ctx.clearRect(0, 0, state.width, state.height); canvas.hidden = ui.badge.hidden = true; }
  function draw() {
    ctx.clearRect(0, 0, state.width, state.height);
    if (canvas.hidden || !state.bound || !state.settings.enabled || !state.selected) return;
    const time = state.selected.currentTime || 0;
    ctx.save(); ctx.globalAlpha = state.settings.opacity / 100; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round'; ctx.strokeStyle = 'rgba(0,0,0,.85)'; ctx.shadowColor = 'rgba(0,0,0,.4)'; ctx.shadowBlur = 2;
    for (let i = upperBound(state.layout, time - LIFETIME); i < state.layout.length; i++) {
      const c = state.layout[i]; if (c.at > time) break; if (c.end <= time) continue;
      ctx.font = `600 ${c.font}px ${FONT}`; ctx.lineWidth = Math.max(2.2, c.font / 10); ctx.fillStyle = `#${c.color.toString(16).padStart(6, '0')}`; ctx.textAlign = c.mode === 1 ? 'left' : 'center';
      const x = c.mode === 1 ? state.width - c.speed * (time - c.at) : state.width / 2;
      ctx.strokeText(c.text, x, c.y); ctx.fillText(c.text, x, c.y);
    }
    ctx.restore();
  }
  function frame() {
    state.raf = 0; if (state.destroyed || state.suspended || document.hidden) return;
    checkContext(); position(); draw();
    if (!canvas.hidden && state.bound && state.settings.enabled && state.selected && !state.selected.paused && !state.selected.ended) state.raf = requestAnimationFrame(frame);
  }
  function startDrawing() { if (!state.raf && !state.destroyed && !state.suspended) state.raf = requestAnimationFrame(frame); }

  function requestBytes(url) {
    let abort = () => {};
    const legacy = typeof GM_xmlhttpRequest === 'function' ? GM_xmlhttpRequest : null;
    const modern = typeof GM !== 'undefined' && typeof GM.xmlHttpRequest === 'function' ? GM.xmlHttpRequest.bind(GM) : null;
    const promise = new Promise((resolve, reject) => {
      let settled = false, timer;
      const finish = (err, value) => { if (settled) return; settled = true; clearTimeout(timer); err ? reject(err) : resolve(value); };
      if (legacy || modern) {
        let handle;
        const options = { method: 'GET', url, anonymous: true, timeout: 30000, responseType: 'arraybuffer',
          headers: { Accept: 'application/json, application/xml, text/xml, text/plain;q=0.8' },
          onload: (r) => {
            if (!(r.status >= 200 && r.status < 300)) { finish(new Error(`来源返回 HTTP ${r.status}。`)); return; }
            try {
              const bytes = typeof r.response === 'string' ? new TextEncoder().encode(r.response) : new Uint8Array(r.response);
              if (!bytes.byteLength) throw new Error('来源没有返回文件内容。');
              if (bytes.byteLength > MAX_BYTES) throw new Error('文件超过当前客户端的 32 MiB 单轨容量。');
              finish(null, { bytes, url: httpUrl(r.finalUrl || url), transport: '脚本管理器' });
            } catch (e) { finish(e); }
          },
          onprogress: (e) => { if (e.loaded > MAX_BYTES) { finish(new Error('文件超过当前客户端的 32 MiB 单轨容量。')); handle?.abort(); } },
          onerror: () => finish(new Error('脚本管理器请求失败；请检查连接授权与来源可达性。')),
          ontimeout: () => finish(new Error('读取来源超时。')), onabort: () => finish(new Error('读取已取消。')),
        };
        try {
          handle = (legacy || modern)(options);
          if (!legacy && handle?.then) handle.then(options.onload).catch(options.onerror);
          abort = () => { handle?.abort(); finish(new Error('读取已取消。')); };
        } catch (_) { finish(new Error('脚本管理器无法发起此请求。')); }
      } else {
        const controller = new AbortController(); abort = () => { controller.abort(); finish(new Error('读取已取消。')); };
        fetch(url, { method: 'GET', credentials: 'omit', referrerPolicy: 'no-referrer', mode: 'cors', signal: controller.signal })
          .then(async (r) => {
            if (!r.ok) throw new Error(`来源返回 HTTP ${r.status}。`);
            if (Number(r.headers.get('content-length')) > MAX_BYTES) throw new Error('文件超过当前客户端的 32 MiB 单轨容量。');
            const bytes = new Uint8Array(await r.arrayBuffer());
            if (bytes.byteLength > MAX_BYTES) throw new Error('文件超过当前客户端的 32 MiB 单轨容量。');
            finish(null, { bytes, url: httpUrl(r.url || url), transport: '浏览器 fetch' });
          }).catch((e) => finish(new Error(e.message?.startsWith('来源返回') || e.message?.startsWith('文件超过') ? e.message : '浏览器读取失败；请检查跨域、本地网络或混合内容限制，或启用脚本管理器的 GM 请求。')));
      }
      if (!settled) timer = setTimeout(() => { finish(new Error('读取来源超时。')); abort(); }, 30000);
    });
    return { promise, abort: () => abort() };
  }
  async function verifyEntry(bytes, entry) {
    if (!entry) return '';
    if (entry.bytes !== null && entry.bytes !== bytes.byteLength) throw new Error('轨道大小与目录记录不一致，请刷新目录。');
    if (!entry.sha256) return '';
    if (!globalThis.crypto?.subtle) return '当前页面未提供 SHA-256 校验能力';
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const actual = [...new Uint8Array(digest)].map((n) => n.toString(16).padStart(2, '0')).join('');
    if (actual !== entry.sha256) throw new Error('轨道 SHA-256 与目录不一致，请刷新目录。');
    return 'SHA-256 已核对';
  }
  function installPayload(payload, source, startEpoch, startContext, entry, integrity = '') {
    if (payload.kind === 'catalog') {
      state.catalog = catalogEntries(payload.data, source.url);
      ui.catalogSelect.replaceChildren(...state.catalog.map((item, index) => el('option', { value: index, text: item.label })));
      ui.catalog.hidden = false;
      notify(`已读取 ${state.catalog.length} 条目录记录，请选择并载入轨道。`, 'ready'); return;
    }
    checkContext();
    state.track = payload.track; state.source = source; state.bound = null;
    if (!state.track.bindings.length && entry?.bindings?.length) state.track.bindings = entry.bindings;
    if (entry?.synthetic) state.track.synthetic = true;
    state.settings.scale = state.track.timeline.scale; state.settings.offset = state.track.timeline.offset;
    ui.scale.value = String(state.settings.scale); ui.offset.value = String(state.settings.offset);
    ui.track.hidden = false; ui.trackLabel.textContent = state.track.label;
    ui.trackType.textContent = state.track.synthetic ? '合成弹幕' : '导入弹幕'; ui.badge.textContent = ui.trackType.textContent;
    ui.trackInfo.textContent = `${state.track.comments.length} 条 · ${payload.format}${state.track.unsupported ? ` · 略过 ${state.track.unsupported} 条特殊模式` : ''}${state.track.rejected ? ` · ${state.track.rejected} 条无效记录` : ''}${integrity ? ` · ${integrity}` : ''}`;
    ui.source.textContent = publicSource(source);
    ui.footer.textContent = `${source.transport ? `读取：${source.transport} · ` : ''}附加弹幕层 · 原站播放保持原样`;
    state.needsLayout = true;
    if (startEpoch !== state.epoch || !sameContext(startContext, fingerprint())) notify('读取期间视频已变化；轨道已载入，请重新确认匹配。', 'pending');
    else applyTrack(false);
    position(); draw(); refreshStatus();
  }
  async function loadRemote(event, fromCatalog = false) {
    if (!trusted(event)) return;
    let url, entry;
    try { entry = fromCatalog ? state.catalog[Number(ui.catalogSelect.value)] : null; url = httpUrl(entry ? entry.url : ui.url.value.trim()); }
    catch (e) { notify(e.message, 'error'); return; }
    state.request?.abort(); const serial = ++state.serial, epoch = state.epoch, context = fingerprint();
    state.busy = true; state.reason = ''; refreshStatus();
    try {
      state.request = requestBytes(url);
      const result = await state.request.promise;
      if (serial !== state.serial) return;
      const integrity = await verifyEntry(result.bytes, entry);
      if (serial !== state.serial) return;
      const payload = parsePayload(result.bytes);
      state.busy = false;
      installPayload(payload, { kind: 'url', url: result.url, transport: result.transport }, epoch, context, entry, integrity);
    } catch (e) { if (serial === state.serial) notify(e.message || '读取失败。', 'error'); }
    finally { if (serial === state.serial) { state.busy = false; state.request = null; refreshStatus(); } }
  }
  async function loadFile(event) {
    // Browser automation and some picker integrations synthesize change events.
    // The preceding real click arms one import; no path or file is read before it.
    if (!state.filePickArmed) return;
    state.filePickArmed = false;
    const file = ui.file.files?.[0]; if (!file) return;
    state.request?.abort(); const serial = ++state.serial, epoch = state.epoch, context = fingerprint();
    state.busy = true; state.reason = ''; refreshStatus();
    try {
      if (file.size > MAX_BYTES) throw new Error('文件超过当前客户端的 32 MiB 单轨容量。');
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (serial !== state.serial) return;
      state.busy = false;
      installPayload(parsePayload(bytes), { kind: 'file', name: file.name }, epoch, context);
    } catch (e) { if (serial === state.serial) notify(e.message || '文件读取失败。', 'error'); }
    finally { if (serial === state.serial) { state.busy = false; refreshStatus(); } ui.file.value = ''; }
  }
  function timingChanged() {
    const offset = finite(ui.offset.value), scale = finite(ui.scale.value);
    if (offset === null || scale === null || scale <= 0) { notify('时间倍率须为正数，偏移须为有限秒数。', 'error'); return; }
    state.settings.offset = offset; state.settings.scale = scale; rebuild(); position(); draw();
    if (state.bound) { state.reason = ''; refreshStatus(); }
  }

  ui.launcher.addEventListener('click', () => panel(ui.panel.hidden));
  ui.close.addEventListener('click', () => panel(false));
  ui.rescan.addEventListener('click', () => { discover(); scan(); });
  ui.videoSelect.addEventListener('change', () => { selectVideo(state.videos.find((v) => String(videoId(v)) === ui.videoSelect.value), true); scan(); });
  ui.fileButton.addEventListener('click', (e) => { if (trusted(e)) { state.filePickArmed = true; ui.file.click(); } });
  ui.file.addEventListener('change', loadFile);
  ui.file.addEventListener('cancel', () => { state.filePickArmed = false; });
  ui.remote.addEventListener('click', (e) => loadRemote(e));
  ui.url.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.isTrusted) { e.preventDefault(); loadRemote(e); } });
  ui.catalogLoad.addEventListener('click', (e) => loadRemote(e, true));
  ui.bind.addEventListener('click', (e) => { if (trusted(e)) applyTrack(true); });
  ui.enabled.addEventListener('change', () => { state.settings.enabled = ui.enabled.checked; checkContext(); refreshStatus(); position(); startDrawing(); });
  ui.offset.addEventListener('change', timingChanged); ui.scale.addEventListener('change', timingChanged);
  ui.earlier.addEventListener('click', (e) => { e.preventDefault(); ui.offset.value = String(Math.round((finite(ui.offset.value) || 0) * 10 - 5) / 10); timingChanged(); });
  ui.later.addEventListener('click', (e) => { e.preventDefault(); ui.offset.value = String(Math.round((finite(ui.offset.value) || 0) * 10 + 5) / 10); timingChanged(); });
  for (const key of ['density', 'opacity', 'fontSize']) ui[key].addEventListener('input', () => {
    state.settings[key] = Number(ui[key].value); ui[`${key}Value`].textContent = ui[key].value + (key === 'fontSize' ? '' : '%'); rebuild(); position(); draw();
  });
  ui.area.addEventListener('change', () => { state.settings.area = Number(ui.area.value); rebuild(); position(); draw(); });
  ui.fullscreen.addEventListener('click', async (e) => {
    if (!trusted(e) || !state.selected) return;
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else {
        panel(false);
        const target = typeof host.showPopover === 'function' ? state.selected : state.selected.parentElement;
        await target.requestFullscreen();
      }
    } catch (_) { notify('当前页面未允许全屏；仍可在原播放器中观看。', 'pending'); }
  });
  const menu = typeof GM_registerMenuCommand === 'function' ? GM_registerMenuCommand : typeof GM !== 'undefined' && typeof GM.registerMenuCommand === 'function' ? GM.registerMenuCommand.bind(GM) : null;
  if (menu) menu('打开弹幕模拟器', () => panel(true));
  document.addEventListener('fullscreenchange', fullscreenChanged);
  document.addEventListener('scroll', () => { position(); draw(); startDrawing(); }, { passive: true, capture: true });
  window.addEventListener('resize', () => { position(); draw(); startDrawing(); });
  window.addEventListener('popstate', requestScan); window.addEventListener('hashchange', requestScan);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { scan(); startDrawing(); } });
  const observer = new MutationObserver((records) => {
    let changed = false;
    for (const record of records) {
      if (record.type === 'attributes') {
        if (record.target.tagName === 'VIDEO' || record.target.tagName === 'SOURCE') changed = true;
        continue;
      }
      if (record.target.tagName === 'VIDEO') changed = true;
      for (const node of record.addedNodes) if (node.nodeType === 1 && node !== host) changed = discover(node) || changed;
      if (record.removedNodes.length && state.selected && !state.selected.isConnected) changed = true;
    }
    if (changed) requestScan();
  });
  watchRoot(document.documentElement);
  // No repeated document-wide walk: discovery is incremental after initial load.
  const interval = setInterval(() => { if (state.selected || state.discovered.size) scan(); }, 1200);
  window.addEventListener('pagehide', (event) => {
    if (event.persisted) {
      state.suspended = true;
      if (state.raf) cancelAnimationFrame(state.raf);
      state.raf = 0; clearTimeout(state.scanTimer); state.scanTimer = 0;
      state.serial++; state.request?.abort(); state.request = null; state.busy = false;
      return;
    }
    state.destroyed = true; clearInterval(interval); clearTimeout(state.scanTimer); clearTimeout(state.toastTimer);
    if (state.raf) cancelAnimationFrame(state.raf);
    state.request?.abort(); state.listeners.forEach((remove) => remove()); observer.disconnect(); resizeObserver?.disconnect();
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && !state.destroyed) { state.suspended = false; scan(); refreshStatus(); startDrawing(); }
  });
  discover(); scan(); refreshStatus();
})();
