'use strict';

// 软件设置与独立调试页共用参数编辑器和导入格式。
window.SwipeTuning = (() => {
  const fields = [
    ['min_distance', '滑动距离', '像素', 10, '移动多远才触发；较短更轻松，也更容易误触。'],
    ['min_speed', '最低速度', '像素/秒', 10, '低于这个速度的日常移动不触发。'],
    ['max_speed', '最高速度', '像素/秒', 10, '高于这个速度的快速甩动不触发。'],
    ['cooldown', '触发冷却', '秒', 0.1, '两次触发之间至少等待多久。'],
    ['max_slope', '纵向偏移容忍', '倍', 0.01, '纵向偏移与横向距离的比值；越大越宽松。'],
    ['max_path', '路径偏移容忍', '倍', 0.05, '实际路径与直线距离的比值；越接近 1 越要求笔直。'],
    ['min_span', '最短滑动时长', '秒', 0.05, '过滤过于短促的动作。'],
    ['still_time', '停止后恢复时间', '秒', 0.05, '触发后停住鼠标多久，才接受下一次滑动。'],
  ];

  async function api(url, data) {
    const response = await fetch(url, data === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    });
    const text = await response.text();
    let result;
    try { result = JSON.parse(text); } catch (_) { throw new Error(text || '服务暂时不可用'); }
    if (!response.ok || !result.ok) throw new Error(result.error || '操作失败，请稍后重试');
    return result;
  }

  function createEditor(host, info, onChange = () => {}, onInvalid = () => {}) {
    const inputs = new Map();
    const auto = document.createElement('label');
    auto.className = 'tune-auto';
    const automatic = document.createElement('input');
    automatic.type = 'checkbox';
    auto.append(automatic, document.createTextNode('按屏幕宽度自动计算距离'));
    host.append(auto);
    const grid = document.createElement('div'); grid.className = 'tune-fields'; host.append(grid);
    const advanced = document.createElement('details'); advanced.className = 'tune-advanced';
    const summary = document.createElement('summary'); summary.textContent = '高级容错参数'; advanced.append(summary);
    const more = document.createElement('div'); more.className = 'tune-fields'; advanced.append(more); host.append(advanced);

    function read() {
      const values = {};
      for (const [key, label] of fields) {
        const input = inputs.get(key).number;
        if (key === 'min_distance' && automatic.checked) { values[key] = null; continue; }
        const value = Number(input.value), bounds = info.bounds[key];
        if (!input.value.trim() || !Number.isFinite(value) || value < bounds[0] || value > bounds[1]) {
          throw new Error(`${label}需在 ${bounds[0]}～${bounds[1]} 之间`);
        }
        values[key] = value;
      }
      if (values.min_speed > values.max_speed) throw new Error('最低速度不能高于最高速度');
      return values;
    }
    function notify() { try { onChange(read()); } catch (error) { onInvalid(error.message); } }
    function updateAuto() {
      const { number, range } = inputs.get('min_distance');
      number.disabled = range.disabled = automatic.checked;
      if (automatic.checked) number.value = range.value = info.default_distance;
    }
    fields.forEach(([key, label, unit, step, hint], index) => {
      const block = document.createElement('div'); block.className = 'tune-field';
      const head = document.createElement('label'); head.className = 'tune-field-head';
      const caption = document.createElement('span'); caption.textContent = label;
      const group = document.createElement('span'); group.className = 'tune-number';
      const number = document.createElement('input');
      number.type = 'number'; number.min = info.bounds[key][0]; number.max = info.bounds[key][1]; number.step = step;
      number.setAttribute('aria-label', `${label}（${unit}）`);
      const suffix = document.createElement('span'); suffix.textContent = unit;
      group.append(number, suffix); head.append(caption, group);
      const range = document.createElement('input'); range.type = 'range';
      range.min = number.min; range.max = number.max; range.step = step;
      range.setAttribute('aria-label', `${label}滑块`);
      const note = document.createElement('p'); note.textContent = hint; note.className = 'tune-hint';
      block.append(head, range, note); (index < 4 ? grid : more).append(block);
      inputs.set(key, { number, range });
      range.oninput = () => { number.value = range.value; notify(); };
      number.oninput = () => { if (number.value) range.value = number.value; notify(); };
    });
    automatic.onchange = () => { updateAuto(); notify(); };
    function setValues(values) {
      automatic.checked = values.min_distance === null;
      for (const [key] of fields) {
        const { number, range } = inputs.get(key);
        number.value = range.value = values[key] === null ? info.default_distance : values[key];
      }
      updateAuto();
    }
    setValues(info.parameters);
    return { read, setValues };
  }

  function documentFor(parameters) { return { type: 'swipe-parameters', version: 1, parameters }; }
  async function copy(parameters) {
    const text = JSON.stringify(documentFor(parameters), null, 2);
    if (navigator.clipboard) {
      try { await navigator.clipboard.writeText(text); return; }
      catch (_) { /* 部分桌面窗口限制剪贴板接口，继续尝试选中文本复制。 */ }
    }
    const field = document.createElement('textarea'); field.value = text; field.style.position = 'fixed';
    field.style.opacity = '0'; document.body.append(field); field.select();
    const copied = document.execCommand('copy'); field.remove();
    if (!copied) throw new Error('复制失败，请检查剪贴板权限');
  }

  function dialog(title) {
    const modal = document.createElement('dialog'); modal.className = 'tuning-dialog';
    modal.setAttribute('aria-label', title);
    modal.innerHTML = '<header class="tune-dialog-head"><h2></h2><button type="button" class="tune-icon" aria-label="关闭">×</button></header><div class="tune-dialog-body"></div><p class="tune-message" role="status"></p><footer class="tune-dialog-actions"></footer>';
    modal.querySelector('h2').textContent = title;
    modal.querySelector('.tune-icon').onclick = () => modal.close();
    modal.addEventListener('close', () => modal.remove(), { once: true });
    document.body.append(modal); modal.showModal();
    return { modal, body: modal.querySelector('.tune-dialog-body'), footer: modal.querySelector('footer'),
      message: modal.querySelector('.tune-message') };
  }
  function button(text, action, primary = false) {
    const b = document.createElement('button'); b.type = 'button';
    b.className = 'tune-btn' + (primary ? ' primary' : ''); b.textContent = text; b.onclick = action;
    return b;
  }

  async function openParameters() {
    const view = dialog('滑动触发参数'); view.message.textContent = '正在读取参数…';
    try {
      const info = await api('/api/swipe/parameters');
      if (!view.modal.isConnected) return;
      const editor = createEditor(view.body, info, () => { view.message.textContent = '修改后点击“应用参数”保存。'; },
        text => { view.message.textContent = text; });
      view.message.textContent = '参数保存后立即生效，开关状态保持不变。';
      const apply = button('应用参数', async () => {
        try {
          const params = editor.read(); apply.disabled = true;
          await api('/api/swipe/parameters', params); view.message.textContent = '参数已保存并应用。';
        } catch (error) { view.message.textContent = error.message; }
        finally { apply.disabled = false; }
      }, true);
      view.footer.append(button('恢复默认', () => { editor.setValues(info.defaults); view.message.textContent = '已恢复默认值，点击“应用参数”保存。'; }),
        button('取消', () => view.modal.close()), apply);
    } catch (error) { view.message.textContent = error.message; }
  }

  function openImport() {
    const view = dialog('导入滑动参数');
    const label = document.createElement('label'); label.className = 'tune-import-label';
    label.textContent = '粘贴从调试页复制的参数';
    const area = document.createElement('textarea'); area.rows = 10; area.spellcheck = false;
    area.setAttribute('aria-label', '滑动参数'); area.placeholder = '在此粘贴参数';
    label.append(area); view.body.append(label);
    let busy = false;
    const paste = button('粘贴并应用', async () => {
      try { area.value = await navigator.clipboard.readText(); await applyText(); }
      catch (_) { view.message.textContent = '无法读取剪贴板，请直接粘贴到上方文本框，再点击“导入并应用”。'; area.focus(); }
    });
    const apply = button('导入并应用', applyText, true);
    async function applyText() {
      if (busy) return;
      busy = true; paste.disabled = apply.disabled = true;
      try {
        let data;
        try { data = JSON.parse(area.value); } catch (_) { throw new Error('参数格式不正确，请重新复制完整内容。'); }
        await api('/api/swipe/import', data); view.message.textContent = '参数已导入并应用。';
      } catch (error) { view.message.textContent = error.message; }
      finally { busy = false; paste.disabled = apply.disabled = false; }
    }
    view.footer.append(paste, button('取消', () => view.modal.close()), apply); area.focus();
  }

  function createEntry() {
    const entry = document.createElement('div'); entry.className = 'swipe-settings-entry';
    const actions = document.createElement('div'); actions.className = 'swipe-entry-actions';
    const status = document.createElement('p'); status.className = 'tune-hint'; status.setAttribute('role', 'status');
    const open = button('打开全屏调试', async () => {
      open.disabled = true;
      try { await api('/api/swipe-lab/open', {}); status.textContent = '调试页已在浏览器打开，点击“开始全屏调试”即可。'; }
      catch (error) { status.textContent = error.message; }
      finally { open.disabled = false; }
    });
    actions.append(button('参数设置', openParameters), open, button('导入参数', openImport));
    entry.append(actions, status); return entry;
  }
  return { api, createEditor, documentFor, copy, createEntry };
})();
