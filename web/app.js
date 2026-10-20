'use strict';

/* 无障碍助手页面：一份代码，两种布局。
   - 手机布局（默认）：内容 / 历史 / 设置 三个 Tab + 遥控截图大面板。
   - PC 控制台布局（/?pc=1，主窗口里的 WebView2 加载）：左 = 连接/状态/日志/窗口操作，
     右 = 设置。设置区两种布局共用同一个渲染器（renderSettings），改任何一边都通过
     服务端实时同步到另一边（统一设置存 config.json 的 settings 段）。
   旧版中转服务器不支持设置同步时，手机自动退回本地设置模式（照常可用）。 */

/* ---------- 工具 ---------- */
const $ = s => document.querySelector(s);
const QUERY = new URLSearchParams(location.search);
const IS_PC = QUERY.has('pc');
// 悬浮窗（/?overlay=1）：复用手机页的内容处理与结果渲染，去掉遥控截图、
// 图片预览和声音反馈；正文整窗鼠标穿透，由原生工具条发指令（window.overlayAction）。
const IS_OVERLAY = QUERY.has('overlay');

const DEFAULT_PROMPT = `你是专业的无障碍辅助助手。用户会发来一处或多处屏幕内容（文字或截图）。
你只负责给出准确的识别结果与选择建议，不要在结果里讲解你的推理过程。

只输出下面这一个 JSON，前后不要有任何其它文字：不要 Markdown 代码围栏，不要"好的"，
不要复述内容，不要写"我先看看""让我分析一下"这类思考过程或说明。
- 只有一条内容：{"answer":"选择建议","analysis":"30字以内简短解析"}
- 有多条内容（按图中从上到下的顺序）：[{"no":"序号","answer":"选择建议","analysis":"30字以内简短解析"}, ...]
  no 字段每项都必须有：图中印有编号就用原编号；没有印编号就按出现顺序从1开始编号。不要遗漏或合并任何一条。

answer 只填建议本身，不要写理由、不要抄选项内容：
- 单选：一个选项字母，如 A
- 多选：把所有正确选项的字母连在一起、按字母升序写，如 ACD。
  多选必须给全，不要只写第一个，也不要用逗号、空格、"和"分隔
- 判断：正确 或 错误
- 其他类型：直接给最简洁、可直接使用的文本

analysis 只写结论性的一句话，不要写完整推导过程。
如果内容看不清或信息不足，answer 填 无法确定。`;

// 本地兜底默认（与服务端 SETTINGS_DEFAULTS 同一份：断连/旧中转时才用它）
const DEF_SETTINGS = {
  channels: [],
  prompt: DEFAULT_PROMPT,
  autoAsk: true,
  includeImage: true,
  autoScroll: true,
  tts: true,
  ttsAnalysis: false,
  bdPhonetic: false,
  vib: true,
  vibStrength: 1,     // 0 弱 / 1 中 / 2 强（只有配套 App 能调强度，浏览器没有强度 API）
  vibPulseGap: 260,   // 同一选项内，每下之间隔多久（毫秒）
  vibOptGap: 650,     // 多选时，选项与选项之间隔多久（毫秒）
  vibQuesGap: 800,    // 多条内容之间隔多久（毫秒）
  overlayHover: 0.7,  // 悬浮窗按钮悬停触发/拖动静止退出（秒）
  room: ''
};

function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem('qa_settings_v1'));
    if (s && typeof s === 'object') {
      const st = Object.assign({}, DEF_SETTINGS, s);
      st.channels = normalizeChannels(st.channels);
      return st;
    }
  } catch (e) {}
  return JSON.parse(JSON.stringify(DEF_SETTINGS));
}
// 渠道兜底（与服务端 normalize_channels 同一套判据）：非字典项剔除、缺字段补默认。
// 空渠道卡要保留——「添加渠道」先插一张空卡再让用户填。
function normalizeChannels(chans) {
  const out = [];
  (Array.isArray(chans) ? chans : []).forEach(c => {
    if (!c || typeof c !== 'object') return;
    out.push({
      name: String(c.name || ''),
      api: c.api || 'chat',
      base: String(c.base || ''),
      key: String(c.key || ''),
      model: String(c.model || ''),
      timeout: parseInt(c.timeout, 10) || 20,
      enabled: c.enabled === undefined ? true : !!c.enabled,
    });
  });
  return out;
}
function saveSettings() { localStorage.setItem('qa_settings_v1', JSON.stringify(settings)); }
function loadHistory() {
  try { return JSON.parse(localStorage.getItem('qa_history_v1')) || []; } catch (e) { return []; }
}
function saveHistory() { localStorage.setItem('qa_history_v1', JSON.stringify(history.slice(0, 30))); }

function fmtTime(ts) {
  const d = new Date(ts || Date.now());
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
let toastT = 0;
function toast(msg, ms = 2200) {
  const el = $('#toast');
  el.textContent = msg; el.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { el.hidden = true; }, ms);
}
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext !== false) {
    navigator.clipboard.writeText(text).then(() => toast('已复制'), () => fallbackCopy(text));
  } else fallbackCopy(text);
}
function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); toast('已复制'); } catch (e) { toast('复制失败，请长按手动复制'); }
  document.body.removeChild(ta);
}
// 输入框自动保存：打字时只改内存，停手 600ms 或失焦时才落盘并广播。
// 不这样做的话，渠道地址/密钥每敲一个字符都会写一次 config.json，日志刷屏。
const pendingSaves = new Set();
function debounce(fn, ms = 600) {
  let timer = 0;
  const run = () => { timer = 0; pendingSaves.delete(wrapped); fn(); };
  const wrapped = () => {
    clearTimeout(timer);
    pendingSaves.add(wrapped);
    timer = setTimeout(run, ms);
  };
  wrapped.flush = () => { if (timer) { clearTimeout(timer); run(); } };
  return wrapped;
}
// 窗口被隐藏/切走时立刻落盘，别把最后几个字符留在 600ms 的等待里。
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') pendingSaves.forEach(f => f.flush());
});
function ico(id, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('aria-hidden', 'true');
  if (cls) svg.setAttribute('class', cls); else svg.setAttribute('class', 'ic');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + id);
  svg.appendChild(use);
  return svg;
}

/* ---------- 状态 ---------- */
let settings = loadSettings();
let history = loadHistory();
let ws = null, retryT = 0, hbT = 0, backoffMs = 500, lastMsgAt = 0;
let seenIds = [], currentItem = null, curAsk = null, pcOnline = true, curView = 'main';
let scrollAnim = null;
let serverSynced = false;   // 服务端统一设置已确认（旧中转下保持 false → 纯本地模式）
let curStatus = null;       // 最近一次服务端状态快照
let setRerenderPending = false;
// 悬浮窗初始是隐藏的：打开后才开始自动识别，关闭时停下，避免在后台空耗接口。
let overlayPaused = IS_OVERLAY;
let answeredId = null;
let askingId = null;   // 正在识别的条目 id：防止显示/重同步时对同一条重复发请求
let resumeT = 0;

/* ---------- 连接 ---------- */
function currentRoom() {
  return new URLSearchParams(location.search).get('room') || settings.room || '';
}
function wsUrl() {
  if (IS_PC) return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/panel';
  if (IS_OVERLAY) return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/overlay';
  const room = currentRoom();
  const base = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
  return room ? base + '/ws/phone?room=' + encodeURIComponent(room) : base + '/ws';
}
function setStatus() {
  if (IS_OVERLAY) return;   // 悬浮页不显示连接状态；断线时结果区会给出失败信息
  if (IS_PC) {
    const dot = $('#pc-dot'), txt = $('#pc-head-txt');
    if (!ws || ws.readyState > 1) { dot.className = 'dot offline'; txt.textContent = '服务已断开'; }
    else if (ws.readyState === 0) { dot.className = 'dot connecting'; txt.textContent = '正在连接'; }
    else { dot.className = 'dot online'; txt.textContent = '服务运行中'; }
    return;
  }
  const dot = $('#dot'), txt = $('#conn-text');
  if (!ws || ws.readyState > 1) {
    dot.className = 'dot offline'; txt.textContent = '连接断开，自动重连中…';
  } else if (ws.readyState === 0) {
    dot.className = 'dot connecting'; txt.textContent = '连接中…';
  } else if (!pcOnline) {
    dot.className = 'dot degraded'; txt.textContent = '中转已连，电脑离线';
  } else {
    dot.className = 'dot online'; txt.textContent = '已连接';
  }
}
function connect() {
  clearTimeout(retryT);
  setStatus();
  if (ws) { ws.onclose = null; ws.onerror = null; try { ws.close(); } catch (e) {} }
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    backoffMs = 500; lastMsgAt = Date.now();
    if (!IS_PC && !IS_OVERLAY) pcOnline = true;
    setStatus();
    send({ type: 'resync' });
    // 悬浮订阅在连上时已由服务端直接推一份设置
    if (!IS_OVERLAY) send({ type: 'get_settings' });
  };
  ws.onmessage = ev => {
    lastMsgAt = Date.now();
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    handle(m);
  };
  ws.onclose = () => { setStatus(); retryT = setTimeout(connect, backoffMs); backoffMs = Math.min(backoffMs * 2, 5000); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function startHeartbeat() {
  clearInterval(hbT);
  hbT = setInterval(() => {
    if (ws && ws.readyState === 1) {
      send({ type: 'ping', t: Date.now() });
      if (Date.now() - lastMsgAt > 25000) { try { ws.close(); } catch (e) {} }
    }
  }, 10000);
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (!ws || ws.readyState > 1) connect();
    else { send({ type: 'resync' }); send({ type: 'get_settings' }); }
  }
});

