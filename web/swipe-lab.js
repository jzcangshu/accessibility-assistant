'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const tuning = window.SwipeTuning;
  let info, editor, socket = null, ready = false, draftTimer = null, lastCount = 0, held = null, heldUntil = 0;
  let requestId = 0, ending = false, hasStarted = false;
  const message = text => { $('#lab-message').textContent = text; };
  function startScreen(visible) {
    $('#lab-start').hidden = !visible;
    $('.lab-header').inert = $('.lab-layout').inert = visible;
    if (visible && !$('#lab-begin').disabled) $('#lab-begin').focus();
  }

  function send(type, parameters) {
    if (!ready || !socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type, parameters, requestId: ++requestId }));
  }
  function preview(parameters) {
    clearTimeout(draftTimer); message('正在试用当前参数，尚未应用到软件。');
    draftTimer = setTimeout(() => send('preview', parameters), 100);
  }
  function controls(active) {
    $('#lab-dot').classList.toggle('active', active);
    $('#lab-connection').textContent = active ? '整屏试调中 · 鼠标截图已暂停' : '调试已停止';
    $('#lab-end').textContent = active ? '结束调试' : '重新全屏调试';
    $('#lab-end').disabled = false;
    $('#lab-reset').disabled = !active;
  }

  function render(data) {
    if (data.count > lastCount) { held = { ...data, ...data.last_trigger }; heldUntil = performance.now() + 1800; }
    lastCount = data.count;
    const useHeld = held && performance.now() < heldUntil && !data.pressed && data.metrics.dist === 0;
    const current = useHeld ? held : data, metrics = current.metrics;
    $('#lab-distance').textContent = Math.round(metrics.dist);
    $('#lab-speed').textContent = Math.round(metrics.speed);
    $('#lab-span').textContent = metrics.span.toFixed(2);
    $('#lab-need').textContent = Math.round(data.need);
    const progress = Math.min(1, metrics.dist / Math.max(1, data.need));
    $('#lab-bar').style.transform = `scaleX(${progress})`;
    $('.lab-progress').setAttribute('aria-valuenow', Math.round(progress * 100));
    let verdict = '松开鼠标键，横向移动。';
    if (data.pressed) verdict = '当前是拖拽操作：松开鼠标键后再试。';
    else if (useHeld || data.fired) verdict = `第 ${data.count} 次触发成功 · ${Math.round(metrics.dist)} 像素 · ${Math.round(metrics.speed)} 像素/秒`;
    else if (data.cooldown > 0) verdict = `冷却中，还需 ${data.cooldown.toFixed(1)} 秒。`;
    else if (!data.armed) verdict = `停住鼠标 ${data.still_time} 秒，再开始下一次滑动。`;
    else if (metrics.dist > 0) {
      const failed = data.checks.filter(check => !check.ok).map(check => check.label);
      verdict = failed.length ? '尚未通过：' + failed.join('、') : '所有条件已满足。';
    }
    $('#lab-verdict').textContent = verdict;
    $('#lab-verdict').classList.toggle('pass', !!useHeld || data.fired);
    $('#lab-checks').replaceChildren(...current.checks.map(check => {
      const row = document.createElement('div'); row.className = 'lab-check' + (check.ok ? ' pass' : '');
      [['mark', check.ok ? '✓' : '·'], ['', check.label], ['actual', `${check.value} ${check.unit}`], ['limit', check.limit]]
        .forEach(([cls, text]) => { const span = document.createElement('span'); span.className = cls; span.textContent = text; row.append(span); });
      return row;
    }));
    $('#lab-count').textContent = data.count;
    if (data.recent.length) $('#lab-recent').replaceChildren(...data.recent.map(record => {
      const row = document.createElement('div'); row.className = 'lab-record';
      [`#${record.sequence}`, `${Math.round(record.dist)} 像素 / ${record.span.toFixed(2)} 秒`, `${Math.round(record.speed)} 像素/秒`, record.at]
        .forEach(text => { const span = document.createElement('span'); span.textContent = text; row.append(span); });
      return row;
    }));
    else $('#lab-recent').innerHTML = '<p class="lab-empty">成功触发后，可在这里对照距离和速度。</p>';
    const trail = current.trail || [], scale = 300 / Math.max(data.need, ...trail.map(point => Math.abs(point[0])), 1);
    $('#lab-trail').setAttribute('d', trail.map(([x, y], i) => `${i ? 'L' : 'M'}${350 + x * scale},${Math.max(8, Math.min(122, 65 + y * scale))}`).join(' '));
  }

  async function finish(text = '调试已结束，鼠标截图已恢复。') {
    if (ending) return;
    ending = true; ready = false; clearTimeout(draftTimer);
    const previous = socket; socket = null;
    if (previous) previous.close();
    controls(false); startScreen(false); message(text + ' 当前参数仍保留，可继续调整、复制或应用。');
    $('#lab-start-message').textContent = text;
    if (document.fullscreenElement) {
      try { await document.exitFullscreen(); } catch (_) { /* 浏览器已退出时无需再次处理。 */ }
    }
    ending = false;
  }

  async function begin() {
    $('#lab-begin').disabled = true;
    $('#lab-end').disabled = true;
    try {
      await document.documentElement.requestFullscreen();
      if (!document.fullscreenElement) throw new Error('Fullscreen request did not enter fullscreen');
      startScreen(false);
      const current = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/swipe-lab`);
      socket = current;
      current.onmessage = event => {
        if (socket !== current) return;
        let data;
        try { data = JSON.parse(event.data); } catch (_) { return; }
        if (data.type === 'ready') {
          ready = true; controls(true); lastCount = 0; held = null;
          if (hasStarted) {
            try { send('preview', editor.read()); message('继续试用当前参数，应用后保存到软件。'); }
            catch (error) { editor.setValues(data.parameters); message(error.message); }
          } else {
            editor.setValues(data.parameters); message('当前为软件已保存的参数。调整后可立即试用。');
          }
          hasStarted = true; $('#lab-end').focus();
        } else if (data.type === 'sample') render(data);
        else if (data.type === 'error') message(data.error);
      };
      current.onclose = event => {
        console.debug('调试连接关闭', event.code, event.reason);
        if (socket === current) finish(ready ? '连接已断开，调试已停止。' : '调试未能开始，请确认没有其他调试页正在运行。');
      };
      current.onerror = () => message('调试连接失败，请检查本地服务。');
    } catch (error) {
      console.warn('无法启动全屏调试', error.name, error.message);
      $('#lab-start-message').textContent = '无法进入整屏模式，请允许浏览器全屏后重试。';
      message('无法进入整屏模式，请重新点击开始。');
    } finally { $('#lab-begin').disabled = $('#lab-end').disabled = false; }
  }
  $('#lab-begin').onclick = begin;
  $('#lab-end').onclick = () => socket ? finish() : begin();
  document.addEventListener('fullscreenchange', () => {
    document.body.dataset.screenMode = document.fullscreenElement ? 'fullscreen' : 'window';
    if (!document.fullscreenElement && socket) finish();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && socket) finish('调试页已离开前台，鼠标截图已恢复。');
  });
  window.addEventListener('pagehide', () => { if (socket) socket.close(); });
  $('#lab-reset').onclick = () => { held = null; lastCount = 0; send('reset'); };
  $('#lab-defaults').onclick = () => { editor.setValues(info.defaults); preview(editor.read()); };
  $('#lab-copy').onclick = async () => {
    try { await tuning.copy(editor.read()); message('参数已复制，可在软件中点击“导入参数”。'); }
    catch (error) { message(error.message); }
  };
  $('#lab-apply').onclick = async () => {
    const apply = $('#lab-apply'); apply.disabled = true;
    try {
      const params = editor.read();
      clearTimeout(draftTimer);
      await tuning.api('/api/swipe/parameters', params); send('preview', params);
      message('已应用到软件。结束调试后，截图将使用这组参数。');
    } catch (error) { message(error.message); }
    finally { apply.disabled = false; }
  };
  tuning.api('/api/swipe/parameters').then(result => {
    info = result; editor = tuning.createEditor($('#lab-editor'), info, preview, message);
    $('#lab-need').textContent = info.parameters.min_distance || info.default_distance;
    $('#lab-begin').disabled = false; $('#lab-begin').focus(); $('#lab-start-message').textContent = '准备就绪。';
    message('开始调试后，移动鼠标即可看到实时结果。');
  }).catch(error => { $('#lab-start-message').textContent = error.message; message(error.message); });
})();
