(() => {
  'use strict';

  const elements = {
    raceDate: document.getElementById('raceDate'),
    loadRacesButton: document.getElementById('loadRacesButton'),
    raceList: document.getElementById('raceList'),
    raceCountBadge: document.getElementById('raceCountBadge'),
    predictionSection: document.getElementById('predictionSection'),
    predictionMeta: document.getElementById('predictionMeta'),
    predictionTableBody: document.getElementById('predictionTableBody'),
    filterSummary: document.getElementById('filterSummary'),
    reportSection: document.getElementById('reportSection'),
    reportContent: document.getElementById('reportContent'),
    emptyState: document.getElementById('emptyState'),
    apiStatus: document.getElementById('apiStatus'),
    apiStatusText: document.getElementById('apiStatusText'),
    notification: document.getElementById('notification'),
    notificationText: document.getElementById('notificationText'),
  };

  const state = { date: null, activeRaceKey: null };
  const toast = window.bootstrap ? new window.bootstrap.Toast(elements.notification, { delay: 5000 }) : null;

  function hongKongToday() {
    const fields = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date()).reduce((result, part) => ({ ...result, [part.type]: part.value }), {});
    return `${fields.year}-${fields.month}-${fields.day}`;
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    }[character]));
  }

  function toFiniteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function percent(value, digits = 1) {
    const number = toFiniteNumber(value);
    return number === null ? '—' : `${(number * 100).toFixed(digits)}%`;
  }

  function decimal(value, digits = 3) {
    const number = toFiniteNumber(value);
    return number === null ? '—' : number.toFixed(digits);
  }

  function showNotification(message, isError = false) {
    elements.notificationText.textContent = message;
    elements.notification.classList.toggle('text-bg-danger', isError);
    elements.notification.classList.toggle('text-bg-primary', !isError);
    if (toast) toast.show();
  }

  function setLoading(button, loading) {
    button.disabled = loading;
    button.querySelector('.button-text').textContent = loading ? '載入中' : '載入賽程';
    button.querySelector('.spinner-border').classList.toggle('d-none', !loading);
  }

  async function request(path, responseType = 'json') {
    const response = await fetch(path, { headers: { Accept: responseType === 'text' ? 'text/markdown' : 'application/json' } });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const error = await response.json();
        message = error.detail || message;
      } catch (_) {
        // Keep the generic HTTP status if an intermediary produced non-JSON output.
      }
      throw new Error(message);
    }
    return responseType === 'text' ? response.text() : response.json();
  }

  function clearRaceView() {
    state.activeRaceKey = null;
    elements.predictionSection.classList.add('d-none');
    elements.reportSection.classList.add('d-none');
    elements.emptyState.classList.remove('d-none');
    elements.predictionTableBody.replaceChildren();
    elements.reportContent.replaceChildren();
    elements.filterSummary.textContent = '';
  }

  function renderRaceButtons(races) {
    elements.raceList.replaceChildren();
    elements.raceCountBadge.textContent = `${races.length} 場可用`;
    if (!races.length) {
      const noRace = document.createElement('p');
      noRace.className = 'text-secondary mb-0';
      noRace.textContent = '此日期尚未有完成的預測工件。請在賽前 T-5 流程完成後再載入。';
      elements.raceList.append(noRace);
      return;
    }
    races.forEach((race) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-outline-primary race-button';
      button.dataset.raceKey = `${race.date}-${race.course}-${race.race_no}`;
      button.innerHTML = `<span class="d-block small opacity-75">${escapeHtml(race.course)}</span>第 ${race.race_no} 場`;
      button.addEventListener('click', () => loadRace(race, button));
      elements.raceList.append(button);
    });
  }

  function renderPrediction(payload, race) {
    const prediction = payload.prediction || {};
    const rows = Array.isArray(prediction.predictions) ? prediction.predictions : [];
    elements.predictionTableBody.replaceChildren();
    if (!rows.length) {
      elements.predictionTableBody.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-4">prediction.json 沒有可呈現的馬匹列。</td></tr>';
    }

    rows.forEach((row) => {
      const ev = toFiniteNumber(row.ev_per_unit);
      const kelly = row.kelly_quarter_fraction_capped ?? row.kelly_full_fraction;
      const isPositiveEv = ev !== null && ev > 0;
      const tr = document.createElement('tr');
      if (isPositiveEv) tr.classList.add('positive-ev');
      const winProbability = toFiniteNumber(row.predicted_win_probability);
      tr.innerHTML = `
        <td class="fw-semibold">${escapeHtml(row.horse_no ?? '—')}</td>
        <td><span class="fw-semibold">${escapeHtml(row.horse_name ?? '未命名')}</span><br><small class="text-secondary">排名 ${escapeHtml(row.rank ?? '—')}</small></td>
        <td class="text-end"><span class="fw-semibold">${percent(winProbability)}</span><div class="probability-bar ms-auto mt-1"><span style="width:${Math.max(0, Math.min(100, (winProbability || 0) * 100))}%"></span></div></td>
        <td class="text-end">${percent(row.predicted_place_probability)}</td>
        <td class="text-end ${isPositiveEv ? 'ev-positive' : ''}">${decimal(ev)}</td>
        <td class="text-end">${percent(kelly, 2)}</td>
        <td><small>${escapeHtml(row.win_suggestion || row.suggestion || '—')}</small></td>
      `;
      elements.predictionTableBody.append(tr);
    });

    const raceMeta = prediction.race || {};
    const model = prediction.model || 'V10 預測模型';
    elements.predictionMeta.textContent = `${race.date} · ${race.course} 第 ${race.race_no} 場 · ${raceMeta.distance_m || '—'}米 · ${raceMeta.going || '場地資料未提供'} · ${model}`;
    const filter = payload.high_probability_filter;
    elements.filterSummary.innerHTML = filter
      ? `<span class="badge text-bg-primary">篩選候選 ${escapeHtml(filter.selection_count ?? '—')} 匹</span>`
      : '<span class="badge text-bg-light border">未提供雙策略篩選檔</span>';
    elements.predictionSection.classList.remove('d-none');
  }

  function renderReport(markdown) {
    if (!window.marked) throw new Error('Marked.js 未能載入，無法渲染 Markdown 報告。');
    const html = window.marked.parse(markdown, { headerIds: false, mangle: false });
    elements.reportContent.innerHTML = window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
    elements.reportSection.classList.remove('d-none');
  }

  async function loadRace(race, button) {
    const raceKey = `${race.date}-${race.course}-${race.race_no}`;
    state.activeRaceKey = raceKey;
    document.querySelectorAll('.race-button').forEach((item) => item.classList.toggle('active', item === button));
    clearRaceView();
    state.activeRaceKey = raceKey;
    button.disabled = true;
    button.textContent = `${race.course} 第 ${race.race_no} 場 · 載入中`;
    try {
      const base = `/api/prediction/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}/${encodeURIComponent(race.race_no)}`;
      const reportUrl = `/api/report/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}/${encodeURIComponent(race.race_no)}`;
      const [prediction, report] = await Promise.all([request(base), request(reportUrl, 'text')]);
      if (state.activeRaceKey !== raceKey) return;
      renderPrediction(prediction, race);
      renderReport(report);
      elements.emptyState.classList.add('d-none');
    } catch (error) {
      if (state.activeRaceKey === raceKey) {
        showNotification(`無法載入 ${race.course} 第 ${race.race_no} 場：${error.message}`, true);
        elements.emptyState.classList.remove('d-none');
      }
    } finally {
      button.disabled = false;
      button.innerHTML = `<span class="d-block small opacity-75">${escapeHtml(race.course)}</span>第 ${race.race_no} 場`;
    }
  }

  async function loadRaces() {
    const requestedDate = elements.raceDate.value;
    if (!requestedDate) {
      showNotification('請先選擇有效賽日。', true);
      return;
    }
    setLoading(elements.loadRacesButton, true);
    clearRaceView();
    elements.raceList.innerHTML = '<p class="text-secondary mb-0">正在讀取已保存的賽程工件…</p>';
    try {
      const payload = await request(`/api/races/${encodeURIComponent(requestedDate)}`);
      state.date = requestedDate;
      renderRaceButtons(Array.isArray(payload.races) ? payload.races : []);
    } catch (error) {
      elements.raceList.innerHTML = '<p class="text-danger mb-0">無法讀取賽程。</p>';
      elements.raceCountBadge.textContent = '讀取失敗';
      showNotification(`無法載入賽程：${error.message}`, true);
    } finally {
      setLoading(elements.loadRacesButton, false);
    }
  }

  async function checkHealth() {
    try {
      const health = await request('/health');
      if (health.status !== 'ok' || health.read_only !== true) throw new Error('健康檢查未回傳唯讀狀態。');
      elements.apiStatus.classList.add('online');
      elements.apiStatusText.textContent = '唯讀 API 已連線';
    } catch (_) {
      elements.apiStatus.classList.add('offline');
      elements.apiStatusText.textContent = 'API 暫時無法連線';
    }
  }

  elements.raceDate.value = hongKongToday();
  elements.loadRacesButton.addEventListener('click', loadRaces);
  checkHealth();
})();