/* ---------- 遥控截图：点按钮让电脑立即全屏截图（手机布局） ---------- */
let snapWaitT = 0;
// 悬浮窗「识别」= 截屏+识别：按一下先截屏，内容推回来时即使关了自动识别也强制识别一次。
let askAfterSnap = false;
const SNAP_TITLE = '点击按钮，遥控电脑立即截屏';
function setSnapWaitUi(waiting) {
  $('#snap-dock').classList.toggle('waiting', waiting);
  $('#snap-title').textContent = waiting ? '已通知电脑，正在截屏…' : SNAP_TITLE;
  $('#snap-sub').textContent = waiting ? '截好后会自动传过来，稍候片刻' : '也可在电脑上按快捷键';
}
function clearSnapWait() {
  if (!snapWaitT) return;
  clearTimeout(snapWaitT);
  snapWaitT = 0;
  setSnapWaitUi(false);
}
function requestSnap() {
  if (snapWaitT) { toast('已请求过，正在等电脑截图…', 1500); return; }
  setSnapWaitUi(true);
  // 悬浮页看不到底部面板，给一条可见反馈；成功后新内容会直接把提示顶掉。
  if (IS_OVERLAY) toast('已通知电脑截屏，截好后自动识别…', 1600);
  snapWaitT = setTimeout(() => {
    snapWaitT = 0;
    askAfterSnap = false;
    setSnapWaitUi(false);
    toast('没等到电脑端的截图，请检查连接后重试');
  }, 10000);
  if (ws && ws.readyState === 1) { send({ type: 'snap' }); return; }
  fetch('/api/snap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ room: currentRoom() }),
  })
    .then(r => r.json())
    .then(j => { if (!j.ok) throw new Error(j.error || '电脑端未响应'); })
    .catch(err => {
      askAfterSnap = false;
      if (!snapWaitT) return;
      clearSnapWait();
      const m = errMsg(err);
      toast('请求失败：' + (/fetch/i.test(m) ? '电脑端不可达' : m));
    });
}

/* ---------- 消息处理 ---------- */
function handle(m) {
  switch (m.type) {
    case 'sync': (m.items || []).forEach(showQuestion); break;
    case 'question': showQuestion(m); break;
    case 'pong': break;
    case 'pc_status': if (!IS_PC) { pcOnline = !!m.online; setStatus(); } break;
    case 'snap_ack':
      if (snapWaitT && !m.ok) {
        askAfterSnap = false;
        clearSnapWait();
        toast(m.error || '电脑端没有执行截图');
      }
      break;
    case 'settings': applyServerSettings(m); break;
    case 'status': applyStatus(m); break;
    case 'log': if (IS_PC) appendLog(m); break;
  }
}

/* ---------- 统一设置：服务端为权威，两端实时同步 ---------- */
function hasLocalConfig() {
  return (settings.channels || []).some(c => c.base && c.model);
}
function applyServerSettings(m) {
  serverSynced = true;
  if (m.empty && !IS_PC && !IS_OVERLAY && !applyServerSettings._seeded && hasLocalConfig()) {
    // 服务端还没有设置而本地有（老用户迁移）：把本地设置播种上去，广播回来再统一渲染。
    // 只播一次：之后用户在另一端清空渠道等操作不该被手机再种回来。
    applyServerSettings._seeded = true;
    send({ type: 'settings_set', patch: settings });
    return;
  }
  if (!m.data || typeof m.data !== 'object') return;
  const merged = Object.assign({}, settings, m.data);
  merged.room = settings.room;   // room 是本机连接信息，不跟随服务端
  if (JSON.stringify(merged) === JSON.stringify(settings)) return;
  settings = merged;
  saveSettings();
  requestSettingsRerender();
}
function setSetting(key, value) {
  settings[key] = value;
  saveSettings();
  if (serverSynced || IS_PC) send({ type: 'settings_set', patch: { [key]: value } });
}
function isEditingSettings() {
  const el = document.activeElement;
  return !!(el && el.closest && el.closest('.set-wrap') &&
            /INPUT|TEXTAREA|SELECT/.test(el.tagName));
}
function requestSettingsRerender() {
  if (IS_OVERLAY) return;   // 悬浮页没有设置界面，只需要同步后的 settings 对象
  if (isEditingSettings()) { setRerenderPending = true; return; }
  renderSettingsAll();
}

/* ---------- 状态快照渲染（两种布局共用数据） ---------- */
function applyStatus(s) {
  if (IS_OVERLAY) return;
  curStatus = s || {};
  if (IS_PC) renderPcStatus(curStatus);
  else updatePhonePcSection(curStatus);
  setStatus();
}

function updatePhonePcSection(s) {
  const el = $('#pc-status-val');
  if (!el) return;
  const bits = [];
  if (s.ver) bits.push(s.ver);
  if (s.mode) bits.push(s.mode === 'relay' ? '中转' : '局域网');
  if (s.phones !== undefined) bits.push(`在线手机 ${s.phones} 台`);
  if (s.ocr_detail) bits.push(s.ocr_detail);
  el.textContent = bits.join(' · ') || '电脑端状态读取中…';
  const dot = $('#pc-status-dot');
  if (dot) dot.className = 'dot ' + (pcOnline ? 'online' : 'degraded');
  setRtSwitch('#rt-corner', !!s.corner_snap);
  setRtSwitch('#rt-swipe', !!s.swipe_snap);
  setRtSwitch('#rt-stealth', !!s.anti_capture);
  setRtSwitch('#rt-ocr', !!s.ocr_enabled);
  const pop = $('#stealth-pop');
  if (pop) pop.textContent = s.anti_capture_detail || pop.textContent;
}

function setRtSwitch(sel, on) {
  const el = $(sel);
  if (el && !el.checked !== !on) el.checked = !!on;   // 只在真变时动，别打断用户点击
}

/* ---------- PC 控制台：左栏渲染 ---------- */
function renderPcStatus(s) {
  $('#pc-ver').textContent = s.ver || '';
  $('#pc-phones').textContent = s.phones !== undefined ? `已连 ${s.phones} 台` : '';
  // 连接卡：二维码 + 地址 + 网卡点选（地址没变就别重载二维码）
  const t = s.targets || [];
  const pick = Math.max(0, Math.min(s.pick || 0, Math.max(0, t.length - 1)));
  const connKey = JSON.stringify([t, pick]);
  if (connKey !== renderPcStatus._key) {
    renderPcStatus._key = connKey;
    $('#pc-qr').src = '/api/qr?v=' + Date.now();
    $('#pc-url').textContent = (t[pick] || {}).url || '';
    const networks = $('#pc-networks');
    const focusedPick = networks.contains(document.activeElement) ? document.activeElement.dataset.pick : null;
    networks.replaceChildren();
    t.forEach((it, i) => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'network-option';
      button.dataset.pick = i;
      button.textContent = it.name || `网卡 ${i + 1}`;
      button.title = button.textContent + ' · ' + it.url;
      button.setAttribute('aria-pressed', String(i === pick));
      networks.appendChild(button);
    });
    if (!t.length) networks.textContent = '暂无可用网卡';
    if (focusedPick !== null && networks.children[focusedPick]) networks.children[focusedPick].focus({ preventScroll: true });
  }
  // 状态键值
  const kv = $('#pc-kv');
  kv.innerHTML = '';
  const hk = s.hotkeys || {};
  const row = (k, v) => {
    const kd = document.createElement('div'); kd.className = 'k'; kd.textContent = k;
    const vd = document.createElement('div'); vd.className = 'v';
    if (v instanceof Node) vd.appendChild(v); else vd.textContent = v;
    kv.append(kd, vd);
  };
  const hotkeyText = key => String(key).split('+').map(k =>
    k.length > 1 && !/^f\d+$/i.test(k) ? k.charAt(0).toUpperCase() + k.slice(1).toLowerCase() : k.toUpperCase()).join('+');
  const kbd = t2 => { const s2 = document.createElement('kbd'); s2.className = 'kbd'; s2.textContent = hotkeyText(t2); return s2; };
  const hotLines = document.createElement('div');
  hotLines.className = 'hotkey-lines';
  [
    [[hk.capture || 'F8', '全屏截图'], [hk.calibrate || 'F9', '设定截图区域'], [hk.region || 'F10', '区域截图']],
    [[hk.window || 'Ctrl+Alt+H', '显示/隐藏'], [hk.quit || 'Ctrl+Alt+Q', '退出程序']],
  ].forEach(items => {
    const line = document.createElement('div'); line.className = 'hotkey-line';
    items.forEach(([key, label]) => {
      const item = document.createElement('span'); item.className = 'hotkey-item';
      item.append(kbd(key), ' ' + label); line.appendChild(item);
    });
    hotLines.appendChild(line);
  });
  const caution = document.createElement('p');
  caution.className = 'hotkey-caution';
  caution.textContent = '*部分软件会记录键盘输入，慎重选择交互方式';
  hotLines.appendChild(caution);
  row('快捷键', hotLines);
  row('截图区域', s.region_detail || '');
  row('屏幕', `${s.monitors || ''}${s.capture_monitor ? ' · 截图取「' + s.capture_monitor + '」' : ''}`);
  const captureStatus = document.createElement('span');
  captureStatus.className = 'pc-cap-status';
  captureStatus.textContent = s.anti_capture_detail || '';
  captureStatus.title = captureStatus.textContent;
  row('防截屏', captureStatus);
  const checks = document.createElement('span');
  (s.checks || []).forEach(c => {
    const m = document.createElement('span');
    m.className = c.ok === true ? 'ok' : (c.ok === false ? 'bad' : '');
    m.textContent = (c.ok === true ? '✓ ' : c.ok === false ? '! ' : '· ') + c.label + '  ';
    checks.appendChild(m);
  });
  row('启动自检', checks);
  // 远端开关的显示态（与手机页共用逻辑）
  setRtSwitch('#rt-corner', !!s.corner_snap);
  setRtSwitch('#rt-swipe', !!s.swipe_snap);
  setRtSwitch('#rt-stealth', !!s.anti_capture);
  setRtSwitch('#rt-ocr', !!s.ocr_enabled);
  const pop = $('#stealth-pop');
  if (pop) pop.textContent = s.anti_capture_detail || pop.textContent;
  const oc = $('#ocr-pop');
  if (oc && s.ocr_detail) oc.textContent = '当前：' + s.ocr_detail;
  renderOverlayButton(s.overlay);
}

// 「悬浮窗」开关：状态来自服务端快照（按钮、悬浮窗自身关闭都会广播）。
function renderOverlayButton(state) {
  const button = $('#btn-pc-overlay');
  if (!button) return;
  const visible = !!(state && state.visible);
  button.setAttribute('aria-pressed', String(visible));
  button.disabled = !!(state && state.available === false);
  button.title = state && state.error ? state.error
    : (visible ? '点击隐藏悬浮窗' : '点击显示悬浮窗');
}

/* ---------- PC 控制台：日志 ---------- */
let logLines = 0;
function appendLog(m) {
  const box = $('#pc-log');
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const lines = String(m.text || '').split('\n');
  lines.forEach((ln, i) => {
    const d = document.createElement('div');
    const cls = m.mark === '!' ? 'warn' : m.mark === 'x' ? 'err' : '';
    d.className = cls ? cls : (i ? 'ln cont' : 'ln');
    if (!i) {
      const ts = document.createElement('span'); ts.className = 'ts'; ts.textContent = m.ts || '';
      const mk = document.createElement('span'); mk.className = 'mk'; mk.textContent = m.mark || '·';
      if (cls) d.className = 'ln ' + cls;
      d.append(mk, ts, document.createTextNode(ln));
    } else {
      d.textContent = '    ' + ln;
    }
    box.appendChild(d);
  });
  logLines += lines.length;
  if (logLines > 600) {   // 上限：删掉最老的，保持滚动轻快
    while (box.children.length > 500) box.removeChild(box.firstChild);
    logLines = box.children.length;
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

/* ---------- 内容视图（手机布局） ---------- */
function showQuestion(item) {
  if (!item || !item.id) return;
  if (seenIds.includes(item.id)) return;
  seenIds.push(item.id);
  if (seenIds.length > 200) seenIds.splice(0, 100);

  const forced = askAfterSnap;   // 「识别」按下的那次截屏回来了：这一条由悬浮窗强制发起识别
  askAfterSnap = false;
  clearSnapWait();
  currentItem = item;
  answeredId = null;
  histUpsert(item.id, { id: item.id, ts: item.ts, text: (item.text || '').trim(), answer: '', analysis: '' });

  stopBounceScroll();
  window.scrollTo(0, 0);
  $('#fit-note').hidden = true;
  $('#empty-state').hidden = true;
  $('#q-card').hidden = false;
  $('#q-time').textContent = fmtTime(item.ts);

  const wrap = $('#q-images');
  wrap.innerHTML = '';
  // 悬浮窗只保留紧凑的文字结果：不创建图片元素，也就不会触发预览/大图加载
  if (item.image && !IS_OVERLAY) {
    const img = new Image();
    img.src = item.image;
    img.onclick = () => openLightbox(item.image);
    wrap.appendChild(img);
    $('#btn-save').disabled = false;
  } else {
    $('#btn-save').disabled = true;
  }
  const t = (item.text || '').trim();
  $('#q-text').textContent = t || (item.image ? '（本条无文字，请看图片）' : '（空内容）');
  const note = item.ocr_note || '';
  $('#q-note').textContent = note;
  $('#q-note').hidden = !note;

  const waiting = IS_OVERLAY && overlayPaused;
  const willAsk = settings.autoAsk || forced;
  setAnsBusy(waiting ? '已收到内容，打开悬浮窗后识别'
    : '已收到内容' + (willAsk ? '，正在识别…' : '（自动识别已关闭）'));
  $('#btn-re-ask').hidden = true;
  if (curView !== 'main') switchView('main');
  if (willAsk && !waiting) askLLM(item);
}

/* ---------- 识别（多渠道自动回退） ---------- */
function setAnsBusy(msg) {
  const el = $('#ans-status');
  el.className = 'busy'; el.textContent = msg; el.hidden = false;
  $('#ans-answer').hidden = true; $('#ans-list').hidden = true;
  $('#ans-list').innerHTML = '';
  $('#ans-analysis').hidden = true; $('#ans-meta').hidden = true;
}
function setAnsError(msg, canRetry) {
  askingId = null;
  const el = $('#ans-status');
  el.className = 'error'; el.textContent = msg; el.hidden = false;
  $('#ans-answer').hidden = true; $('#ans-list').hidden = true;
  $('#ans-list').innerHTML = '';
  $('#ans-analysis').hidden = true; $('#ans-meta').hidden = true;
  $('#btn-re-ask').hidden = !canRetry;
}

function isUncertain(parsed) {
  return !!parsed && parsed.length === 1
    && String(parsed[0].answer || '').trim() === '无法确定';
}

async function askLLM(item, manual) {
  if (!item) return;
  if (IS_OVERLAY && overlayPaused) { setAnsBusy('悬浮窗已隐藏，显示后继续'); return; }
  if (curAsk) { try { curAsk.abort(); } catch (e) {} }
  const myId = item.id;
  askingId = myId;
  const qtext = (item.text || '').trim();
  if (!qtext && !item.image) {
    setAnsError('内容为空：电脑端未识别到文字且没有图片。', true);
    return;
  }
  const chans = (settings.channels || []).filter(c => c.enabled && c.base && c.model);
  if (!chans.length) { setAnsError('未配置可用的 API 渠道，请在「设置」中填写（地址/模型名）。', false); return; }

  // OCR 没识别到文字时，即使关闭了"附带截图"也自动改用图片识别，避免拿空内容去识别
  const forceImage = !qtext && !!item.image;
  const useImg = settings.includeImage || forceImage;
  const forcedNote = forceImage && !settings.includeImage ? 'OCR无文字，已自动附带图片' : '';

  const trail = [];
  for (const c of chans) {
    if (currentItem && currentItem.id !== myId) return; // 已有新内容，放弃本次
    if (IS_OVERLAY && overlayPaused) return;            // 悬浮窗已关闭：不再继续消耗接口
    setAnsBusy('正在识别：' + (c.name || c.model) + ' …' + (trail.length ? '\n' + trail.join('\n') : ''));

    const variants = [];
    if (item.image && useImg) variants.push(true);
    variants.push(false);

    for (const useImage of variants) {
      if (!useImage && !qtext && variants.length > 1) continue;
      try {
        let parsed = await attempt(c, item, useImage, myId, manual);
        if (currentItem && currentItem.id !== myId) return;
        if (IS_OVERLAY && overlayPaused) return;
        // 图片类内容第一次结果就是"无法确定"，多半是上游这一次没读到图（同一份共享结果还会
        // 同时喂给手机和悬浮窗）。用不带 question_id 的请求重新识别一次，绕过共享缓存。
        if (!manual && useImage && item.image && isUncertain(parsed)) {
          setAnsBusy('结果不确定，正在重新识别：' + (c.name || c.model) + ' …');
          parsed = await attempt(c, item, useImage, myId, true);
          if (currentItem && currentItem.id !== myId) return;
          if (IS_OVERLAY && overlayPaused) return;
        }
        showAnswer(parsed, c, trail, forcedNote);
        return;
      } catch (err) {
        if (currentItem && currentItem.id !== myId) return;
        if (IS_OVERLAY && overlayPaused) return;
        trail.push((c.name || c.model) + (useImage ? '(带图)' : '(纯文字)') + '：' + errMsg(err));
      }
    }
  }
  setAnsError('所有渠道均失败：\n' + trail.join('\n'), true);
}

function attempt(c, item, useImage, myId, manual) {
  const ctrl = new AbortController();
  curAsk = ctrl;
  const to = setTimeout(() => ctrl.abort(), (parseInt(c.timeout, 10) || 20) * 1000);
  return callChannel(c, item, useImage, ctrl.signal, manual).finally(() => clearTimeout(to));
}

function errMsg(err) {
  if (err && err.name === 'AbortError') return '超时';
  return String((err && err.message) || err).slice(0, 120);
}

async function callChannel(c, item, useImage, signal, manual) {
  const sys = settings.prompt || DEFAULT_PROMPT;
  const qtext = (item.text || '').trim();
  let userMsg;
  if (useImage && item.image) {
    userMsg = { role: 'user', content: [
      { type: 'image_url', image_url: { url: item.image } },
      { type: 'text', text: qtext || '请读取图片中的内容并给出选择建议' }
    ] };
  } else {
    userMsg = { role: 'user', content: qtext || '（无内容）' };
  }
  const r = await fetch('/api/llm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      base: c.base, key: c.key, model: c.model, api: c.api || 'chat',
      timeout: parseInt(c.timeout, 10) || 20,
      // 自动识别带上条目 id：手机和悬浮窗同时收到同一条内容时，服务端只向上游发一次。
      // 手动重试不带 id，保证用户点“重新识别”会真的重新请求。
      question_id: manual ? undefined : item.id,
      messages: [{ role: 'system', content: sys }, userMsg]
    }),
    signal
  });
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('代理响应异常 HTTP ' + r.status); }
  if (!j.ok) throw new Error(j.error || '请求失败');
  const txt = String(j.text || '');
  if (!txt.trim()) throw new Error('空回复');
  return parseAnswers(txt);
}

// 推理模型（deepseek-r1、QwQ 等）会把思考过程一起吐出来，先剥掉再找 JSON
function stripThinking(t) {
  return String(t || '')
    .replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '')
    .replace(/^[\s\S]*?<\/think(?:ing)?>/i, '')
    .trim();
}
// 按"括号配平"逐个抠 JSON 候选。不用贪心正则：思考过程里常带花括号（写示例、写代码），
// 贪心会从第一个 { 一路抓到最后一个 }，抓出来就解析不了。
function jsonCandidates(t) {
  const out = [];
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (ch !== '{' && ch !== '[') continue;
    const open = ch, close = ch === '{' ? '}' : ']';
    let depth = 0, inStr = false, esc = false, end = -1;
    for (let j = i; j < t.length; j++) {
      const c = t[j];
      if (inStr) {
        if (esc) esc = false;
        else if (c === '\\') esc = true;
        else if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') { inStr = true; continue; }
      if (c === open) depth++;
      else if (c === close && --depth === 0) { end = j; break; }
    }
    if (end > i) { out.push(t.slice(i, end + 1)); i = end; }
  }
  return out;
}
function parseAnswers(raw) {
  let t = stripThinking(raw).replace(/^```(?:json)?/i, '').replace(/```$/, '').trim();
  const one = o => {
    if (!o || typeof o !== 'object') return null;
    const a = o.answer !== undefined ? o.answer : o['答案'];
    if (a === undefined) return null;
    const no = o.no !== undefined ? o.no : o['题号'];
    return {
      no: no === undefined ? null : String(no).trim(),
      answer: String(a).trim(),
      analysis: String(o.analysis !== undefined ? o.analysis : (o['解析'] || '')).trim()
    };
  };
  const tryParse = s => {
    try {
      const j = JSON.parse(s);
      if (Array.isArray(j)) {
        const l = j.map(one).filter(Boolean);
        return l.length ? l : null;
      }
      const q = one(j);
      return q ? [q] : null;
    } catch (e) { return null; }
  };
  let r = tryParse(t);
  if (r) return sanitizeNos(r);
  for (const cand of jsonCandidates(t)) {
    r = tryParse(cand);
    if (r) return sanitizeNos(r);
  }
  const am = t.match(/(?:答案|answer)\s*[:：]?\s*([^\n，。;；]{1,60})/i);
  const an = t.match(/(?:解析|analysis|解释)\s*[:：]?\s*([\s\S]{1,150})/i);
  return [{ no: null, answer: am ? am[1].trim() : t.split('\n')[0].slice(0, 60), analysis: an ? an[1].trim() : '' }];
}

// 序号一致性校验：多条内容时要么每项都有合法序号（全部保留），要么全部退回顺序编号。
function sanitizeNos(list) {
  if (!Array.isArray(list)) return list;
  if (list.length < 2) return list;
  const allHave = list.every(q => q && q.no && /^\d{1,3}$/.test(String(q.no)));
  if (allHave) {
    const nums = list.map(q => parseInt(q.no, 10));
    const distinct = new Set(nums).size === nums.length;
    if (distinct) return list;
  }
  return list.map(q => ({ no: null, answer: q.answer, analysis: q.analysis }));
}

function showAnswer(parsedList, chan, trail, forcedNote) {
  askingId = null;
  const list = (parsedList && parsedList.length ? parsedList : [{ no: null, answer: '', analysis: '' }]);
  $('#ans-status').hidden = true;
  const ansEl = $('#ans-answer'), listEl = $('#ans-list');
  if (list.length === 1) {
    ansEl.textContent = list[0].answer || '（无结果）';
    ansEl.hidden = false;
    listEl.hidden = true;
    listEl.innerHTML = '';
  } else {
    ansEl.hidden = true;
    ansEl.textContent = '';
    listEl.innerHTML = '';
    list.slice(0, 12).forEach((q, i) => {
      const row = document.createElement('div'); row.className = 'mrow';
      const num = document.createElement('span'); num.className = 'mnum';
      num.textContent = q.no || String(i + 1);
      const ans = document.createElement('span'); ans.className = 'mans';
      ans.textContent = q.answer || '—';
      row.append(num, ans);
      if (q.analysis) {
        const note = document.createElement('span'); note.className = 'mnote';
        note.textContent = q.analysis;
        row.appendChild(note);
      }
      listEl.appendChild(row);
    });
    listEl.hidden = false;
  }
  $('#ans-analysis').hidden = true;
  const used = (forcedNote ? forcedNote + ' · ' : '')
    + (trail.length ? trail.join(' → ') + ' → ' : '') + (chan.name || chan.model)
    + (list.length > 1 ? ` · 共${list.length}条` : '');
  $('#ans-meta').textContent = used;
  $('#ans-meta').hidden = false;
  $('#btn-re-ask').hidden = false;
  if (currentItem) {
    answeredId = currentItem.id;
    histUpsert(currentItem.id, {
      answer: list.map((q, i) => `${q.no || i + 1}.${q.answer}`).join('  '),
      analysis: list.map(q => q.analysis).filter(Boolean).join(' / '),
      answers: list
    });
  }
  // 悬浮窗和手机同时收到结果：声音与震动只留给手机，避免电脑端重复播报
  if (!IS_OVERLAY) {
    if (settings.vib) {
      // 只有内容里出现 A. / B、 这类选项标记时才把数字结果当选项序号，
      // 输入类内容（OCR 关闭时 text 为空）不会因为数字是 42 就震 4 下再震 2 下。
      const hasOptions = questionHasOptions(currentItem && currentItem.text);
      if (list.length === 1) vibrateAns(list[0].answer, hasOptions);
      else vibrateAnsList(list, hasOptions);
    }
    if (settings.tts) speakAnsList(list);
  }
  requestAnimationFrame(applyAnswerFitting);
}

/* ---------- 超屏适配：先收起内容区，仍溢出则自动往返滚动（手机布局） ---------- */
function pageOverflows() {
  return document.documentElement.scrollHeight > window.innerHeight + 2;
}
function stopBounceScroll() {
  if (scrollAnim) {
    cancelAnimationFrame(scrollAnim.raf);
    document.removeEventListener('touchstart', stopBounceScroll, { passive: true });
    scrollAnim = null;
  }
}
function applyAnswerFitting() {
  if (IS_PC) return;
  if (IS_OVERLAY) {
    // 内容窗整窗穿透，收不到滚轮/触摸；把结果直接送到视口顶部，回看内容用工具条 ↑
    stopBounceScroll();
    const card = $('#ans-card');
    if (card) requestAnimationFrame(() => {
      window.scrollTo({ top: Math.max(0, card.offsetTop - 6), behavior: 'smooth' });
    });
    return;
  }
  stopBounceScroll();
  window.scrollTo(0, 0);
  if (!pageOverflows()) { $('#fit-note').hidden = true; return; }
  if (!$('#q-card').hidden && currentItem) {
    $('#q-card').hidden = true;
    $('#fit-note').hidden = false;
  }
  requestAnimationFrame(() => {
    if (!pageOverflows()) return;
    if (curView !== 'main') return;
    if (!settings.autoScroll) return;
    startBounceScroll();
  });
}
function startBounceScroll() {
  stopBounceScroll();
  const SPEED = 42;
  const DWELL = 1500;
  const st = { dir: 1, pos: window.scrollY, last: 0, dwell: DWELL };
  document.addEventListener('touchstart', stopBounceScroll, { passive: true });
  const anim = {};
  scrollAnim = anim;
  function step(now) {
    if (scrollAnim !== anim) return;
    const max = document.documentElement.scrollHeight - window.innerHeight;
    if (max <= 2 || curView !== 'main') { stopBounceScroll(); return; }
    if (!st.last) st.last = now;
    const dt = Math.min(now - st.last, 100);
    st.last = now;
    if (st.dwell > 0) {
      st.dwell -= dt;
    } else {
      st.pos += st.dir * SPEED * dt / 1000;
      if (st.pos >= max) { st.pos = max; st.dir = -1; st.dwell = DWELL; }
      else if (st.pos <= 0) { st.pos = 0; st.dir = 1; st.dwell = DWELL; }
      window.scrollTo(0, st.pos);
    }
    anim.raf = requestAnimationFrame(step);
  }
  anim.raf = requestAnimationFrame(step);
}

/* ---------- 震动 / 语音 ---------- */
const CN_NUM = { '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10 };
const VIB_LEVELS = [
  { name: '弱', intensity: 0.45, sharpness: 0.4 },
  { name: '中', intensity: 0.75, sharpness: 0.55 },
  { name: '强', intensity: 1.0, sharpness: 0.72 }
];
const VIB_PULSE_MS = 180;
const VIB_MAX_PULSES = 8;
const VIB_MAX_GROUPS = 6;

function vibNum(v, def, min, max) {
  const n = parseInt(v, 10);
  return isNaN(n) ? def : Math.min(max, Math.max(min, n));
}
function vibLevel() {
  return VIB_LEVELS[vibNum(settings.vibStrength, 1, 0, VIB_LEVELS.length - 1)];
}
// 只有确认是选择题时，数字/中文数字才按"第几个选项"解释；填空题的 42、12 这类
// 多位数结果不能被拆成 [4,2] 去震。字母本身就是选项，不受此限制。
function questionHasOptions(text) {
  return /(^|[\s\n（(])([A-Ha-h])[.、:：)）]\s*\S/.test(String(text || ''));
}
function optionPulses(ans, hasOptions = true) {
  const a = String(ans || '').trim();
  if (!a) return null;
  const m = a.match(/^([A-Ha-h](?:[\s,，、/和]*[A-Ha-h])*)(?![A-Za-z])/);
  if (m) {
    const letters = [...new Set(m[1].toUpperCase().replace(/[^A-H]/g, ''))];
    if (letters.length && letters.length <= VIB_MAX_GROUPS) {
      return letters.map(ch => ch.charCodeAt(0) - 64);
    }
  }
  if (hasOptions) {
    // 多位必须用分隔符写开（"1,3"），否则 "42" 会被误读成 4、2；单位数才是第 n 个选项。
    const multi = a.match(/^([1-9](?:[\s,，、/和]+[1-9])+)(?![0-9])/);
    if (multi) {
      const ds = [...new Set(multi[1].replace(/[^1-9]/g, ''))];
      if (ds.length) return ds.map(Number);
    }
    const one = a.match(/^([1-9])(?![0-9])/);
    if (one) return [Number(one[1])];
  }
  if (/^(正确|对|是)$/.test(a)) return [1];
  if (/^(错误|错|否)$/.test(a)) return [2];
  if (hasOptions) {
    const cn = a.match(/^[一二三四五六七八九十]/);
    if (cn) return [CN_NUM[cn[0]]];
  }
  return null;
}
// iPhone 网页拿不到震动接口；配套 iOS App 用 WKWebView 包住本页并注册
// window.webkit.messageHandlers.haptics，有它时交给原生 CoreHaptics 播放。
function nativeHaptics() {
  try {
    const h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.haptics;
    return h && typeof h.postMessage === 'function' ? h : null;
  } catch (e) { return null; }
}
function vibrateAvailable() {
  return !!nativeHaptics() || typeof navigator.vibrate === 'function';
}
function buildVibPattern(perQuestion) {
  const pulseGap = vibNum(settings.vibPulseGap, 260, 40, 2000);
  const optGap = vibNum(settings.vibOptGap, 650, 60, 3000);
  const quesGap = vibNum(settings.vibQuesGap, 800, 60, 4000);
  const pat = [];
  perQuestion.forEach((groups, qi) => {
    if (qi > 0) pat.push(quesGap);
    groups.forEach((n, gi) => {
      if (gi > 0) pat.push(optGap);
      for (let i = 0; i < Math.min(n, VIB_MAX_PULSES); i++) {
        if (i > 0) pat.push(pulseGap);
        pat.push(VIB_PULSE_MS);
      }
    });
  });
  return pat;
}
function playVibration(pat) {
  if (!pat || !pat.length) return false;
  const bridge = nativeHaptics();
  if (bridge) {
    const lv = vibLevel();
    try {
      bridge.postMessage({ pattern: pat, intensity: lv.intensity, sharpness: lv.sharpness });
      return true;
    } catch (e) {}
  }
  if (typeof navigator.vibrate === 'function') {
    try { return navigator.vibrate(pat); } catch (e) { return false; }
  }
  return false;
}
function vibrateAns(ans, hasOptions = true) {
  const groups = optionPulses(ans, hasOptions);
  if (!groups || groups.length > VIB_MAX_GROUPS) return false;
  return playVibration(buildVibPattern([groups]));
}
function previewVib() {
  if (!settings.vib) return;
  vibrateAns('C');
}
function vibrateAnsList(list, hasOptions = true) {
  const perQ = [];
  list.slice(0, 4).forEach(q => {
    const groups = optionPulses(q.answer, hasOptions);
    if (groups && groups.length <= VIB_MAX_GROUPS) perQ.push(groups);
  });
  return playVibration(buildVibPattern(perQ));
}
function speakAns(p) {
  speakAnsList(Array.isArray(p) ? p : [p]);
}
function speakAnsList(list) {
  if (!('speechSynthesis' in window)) return;
  const many = list.length > 1;
  const parts = list.slice(0, 8).map((q, i) => {
    let s = many ? `第${q.no || i + 1}条，选择${phoneticAns(q.answer)}` : `选择 ${phoneticAns(q.answer) || ''}`;
    if (settings.ttsAnalysis && q.analysis) s += `。${q.analysis.slice(0, 50)}`;
    return s;
  });
  const t = parts.join('。');
  const u = new SpeechSynthesisUtterance(t);
  u.lang = 'zh-CN'; u.rate = 1.12;
  const zh = speechSynthesis.getVoices().filter(v => v.lang && v.lang.toLowerCase().indexOf('zh') === 0);
  if (zh.length) u.voice = zh[0];
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
}
function phoneticAns(ans) {
  const s = String(ans || '');
  if (!settings.bdPhonetic) return s;
  const t = s.trim();
  if (!/^[A-Ha-h](?:[\s,，、]*[A-Ha-h])*$/.test(t)) return s;
  return t.split('').map(ch => ch === 'B' ? 'Bravo' : ch === 'D' ? 'Delta' : ch).join(' ');
}

/* ---------- 历史 ---------- */
function histUpsert(id, patch) {
  let h = history.find(x => x.id === id);
  if (!h) { h = Object.assign({ id, ts: Date.now(), text: '', answer: '', analysis: '' }, patch); history.unshift(h); }
  else Object.assign(h, patch);
  history = history.slice(0, 30);
  saveHistory();
  if (curView === 'history') renderHistory();
}
function renderHistory() {
  const list = $('#hist-list');
  list.innerHTML = '';
  $('#hist-empty').hidden = history.length > 0;
  history.forEach(h => {
    const div = document.createElement('div');
    div.className = 'card hist-item';
    const t = document.createElement('div'); t.className = 't'; t.textContent = fmtTime(h.ts);
    const q = document.createElement('div'); q.className = 'q'; q.textContent = h.text || '（图片内容）';
    const a = document.createElement('div'); a.className = 'a'; a.textContent = h.answer ? '结果：' + h.answer : '（无结果）';
    div.append(t, q, a);
    div.onclick = () => {
      switchView('main');
      $('#empty-state').hidden = true;
      $('#q-card').hidden = false;
      $('#q-time').textContent = fmtTime(h.ts);
      $('#q-images').innerHTML = '';
      $('#btn-save').disabled = true;
      $('#q-text').textContent = h.text || '（历史记录不含图片）';
      $('#ans-status').hidden = true;
      $('#ans-answer').textContent = h.answer || '—'; $('#ans-answer').hidden = !h.answer;
      $('#ans-analysis').textContent = h.analysis || ''; $('#ans-analysis').hidden = !h.analysis;
      $('#ans-meta').hidden = true; $('#btn-re-ask').hidden = true;
      currentItem = { id: 'hist-' + h.id, ts: h.ts, image: null, text: h.text };
      requestAnimationFrame(applyAnswerFitting);
    };
    list.appendChild(div);
  });
}

/* ================================================================
   统一设置渲染器：手机设置页与 PC 右栏共用（页面一致的两个入口）
   ================================================================ */

const SCHEMA = [
  { id: 'channels', icon: 'plug', title: '模型渠道', sub: '按排列顺序识别，失败后自动切换', type: 'channels' },
  { id: 'ask', icon: 'send', title: '识别行为', items: [
    { type: 'toggle', key: 'autoAsk', label: '收到内容后自动识别', icon: 'zap' },
    { type: 'toggle', key: 'includeImage', label: '识别时附带截图', icon: 'camera', hint: '未识别到文字时，始终附带截图',
      pop: '开启后屏幕截图一起发给模型，视觉判断更准；电脑端没识别到文字时即使关闭也会自动附图。' },
    { type: 'toggle', key: 'autoScroll', label: '超屏内容自动滚动', icon: 'swipe', hint: '手机端内容超出屏幕时，自动往返滚动' },
  ]},
  { id: 'alert', icon: 'bell', title: '手机提醒', items: [
    { type: 'toggle', key: 'tts', label: '语音播报结果', icon: 'volume' },
    { type: 'toggle', key: 'ttsAnalysis', label: '同时播报解析', icon: 'chat', hint: '播报结果后，继续朗读简短解析' },
    { type: 'toggle', key: 'bdPhonetic', label: '区分易混淆字母', icon: 'volume', hint: 'B（选项二）和 D（选项四）使用北约音标读音',
      pop: '仅对纯选项结果生效：B（选项二）读作 Bravo（布拉沃），D（选项四）读作 Delta（德尔塔）。页面仍显示原选项字母。' },
    { type: 'toggle', key: 'vib', label: '振动提示结果', icon: 'vibrate', hint: '按选项序号振动：第几个选项，就振动几次',
      pop: '选项按序号振动：第一项一次，第二项两次，以此类推。多选按选项依次提示，多条内容按顺序提示。苹果手机需使用配套应用，安卓手机可在浏览器中使用。' },
    { type: 'select', key: 'vibStrength', label: '振动强度', icon: 'zap',
      options: [['0', '弱'], ['1', '中'], ['2', '强']], hint: '仅在配套手机应用中生效', after: previewVib },
    { type: 'button', id: 'btn-feedback-test', label: '测试提醒', btn: '测试语音与振动', icon: 'zap' },
  ]},
  { id: 'pc', icon: 'monitor', title: '电脑控制', items: [
    { type: 'rtoggle', id: 'rt-ocr', label: '识别截图文字', icon: 'text', api: '/api/ocr',
      flag: 'ocr_enabled', hint: '先识别文字，再发送给模型',
      pop: '适合仅接受文字的模型；支持图片的模型通常可以关闭。未识别到文字时，会自动改用截图识别。' },
    { type: 'rtoggle', id: 'rt-corner', label: '触碰屏幕角落截图', icon: 'corner', api: '/api/corner',
      flag: 'corner_snap', hint: '鼠标停留在屏幕角落时，自动截取全屏',
      pop: '鼠标停留在截图屏幕的任一角落即可触发，无需点击。触发后需先移开鼠标，并等待约 1.5 秒才能再次触发。' },
    { type: 'rtoggle', id: 'rt-swipe', label: '横向滑动鼠标截图', icon: 'swipe', api: '/api/swipe', tuning: true,
      flag: 'swipe_snap', hint: '松开鼠标键，横向平稳移动一小段',
      pop: '横向平稳移动一小段即可触发，无需按键。按住鼠标键拖动、选择文字或移动窗口时不会触发；可在电脑端调整触发参数，并打开全屏调试寻找合适的手感。' },
    { type: 'rtoggle', id: 'rt-stealth', label: '防截屏', icon: 'eye-off', api: '/api/stealth',
      flag: 'anti_capture', hint: '在截图、录屏和屏幕共享中隐藏本窗口',
      pop: '开启后，本窗口不出现在截图、录屏和屏幕共享中。你仍可正常查看和操作窗口，手机遥控截图也不受影响。' },
    { type: 'number', key: 'overlayHover', label: '悬浮窗悬停触发', icon: 'timer',
      min: 0.2, max: 3, step: 0.1, float: true, unit: '秒',
      hint: '鼠标在悬浮窗按钮上停留多久触发；拖动时静止这么久结束跟随',
      pop: '鼠标停在悬浮窗的按钮上达到这个时间就等同点击一次；「拖动」按钮则是悬停后开始跟随鼠标，跟随时鼠标静止同样时间就停止。按钮顶部的进度条会显示等待进度。' },
    { type: 'rstatus', id: 'pc-status', phoneOnly: true },
    { type: 'rbuttons', buttons: [
      { id: 'btn-calibrate', label: '框选截图区域', icon: 'crop', phoneOnly: true },
      { id: 'btn-reconnect', label: '重新连接', icon: 'reload', phoneOnly: true },
    ]},
    { type: 'button', id: 'btn-pc-quit', label: '退出电脑端', btn: '退出电脑端', icon: 'power', danger: true, phoneOnly: true },
  ]},
  { id: 'advanced', icon: 'sliders', title: '高级设置', sub: '提示词、振动节奏与配置管理',
    collapsible: true, items: [
    { type: 'prompt' },
    { type: 'numgroup', label: '振动节奏', icon: 'timer',
      pop: '振动次数由选项序号决定，这里仅调整时间间隔。修改后会自动振动三次，便于感受新节奏。' },
    { type: 'number', key: 'vibPulseGap', label: '单次振动间隔', min: 40, max: 2000, after: previewVib },
    { type: 'number', key: 'vibOptGap', label: '选项之间的间隔', min: 60, max: 3000, after: previewVib },
    { type: 'number', key: 'vibQuesGap', label: '内容之间的间隔', min: 60, max: 4000, after: previewVib },
    { type: 'room', phoneOnly: true },
    { type: 'data' },
  ]},
];

function mkSwitch(checked, attrs) {
  const lab = document.createElement('label');
  lab.className = 'sw';
  const inp = document.createElement('input');
  inp.type = 'checkbox';
  if (checked) inp.checked = true;
  if (attrs) Object.entries(attrs).forEach(([k, v]) => inp.setAttribute(k, v));
  const i = document.createElement('i');
  lab.append(inp, i);
  return lab;
}

function positionTooltip(info, pop) {
  const anchor = info.getBoundingClientRect(), rect = pop.getBoundingClientRect();
  if (anchor.bottom < 0 || anchor.top > innerHeight) { pop.hidden = true; return; }
  pop.style.left = Math.max(12, Math.min(innerWidth - rect.width - 12, anchor.right - rect.width)) + 'px';
  const below = anchor.bottom + 8;
  pop.style.top = Math.max(12, below + rect.height <= innerHeight - 12 ? below : anchor.top - rect.height - 8) + 'px';
}

function mkHintPop(text, id) {
  const d = document.createElement('div');
  d.className = 'hint-pop';
  d.hidden = true;
  if (id) d.id = id;
  d.textContent = text;
  return d;
}

function secHeader(sec, collapsible) {
  const h = document.createElement(collapsible ? 'summary' : 'div');
  h.className = 'sec-h';
  h.appendChild(ico(sec.icon));
  const t = document.createElement(collapsible ? 'span' : 'h2');
  t.className = 't';
  t.textContent = sec.title;
  h.appendChild(t);
  if (sec.sub) {
    const s = document.createElement('span');
    s.className = 'sec-b';
    s.textContent = sec.sub;
    h.appendChild(s);
  }
  return h;
}

function renderRow(item) {
  const row = document.createElement('div');
  row.className = 'srow';
  if (item.icon) row.appendChild(ico(item.icon));
  const lab = document.createElement('div');
  lab.className = 'lab';
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = item.label;
  lab.appendChild(t);
  if (item.hint) {
    const h = document.createElement('span');
    h.className = 'h';
    h.textContent = item.hint;
    lab.appendChild(h);
  }
  row.appendChild(lab);
  if (item.pop) {
    const info = document.createElement('button');
    info.className = 'ibtn info';
    info.title = '说明';
    info.setAttribute('aria-label', item.label + '说明');
    info.setAttribute('aria-expanded', 'false');
    info.appendChild(ico('info'));
    const pop = mkHintPop(item.pop, item.popId);
    if (IS_PC) {
      const help = document.createElement('span'); help.className = 'help-tooltip';
      pop.className = 'hint-tooltip';
      pop.id = item.id === 'rt-stealth' ? 'stealth-pop' : item.id === 'rt-ocr' ? 'ocr-pop' : (item.key || item.id || 'vib-rhythm') + '-hint';
      pop.setAttribute('role', 'tooltip');
      info.removeAttribute('title');
      info.removeAttribute('aria-expanded');
      info.setAttribute('aria-describedby', pop.id);
      const show = () => { pop.hidden = false; positionTooltip(info, pop); };
      const hide = () => {
        if (document.activeElement !== info && !help.matches(':hover')) pop.hidden = true;
      };
      help.onmouseenter = show; help.onmouseleave = hide;
      info.onfocus = show; info.onblur = hide;
      help.append(info, pop); row.appendChild(help);
    } else {
      info.onclick = () => {
        pop.hidden = !pop.hidden;
        info.setAttribute('aria-expanded', String(!pop.hidden));
      };
      row.appendChild(info);
      row._pop = pop;
    }
  }
  return row;
}

function mkToggleRow(item) {
  const row = renderRow(item);
  const sw = mkSwitch(!!settings[item.key], { 'aria-label': item.label });
  sw.querySelector('input').onchange = e => setSetting(item.key, e.target.checked);
  row.appendChild(sw);
  return row._pop ? [row, row._pop] : [row];
}

function mkRtoggleRow(item) {
  const row = renderRow(item);
  row._pop && (row._pop.id = item.id + '-pop');
  if (item.id === 'rt-stealth' && row._pop) row._pop.id = 'stealth-pop';
  if (item.id === 'rt-ocr' && row._pop) row._pop.id = 'ocr-pop';
  const sw = mkSwitch(false, { id: item.id, 'aria-label': item.label });
  sw.querySelector('input').dataset.api = item.api;
  sw.querySelector('input').onchange = async e => {
    const on = e.target.checked;
    try {
      const r = await fetch(item.api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: on }),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || 'bad response');
    } catch (err) {
      e.target.checked = !on;
      toast('设置失败：电脑端是否在线？');
    }
  };
  row.appendChild(sw);
  if (item.tuning && IS_PC) {
    const group = document.createElement('div');
    group.className = 'swipe-setting-group';
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', item.label);
    group.append(row, window.SwipeTuning.createEntry());
    return [group];
  }
  return row._pop ? [row, row._pop] : [row];
}

function mkSelectRow(item) {
  const row = renderRow(item);
  const sel = document.createElement('select');
  sel.setAttribute('aria-label', item.label);
  item.options.forEach(([v, l]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = l;
    sel.appendChild(o);
  });
  sel.value = String(settings[item.key]);
  sel.onchange = () => { setSetting(item.key, sel.value); if (item.after) item.after(); };
  row.appendChild(sel);
  return [row];
}

function mkNumberRow(item) {
  const row = renderRow(item);
  const unitText = item.unit || '毫秒';
  const float = !!item.float;
  const inp = document.createElement('input');
  inp.type = 'number';
  inp.setAttribute('aria-label', item.label + '（' + unitText + '）');
  inp.min = item.min; inp.max = item.max; inp.step = item.step || 10;
  inp.inputMode = float ? 'decimal' : 'numeric';
  inp.value = settings[item.key];
  inp.onchange = () => {
    const raw = float ? parseFloat(inp.value) : parseInt(inp.value, 10);
    const def = settings[item.key];
    const n = isNaN(raw) ? def : Math.min(item.max, Math.max(item.min, raw));
    inp.value = n;
    setSetting(item.key, n);
    if (item.after) item.after();
  };
  const unit = document.createElement('span');
  unit.className = 'field-unit'; unit.textContent = unitText;
  row.append(inp, unit);
  return [row];
}

function mkButtonRow(item) {
  // 动作行只出一个按钮（左侧再写一遍同名文字就是重复）：整行按钮，图标 + 文字
  const row = document.createElement('div');
  row.className = 'sec-foot';
  const b = document.createElement('button');
  b.className = 'btn ghost growb' + (item.danger ? ' danger' : '');
  b.id = item.id || '';
  if (item.icon) b.appendChild(ico(item.icon));
  b.appendChild(document.createTextNode(item.btn || item.label || ''));
  row.appendChild(b);
  return [row];
}

function mkRbuttonsRow(item) {
  const row = document.createElement('div');
  row.className = 'sec-foot';
  item.buttons.forEach(bt => {
    if (bt.phoneOnly && IS_PC) return;
    const b = document.createElement('button');
    b.className = 'btn ghost sm growb';
    b.id = bt.id;
    if (bt.icon) b.appendChild(ico(bt.icon));
    b.appendChild(document.createTextNode(bt.label));
    row.appendChild(b);
  });
  return row.children.length ? [row] : [];
}

function mkRstatusRow(item) {
  const row = document.createElement('div');
  row.className = 'srow';
  row.appendChild(ico('monitor'));
  const lab = document.createElement('div');
  lab.className = 'lab';
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = '状态';
  const h = document.createElement('span');
  h.className = 'h';
  h.id = 'pc-status-val';
  h.textContent = '电脑端状态读取中…';
  lab.append(t, h);
  row.appendChild(lab);
  const dot = document.createElement('span');
  dot.className = 'dot connecting';
  dot.id = 'pc-status-dot';
  row.appendChild(dot);
  return [row];
}

function mkPromptItem() {
  const wrap = document.createElement('div');
  wrap.style.padding = '10px 2px 6px';
  const lab = document.createElement('span');
  lab.className = 'field-lab';
  lab.textContent = '提示词 · 设定模型的识别方式';
  const ta = document.createElement('textarea');
  ta.id = 'prompt';
  ta.setAttribute('aria-label', '提示词');
  ta.rows = 7;
  ta.spellcheck = false;
  ta.value = settings.prompt || DEFAULT_PROMPT;
  const savePrompt = debounce(() => setSetting('prompt', ta.value));
  ta.addEventListener('input', savePrompt);
  ta.addEventListener('change', savePrompt.flush);
  const bar = document.createElement('div');
  bar.className = 'sec-foot';
  const reset = document.createElement('button');
  reset.className = 'btn ghost xs';
  reset.id = 'btn-prompt-reset';
  reset.appendChild(ico('reload'));
  reset.appendChild(document.createTextNode('恢复默认提示词'));
  reset.onclick = () => {
    settings.prompt = DEFAULT_PROMPT;
    ta.value = DEFAULT_PROMPT;
    setSetting('prompt', DEFAULT_PROMPT);
    toast('已恢复默认');
  };
  bar.appendChild(reset);
  wrap.append(lab, ta, bar);
  return [wrap];
}

function mkNumgroupRow(item) {
  const row = renderRow({ ...item, type: 'info' });
  return row._pop ? [row, row._pop] : [row];
}

function mkRoomItem() {
  const wrap = document.createElement('div');
  wrap.style.padding = '10px 2px 6px';
  const lab = document.createElement('span');
  lab.className = 'field-lab';
  lab.textContent = '中转房间码（仅中转模式需要，局域网留空）';
  const inp = document.createElement('input');
  inp.id = 'room';
  inp.setAttribute('aria-label', '中转房间码');
  inp.placeholder = '房间码';
  inp.value = settings.room || '';
  inp.addEventListener('input', () => { settings.room = inp.value.trim(); saveSettings(); });
  const origin = document.createElement('p');
  origin.className = 'hint';
  origin.innerHTML = '当前页面地址：<span id="cur-origin" class="code"></span>';
  wrap.append(lab, inp, origin);
  return [wrap];
}

function mkDataItem() {
  const row = document.createElement('div');
  row.className = 'sec-foot';
  const mk = (id, label, icon) => {
    const b = document.createElement('button');
    b.className = 'btn ghost sm';
    b.id = id;
    b.appendChild(ico(icon));
    b.appendChild(document.createTextNode(label));
    row.appendChild(b);
  };
  mk('btn-export', '导出设置', 'down');
  mk('btn-import', '导入设置', 'up');
  if (!IS_PC) mk('btn-clear-hist', '清空历史', 'trash');
  const f = document.createElement('input');
  f.type = 'file';
  f.id = 'file-import';
  f.accept = 'application/json,.json';
  f.hidden = true;
  f.onchange = e => {
    if (e.target.files[0]) importSettings(e.target.files[0]);
    e.target.value = '';
  };
  row.appendChild(f);
  return [row];
}

/* 渠道卡片（与旧版结构一致，按钮改为图标） */
function renderChannelsInto(host) {
  const list = document.createElement('div');
  list.id = 'ch-list';
  host.appendChild(list);
  if (IS_PC && !settings.channels.length) {
    const empty = document.createElement('p');
    empty.className = 'ch-empty';
    empty.textContent = '还没有模型渠道。添加后填写接口地址、密钥和模型，即可开始识别。';
    list.appendChild(empty);
  }
  const add = document.createElement('button');
  add.className = 'btn ghost sm';
  add.id = 'btn-add-ch';
  add.appendChild(ico('plus'));
  add.appendChild(document.createTextNode('添加渠道'));
  add.style.margin = '2px 0 8px';
  host.appendChild(add);

  settings.channels.forEach((c, i) => {
    const card = document.createElement('div');
    card.className = 'ch' + (c.enabled ? '' : ' off');

    const head = document.createElement('div'); head.className = 'ch-head';
    const name = document.createElement('input'); name.type = 'text'; name.className = 'ch-name';
    name.value = c.name || ''; name.placeholder = '渠道名称';
    name.setAttribute('aria-label', '渠道名称');
    const en = mkSwitch(!!c.enabled, { 'aria-label': '启用此渠道' });
    head.append(name, en);
    if (!c.enabled) {
      const state = document.createElement('span');
      state.className = 'ch-state'; state.textContent = '已停用';
      head.insertBefore(state, en);
    }

    const apiSel = document.createElement('select');
    apiSel.className = 'ch-api';
    [['chat', 'Chat Completions（通用聊天接口）'],
     ['responses', 'Responses（响应接口）'],
     ['anthropic', 'Anthropic Messages（消息接口）']].forEach(([v, l]) => {
      const o = document.createElement('option');
      o.value = v; o.textContent = l;
      if ((c.api || 'chat') === v) o.selected = true;
      apiSel.appendChild(o);
    });
    apiSel.onchange = () => { c.api = apiSel.value; setSetting('channels', settings.channels); };

    const base = document.createElement('input'); base.type = 'url'; base.className = 'ch-base';
    base.value = c.base || ''; base.placeholder = 'https://api.xxx.com/v1';

    const grid = document.createElement('div'); grid.className = 'ch-grid';
    const key = document.createElement('input'); key.type = 'password'; key.className = 'wide';
    key.value = c.key || ''; key.placeholder = '填写接口密钥';
    const model = document.createElement('input'); model.type = 'text';
    model.value = c.model || ''; model.placeholder = '填写模型名称';
    const to = document.createElement('input'); to.type = 'number'; to.min = 3; to.max = 120;
    to.value = c.timeout || 20; to.placeholder = '超时秒数';
    const field = (label, control, wide = false) => {
      control.setAttribute('aria-label', label);
      const wrap = document.createElement('label');
      wrap.className = 'ch-field' + (wide ? ' wide' : '');
      const text = document.createElement('span'); text.textContent = label;
      wrap.append(text, control);
      return wrap;
    };
    if (IS_PC) {
      grid.append(field('接口类型', apiSel), field('模型名称', model),
                  field('接口地址', base, true), field('接口密钥', key), field('超时（秒）', to));
    } else {
      [[apiSel, '接口类型'], [base, '接口地址'], [key, '接口密钥'], [model, '模型名称'], [to, '超时（秒）']]
        .forEach(([el, label]) => el.setAttribute('aria-label', label));
      grid.append(key, model, to);
    }

    const ops = document.createElement('div'); ops.className = 'ch-ops';
    const mkI = (icon, title, fn) => {
      const b = document.createElement('button');
      b.className = 'ibtn'; b.title = title;
      b.appendChild(ico(icon));
      b.onclick = fn;
      ops.appendChild(b);
      return b;
    };
    const up = mkI('arrow-up', '上移（优先识别）', () => {
      if (i > 0) { [settings.channels[i - 1], settings.channels[i]] = [settings.channels[i], settings.channels[i - 1]];
        setSetting('channels', settings.channels); renderSettingsAll(); }
    });
    up.disabled = i === 0;
    const down = mkI('arrow-dn', '下移（回退顺序）', () => {
      if (i < settings.channels.length - 1) { [settings.channels[i + 1], settings.channels[i]] = [settings.channels[i], settings.channels[i + 1]];
        setSetting('channels', settings.channels); renderSettingsAll(); }
    });
    down.disabled = i === settings.channels.length - 1;
    mkI('trash', '删除渠道', () => {
      settings.channels.splice(i, 1);
      setSetting('channels', settings.channels); renderSettingsAll();
    });
    const result = document.createElement('p');
    result.className = 'ch-test-status';
    result.setAttribute('role', 'status');
    result.hidden = true;
    const test = document.createElement('button');
    test.className = 'btn-test'; test.textContent = '测试连接';
    test.onclick = async () => {
      test.disabled = true;
      test.textContent = '测试中…';
      result.hidden = true;
      const ctrl = new AbortController();
      const t0 = Date.now();
      const timer = setTimeout(() => ctrl.abort(), (parseInt(c.timeout, 10) || 20) * 1000);
      try {
        const r = await callChannel(c, { text: '连通性测试，请直接按 JSON 格式回复。', image: null }, false, ctrl.signal);
        result.textContent = '连接成功 · ' + ((Date.now() - t0) / 1000).toFixed(1) + ' 秒 · 返回：' + (r[0] && r[0].answer || '').slice(0, 30);
      } catch (e) {
        result.textContent = '连接失败：' + errMsg(e);
      } finally {
        clearTimeout(timer);
        test.disabled = false;
        test.textContent = '测试连接';
        result.hidden = false;
      }
    };
    ops.appendChild(test);

    const saveChannels = debounce(() => setSetting('channels', settings.channels));
    const bind = (el, f) => {
      el.addEventListener('input', () => { f(el.value); saveChannels(); });
      el.addEventListener('change', () => { f(el.value); saveChannels.flush(); });
    };
    bind(name, v => c.name = v);
    en.querySelector('input').onchange = e => { c.enabled = e.target.checked; setSetting('channels', settings.channels); renderSettingsAll(); };
    bind(base, v => c.base = v.trim());
    bind(key, v => c.key = v.trim());
    bind(model, v => c.model = v.trim());
    bind(to, v => c.timeout = parseInt(v, 10) || 20);

    if (IS_PC) card.append(head, grid, ops, result);
    else card.append(head, apiSel, base, grid, ops, result);
    list.appendChild(card);
  });

  $('#btn-add-ch').onclick = () => {
    settings.channels.push({ name: '渠道' + (settings.channels.length + 1), api: 'chat', base: '', key: '', model: '', timeout: 20, enabled: true });
    setSetting('channels', settings.channels);
    renderSettingsAll();
    // 渲染后聚焦新卡片，连续录入时无需重新寻找输入位置。
    const currentName = $('#ch-list .ch:last-child .ch-name');
    if (currentName) currentName.focus();
  };
}

function renderSettingsAll() {
  const host = IS_PC ? $('#pc-settings') : $('#phone-settings');
  if (!host) return;
  host.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'set-wrap';
  host.appendChild(wrap);

  SCHEMA.forEach(sec => {
    // 用真实分区承载标题和内容，统一分组边界与纵向间距。
    const section = document.createElement('section');
    section.className = 'set-section set-' + sec.id;
    section.id = 'settings-' + sec.id;
    section.setAttribute('aria-label', sec.title);
    wrap.appendChild(section);
    if (sec.type === 'channels') {
      const sh = secHeader(sec, false);
      section.appendChild(sh);
      const card = document.createElement('div');
      card.className = 'sec-card';
      card.style.padding = '12px 14px';
      section.appendChild(card);
      renderChannelsInto(card);
      return;
    }
    let body;
    if (sec.collapsible) {
      const det = document.createElement('details');
      det.className = 'collap';
      det.id = 'adv-settings';
      try { det.open = localStorage.getItem('qa_adv_open') === '1'; } catch (e) {}
      const sum = secHeader(sec, true);
      sum.style.cursor = 'pointer';
      const chev = ico('chev');
      chev.classList.add('chev');
      sum.appendChild(chev);
      det.appendChild(sum);
      body = document.createElement('div');
      body.className = 'collap-body';
      det.appendChild(body);
      section.appendChild(det);
    } else {
      const sh = secHeader(sec, false);
      section.appendChild(sh);
      const card = document.createElement('div');
      card.className = 'sec-card';
      section.appendChild(card);
      body = card;
    }
    (sec.items || []).forEach(item => {
      if (item.phoneOnly && IS_PC) return;
      let nodes = [];
      switch (item.type) {
        case 'toggle': nodes = mkToggleRow(item); break;
        case 'rtoggle': nodes = mkRtoggleRow(item); break;
        case 'select': nodes = mkSelectRow(item); break;
        case 'number': nodes = mkNumberRow(item); break;
        case 'button': nodes = mkButtonRow(item); break;
        case 'rbuttons': nodes = mkRbuttonsRow(item); break;
        case 'rstatus': nodes = mkRstatusRow(item); break;
        case 'prompt': nodes = mkPromptItem(); break;
        case 'numgroup': nodes = mkNumgroupRow(item); break;
        case 'room': nodes = mkRoomItem(); break;
        case 'data': nodes = mkDataItem(); break;
      }
      nodes.forEach(n => body.appendChild(n));
    });
  });

  if (!IS_PC) {
    const ver = document.createElement('span');
    ver.className = 'ver';
    ver.textContent = '无障碍助手 v1.4.2';
    wrap.appendChild(ver);
    // 震动不可用时的一行说明（iPhone 浏览器）
    if (!vibrateAvailable()) {
      const h = mkHintPop('iPhone 网页无法震动：苹果从未实现震动接口，程序化触发在 iOS 26.5 被封死。' +
        '要按选项序号精准震动，请装配套 iOS App（见 README 的 ios/ 一节）。安卓 Chrome 不受影响。');
      h.hidden = false;
      h.id = 'vib-note';
      wrap.appendChild(h);
    }
    const origin = $('#cur-origin');
    if (origin) origin.textContent = location.host;
  }
  if (curStatus) applyStatus(curStatus);
}

/* ---------- 导出 / 导入 ---------- */
function exportSettings() {
  const blob = new Blob([JSON.stringify(settings, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'qa-settings.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}
function importSettings(file) {
  const fr = new FileReader();
  fr.onload = () => {
    try {
      const s = JSON.parse(fr.result);
      settings = Object.assign({}, DEF_SETTINGS, s);
      saveSettings();
      send({ type: 'settings_set', patch: settings });
      renderSettingsAll();
      toast('设置已导入');
    } catch (e) { toast('导入失败：文件格式不正确'); }
  };
  fr.readAsText(file);
}

/* ---------- 视图 / 事件 ---------- */
function switchView(v) {
  curView = v;
  stopBounceScroll();
  $('#view-main').hidden = v !== 'main';
  $('#view-history').hidden = v !== 'history';
  $('#view-settings').hidden = v !== 'settings';
  document.querySelectorAll('#tabbar button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  if (v === 'history') renderHistory();
  if (v === 'settings') refreshPcStatus();
}

async function refreshPcStatus() {
  // 状态主要靠 WS 广播（局域网与中转都会推）；这里补一次 HTTP 兜底，
  // 但旧中转服务器的 /api/status 只有骨架字段——没有 targets/开关位就不套用。
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    if (j && j.ok && j.targets) applyStatus(j);
  } catch (e) { /* 服务不可达：页面顶部连接状态已说明 */ }
}

function openLightbox(src) { $('#lb-img').src = src; $('#lightbox').hidden = false; }

function bindEvents() {
  if (IS_OVERLAY) {
    // 正文全部点击穿透，只有原生工具条能发指令；这里只保留工具条动作需要的入口。
    $('#btn-re-ask').onclick = () => { if (currentItem) askLLM(currentItem, true); };
  } else if (!IS_PC) {
    $('#tabbar').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b) switchView(b.dataset.view);
    });
    $('#snap-dock').onclick = () => requestSnap();
    $('#snap-dock').onkeydown = e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); requestSnap(); }
    };
    // 只认按钮本身：以前点正文任意位置也会截屏，实际很少用得上，还容易误触。
    $('#btn-save').onclick = () => {
      if (!currentItem || !currentItem.image) { toast('本条没有图片'); return; }
      const a = document.createElement('a');
      a.href = currentItem.image;
      a.download = 'question-' + (currentItem.id || Date.now()) + '.jpg';
      a.click();
      toast('已保存到下载');
    };
    $('#btn-copy').onclick = () => {
      const t = currentItem ? (currentItem.text || '').trim() : '';
      if (!t) { toast('本条没有文字（可在设置里开启 OCR）'); return; }
      copyText(t);
    };
    $('#btn-re-ask').onclick = () => { if (currentItem) askLLM(currentItem, true); };
    $('#fit-note').onclick = () => {
      stopBounceScroll();
      $('#q-card').hidden = false;
      $('#fit-note').hidden = true;
    };
    $('#lightbox').onclick = () => { $('#lightbox').hidden = true; };
  } else {
    // PC 控制台：窗口操作 / 日志工具 / 连接地址
    const overlayBtn = $('#btn-pc-overlay');
    if (overlayBtn) overlayBtn.onclick = async () => {
      if (overlayBtn.disabled) return;
      const want = overlayBtn.getAttribute('aria-pressed') !== 'true';
      overlayBtn.disabled = true;
      try {
        const response = await fetch('/api/overlay', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ visible: want }),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || '悬浮窗未能打开');
        renderOverlayButton(result);
        if (result.protection === 'monitor') toast('悬浮窗已显示（系统不支持隐藏，截图里显示为黑块）');
      } catch (err) {
        toast(errMsg(err));
        refreshPcStatus();
      } finally {
        overlayBtn.disabled = false;
      }
    };
    const networks = $('#pc-networks');
    networks.onclick = async e => {
      const button = e.target.closest('[data-pick]');
      if (!button || button.getAttribute('aria-pressed') === 'true' || networks.getAttribute('aria-busy') === 'true') return;
      networks.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch('/api/conn', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pick: Number(button.dataset.pick) }),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error('network change failed');
        await refreshPcStatus();
      } catch (err) {
        toast('网络切换失败，请检查服务连接');
      } finally {
        networks.removeAttribute('aria-busy');
      }
    };
    const updateTooltips = () => document.querySelectorAll('.hint-tooltip:not([hidden])').forEach(pop => {
      positionTooltip(pop.parentElement.querySelector('button'), pop);
    });
    document.addEventListener('scroll', updateTooltips, true);
    window.addEventListener('resize', updateTooltips);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') document.querySelectorAll('.hint-tooltip:not([hidden])').forEach(pop => { pop.hidden = true; });
    });
    $('#pc-url').onclick = () => { const t = $('#pc-url').textContent; if (t) copyText(t); };
    $('#btn-log-copy').onclick = () => {
      const txt = [...$('#pc-log').children].map(d => d.textContent).join('\n');
      if (!txt) { toast('日志还是空的'); return; }
      copyText(txt);
    };
    $('#btn-log-clear').onclick = () => { const b = $('#pc-log'); b.innerHTML = ''; logLines = 0; };
    $('#lightbox').onclick = () => { $('#lightbox').hidden = true; };
  }

  // 「关闭电脑端」两段式确认：点一下变提示，3 秒内再点一次才真发
  let quitArmed = null;
  document.addEventListener('click', async e => {
    const b = e.target.closest('#btn-pc-quit');
    if (!b) return;
    const txt = [...b.childNodes].find(n => n.nodeType === 3);
    if (!txt) return;
    if (!quitArmed) {
      b.dataset.label = txt.textContent;
      txt.textContent = '确认退出';
      toast('再次点击确认退出，手机连接也会断开');
      quitArmed = setTimeout(() => { txt.textContent = b.dataset.label; quitArmed = null; }, 3000);
      return;
    }
    clearTimeout(quitArmed);
    quitArmed = null;
    txt.textContent = b.dataset.label || '退出电脑端';
    try {
      const r = await fetch('/api/quit', { method: 'POST' });
      const j = await r.json();
      toast(j.ok ? '电脑端正在退出' : (j.error || '电脑端未响应'));
    } catch (e2) { toast('请求失败：电脑端是否在线？'); }
  });

  document.addEventListener('click', async e => {
    const cal = e.target.closest('#btn-calibrate, #btn-pc-calibrate');
    if (cal) {
      try {
        const r = await fetch('/api/calibrate', { method: 'POST' });
        const j = await r.json();
        toast(j.ok ? '已在电脑屏幕上弹出框选窗口，拉框后点「确定」' : (j.error || '电脑端未响应'));
      } catch (err) { toast('请求失败：电脑端是否在线？'); }
      return;
    }
    const rc = e.target.closest('#btn-reconnect');
    if (rc) { connect(); toast('正在重连…'); return; }
    const ft = e.target.closest('#btn-feedback-test');
    if (ft) {
      const ok = vibrateAns('C');
      speakAns({ answer: 'C', analysis: '这是语音测试，第三个选项对应三下震动。' });
      toast(ok ? '已触发：语音 + 三下震动' : '已触发语音；当前环境不支持震动（iPhone 请装配套 App）');
      return;
    }
    const ex = e.target.closest('#btn-export');
    if (ex) { exportSettings(); return; }
    const im = e.target.closest('#btn-import');
    if (im) { $('#file-import').click(); return; }
    const ch = e.target.closest('#btn-clear-hist');
    if (ch) {
      if (!history.length) { toast('历史已为空'); return; }
      history = []; saveHistory(); renderHistory(); toast('已清空');
      return;
    }
  });
  // 高级设置开合状态记本地；编辑中收到广播则等失焦后再重渲染
  const adv = () => $('#adv-settings');
  try {
    const applyAdv = () => { const a = adv(); if (a) a.open = localStorage.getItem('qa_adv_open') === '1'; };
    applyAdv();
    document.addEventListener('toggle', e => {
      if (e.target.id === 'adv-settings') {
        try { localStorage.setItem('qa_adv_open', e.target.open ? '1' : '0'); } catch (err) {}
      }
    }, true);
  } catch (e) {}

  document.addEventListener('focusout', () => {
    if (setRerenderPending && !isEditingSettings()) {
      setRerenderPending = false;
      renderSettingsAll();
    }
  });
}

// 原生工具条 → 网页的唯一指令入口（窗口正文是整窗鼠标穿透的，收不到点击）。
function overlayAction(action) {
  if (action === 'retry') {
    // 悬浮窗的「识别」= 截屏+识别：先让电脑截一张，内容推回来即自动识别；
    // 不连手机也能独立使用（连了手机时，同一条仍只向上游发一次）。
    askAfterSnap = true;
    requestSnap();
    return;
  }
  if (action === 'up' || action === 'down') {
    const step = Math.max(90, Math.round(window.innerHeight * 0.58));
    window.scrollBy({ top: action === 'up' ? -step : step, behavior: 'smooth' });
    return;
  }
  if (action === 'pause') {
    overlayPaused = true;
    askingId = null;
    clearTimeout(resumeT);
    if (curAsk) { try { curAsk.abort(); } catch (e) {} }
    return;
  }
  if (action === 'resume') {
    overlayPaused = false;
    askingId = null;
    // 先要一次最新内容：隐藏期间可能漏收推送（或推送发送失败被服务端摘掉连接），
    // 只靠本地 currentItem 会把旧内容再识别一遍，看起来就像"总是无法确定"。
    send({ type: 'resync' });
    // 等 sync 回来再决定：新内容会由 showQuestion 自动识别；没有新内容（或旧服务端
    // 不回 sync）时再识别当前内容，避免同一条发两次请求。
    clearTimeout(resumeT);
    resumeT = setTimeout(() => {
      if (!currentItem || answeredId === currentItem.id || !settings.autoAsk) return;
      if (askingId === currentItem.id) return;
      askLLM(currentItem);
    }, 600);
  }
}
window.overlayAction = overlayAction;

function init() {
  if (IS_OVERLAY) {
    document.body.classList.add('overlay');
    document.documentElement.classList.add('overlay');
    const empty = $('#empty-state');
    if (empty) empty.textContent = '等待内容…';
  } else if (IS_PC) {
    document.body.classList.add('pc');
    $('#pc-root').hidden = false;
  }
  if (!IS_OVERLAY) renderSettingsAll();
  bindEvents();
  if (!IS_PC && !IS_OVERLAY) switchView('main');
  connect();
  startHeartbeat();
}
// 纯逻辑自测用（tools/vibtest.js 在 node 里跑）：node 里没有 document，跳过初始化，
// 只导出纯函数供断言；浏览器里没有 module，这段不生效。
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    buildVibPattern, optionPulses, questionHasOptions, parseAnswers, stripThinking, jsonCandidates,
    vibrateAns, playVibration, vibLevel, VIB_LEVELS, DEF_SETTINGS, DEFAULT_PROMPT,
    normalizeChannels, setSettings: s => { settings = s; }
  };
} else {
  init();
}
