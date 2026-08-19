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
    doubleTrioSection: document.getElementById('doubleTrioSection'),
    doubleTrioContent: document.getElementById('doubleTrioContent'),
    reportSection: document.getElementById('reportSection'),
    reportContent: document.getElementById('reportContent'),
    emptyState: document.getElementById('emptyState'),
    apiStatus: document.getElementById('apiStatus'),
    apiStatusText: document.getElementById('apiStatusText'),
    n6Status: document.getElementById('n6Status'),
    n6StatusText: document.getElementById('n6StatusText'),
    n6ModelSummary: document.getElementById('n6ModelSummary'),
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

  function setN6Status(status, detail = '') {
    elements.n6Status.classList.remove('online', 'offline');
    if (status === 'available') {
      elements.n6Status.classList.add('online');
      elements.n6StatusText.textContent = 'N6 神經訊號已整合';
      return;
    }
    if (status === 'unavailable') {
      elements.n6Status.classList.add('offline');
      elements.n6StatusText.textContent = detail || 'N6 訊號暫不可用';
      return;
    }
    elements.n6StatusText.textContent = 'N6 訊號待載入';
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
    elements.doubleTrioSection.classList.add('d-none');
    elements.reportSection.classList.add('d-none');
    elements.emptyState.classList.remove('d-none');
    elements.predictionTableBody.replaceChildren();
    elements.doubleTrioContent.replaceChildren();
    elements.reportContent.replaceChildren();
    elements.filterSummary.textContent = '';
    elements.n6ModelSummary.textContent = '';
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
    const n6 = prediction.n6_integration || {};
    const n6Model = n6.model && typeof n6.model === 'object' ? n6.model : null;
    setN6Status(n6.status, n6.message);
    if (!rows.length) {
      elements.predictionTableBody.innerHTML = '<tr><td colspan="10" class="text-center text-secondary py-4">prediction.json 沒有可呈現的馬匹列。</td></tr>';
    }

    rows.forEach((row) => {
      const ev = toFiniteNumber(row.ev_per_unit);
      const kelly = row.kelly_quarter_fraction_capped ?? row.kelly_full_fraction;
      const isPositiveEv = ev !== null && ev > 0;
      const tr = document.createElement('tr');
      if (isPositiveEv) tr.classList.add('positive-ev');
      if (row.joint_consensus === true) tr.classList.add('joint-consensus');
      const winProbability = toFiniteNumber(row.predicted_win_probability);
      const neuralScore = toFiniteNumber(row.n6_neural_score);
      const n6Rank = toFiniteNumber(row.n6_rank);
      const jointLabel = n6.status !== 'available'
        ? '<span class="text-secondary small">N6 暫不可用</span>'
        : (row.joint_consensus === true
          ? '<span class="badge joint-badge">綜合聯合推薦</span>'
          : '<span class="text-secondary small">聯合觀察</span>');
      tr.innerHTML = `
        <td class="fw-semibold">${escapeHtml(row.horse_no ?? '—')}</td>
        <td><span class="fw-semibold">${escapeHtml(row.horse_name ?? '未命名')}</span><br><small class="text-secondary">V10 排名 ${escapeHtml(row.rank ?? '—')}</small></td>
        <td class="text-end"><span class="fw-semibold">${percent(winProbability)}</span><div class="probability-bar ms-auto mt-1"><span style="width:${Math.max(0, Math.min(100, (winProbability || 0) * 100))}%"></span></div></td>
        <td class="text-end">${percent(row.predicted_place_probability)}</td>
        <td class="text-end ${isPositiveEv ? 'ev-positive' : ''}">${decimal(ev)}</td>
        <td class="text-end">${percent(kelly, 2)}</td>
        <td class="text-end neural-score">${neuralScore === null ? '—' : neuralScore.toFixed(2)}</td>
        <td class="text-end">${n6Rank === null ? '—' : `#${Math.trunc(n6Rank)}`}</td>
        <td>${jointLabel}</td>
        <td><small>${escapeHtml(row.win_suggestion || row.suggestion || '—')}</small></td>
      `;
      elements.predictionTableBody.append(tr);
    });

    const raceMeta = prediction.race || {};
    const model = prediction.model || 'V10 預測模型';
    elements.predictionMeta.textContent = `${race.date} · ${race.course} 第 ${race.race_no} 場 · ${raceMeta.distance_m || '—'}米 · ${raceMeta.going || '場地資料未提供'} · ${model}`;
    elements.n6ModelSummary.textContent = n6Model
      ? `N6 ${escapeHtml(n6Model.production_release || '生產模型')} · ${escapeHtml(n6Model.input_dim || '—')} 維輸入 · 市場：${escapeHtml(n6Model.market_feature_policy || '未提供')}`
      : (n6.status === 'available' ? 'N6 模型資訊未提供；神經評分仍以 N6 服務回應為準。' : 'N6 模型資訊待服務可用後載入。');
    const filter = payload.high_probability_filter;
    elements.filterSummary.innerHTML = filter
      ? `<span class="badge text-bg-primary">篩選候選 ${escapeHtml(filter.selection_count ?? '—')} 匹</span>`
      : '<span class="badge text-bg-light border">未提供雙策略篩選檔</span>';
    elements.predictionSection.classList.remove('d-none');
  }

  function hkd(value) {
    const amount = toFiniteNumber(value);
    return amount === null ? '—' : `HK$${amount.toLocaleString('en-HK', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
  }

  function oddsNumber(value) {
    const number = toFiniteNumber(value);
    return number === null ? '—' : number.toFixed(1);
  }

  function signedPercent(value) {
    const number = toFiniteNumber(value);
    if (number === null) return '快照未齊';
    return `${number > 0 ? '+' : ''}${(number * 100).toFixed(1)}%`;
  }

  function oddsMovementClass(status) {
    if (status === 'large_shortening') return 'odds-movement-shortening';
    if (status === 'large_drift') return 'odds-movement-drift';
    if (status === 'stable') return 'odds-movement-stable';
    return 'odds-movement-unavailable';
  }

  function backtestPercent(value) {
    const number = toFiniteNumber(value);
    return number === null ? 'N/A' : `${(number * 100).toFixed(1)}%`;
  }

  function renderDoubleTrioBacktest(summary) {
    const card = document.createElement('section');
    card.className = 'double-trio-backtest mt-3';
    const cohorts = summary?.cohorts && typeof summary.cohorts === 'object' ? Object.entries(summary.cohorts) : [];
    if (summary?.readiness !== 'ready' || !cohorts.length) {
      card.innerHTML = `<div class="d-flex flex-wrap justify-content-between gap-2 align-items-center"><div><strong>四匹複式歷史回測</strong><br><small class="text-secondary">勝率與回報率：N/A</small></div><span class="badge backtest-na">資料不足</span></div><p class="small text-secondary mb-0 mt-2">${escapeHtml(summary?.notice || '尚未有可稽核的賽前四匹決策、官方頭三與派彩結算資料；系統不會以賽後重算或測試資料代替。')}</p>`;
      return card;
    }
    const rows = cohorts.map(([sha, cohort]) => `<tr><td><code>${escapeHtml(sha.slice(0, 12))}</code></td><td>${escapeHtml(cohort.settled_event_count)}</td><td>${escapeHtml(cohort.hit_count)}</td><td>${backtestPercent(cohort.hit_rate)}</td><td class="${toFiniteNumber(cohort.roi) !== null && cohort.roi > 0 ? 'positive-ev' : ''}">${backtestPercent(cohort.roi)}</td><td><span class="badge ${cohort.status === 'exploratory' ? 'backtest-exploratory' : 'backtest-ready'}">${escapeHtml(cohort.status === 'exploratory' ? '探索性' : '資料充足')}</span></td></tr>`).join('');
    card.innerHTML = `<div class="d-flex flex-wrap justify-content-between gap-2 align-items-center"><div><strong>四匹複式歷史回測</strong><br><small class="text-secondary">每個模型 SHA-256 cohort 獨立結算，不跨版本混合。</small></div><span class="badge backtest-ready">已結算 ${escapeHtml(summary.settled_record_count)} 個事件</span></div><div class="table-responsive mt-2"><table class="table table-sm mb-1"><thead><tr><th>模型版本</th><th>事件</th><th>命中</th><th>勝率</th><th>ROI</th><th>樣本標籤</th></tr></thead><tbody>${rows}</tbody></table></div><p class="small text-secondary mb-0">${escapeHtml(summary.notice || '僅使用不可變賽前決策與官方結算資料。')}</p>`;
    return card;
  }

  function renderDoubleTrio(strategy, backtest) {
    elements.doubleTrioContent.replaceChildren();
    const events = Array.isArray(strategy?.events) ? strategy.events : [];
    const readyEvents = events.filter((event) => event?.status === 'ready');
    if (!readyEvents.length) {
      const notice = document.createElement('div');
      notice.className = 'double-trio-awaiting rounded-3';
      notice.innerHTML = `<strong>孖T策略暫未就緒</strong><br><span>${escapeHtml(strategy?.message || events[0]?.message || '等待香港賽馬會官方指定場次及兩關完整的 V10＋N6 聯合排名。')}</span>`;
      elements.doubleTrioContent.append(notice);
      elements.doubleTrioContent.append(renderDoubleTrioBacktest(backtest));
      elements.doubleTrioSection.classList.remove('d-none');
      return;
    }

    readyEvents.forEach((event) => {
      const eventCard = document.createElement('article');
      eventCard.className = 'double-trio-event mb-3';
      const plan = event.combination_plan || {};
      const monitoringSummary = event.odds_monitoring_summary || {};
      const legs = Array.isArray(event.legs) ? event.legs : [];
      const alertSummary = monitoringSummary.status === 'available'
        ? (monitoringSummary.large_movement_count > 0
          ? `<span class="badge odds-summary-alert">賠率大幅變動 ${escapeHtml(monitoringSummary.large_movement_count)} 匹</span>`
          : '<span class="badge odds-summary-stable">賠率變動未達大幅門檻</span>')
        : '<span class="badge odds-summary-unavailable">T-15／T-5 快照待齊</span>';
      const legHtml = legs.map((leg) => {
        const selections = Array.isArray(leg.selections) ? leg.selections : [];
        const monitor = leg.odds_monitoring || {};
        const movementRows = Array.isArray(monitor.selections) ? monitor.selections : [];
        const movementByHorse = new Map(movementRows.map((row) => [String(row.horse_no), row]));
        const snapshotMeta = monitor.status === 'available'
          ? '<span class="small text-secondary">官方 T-15 → T-5 獨贏快照</span>'
          : '<span class="small text-secondary">官方賠率快照未齊</span>';
        const selectionHtml = selections.map((runner) => {
          const movement = movementByHorse.get(String(runner.horse_no)) || {};
          const status = movement.movement_status || 'snapshot_unavailable';
          const label = movement.movement_label || '賠率快照未齊';
          return `
            <div class="double-trio-selection">
              <div class="d-flex align-items-center gap-2">
                <span class="double-trio-selection-number">${escapeHtml(runner.horse_no)}</span>
                <div><span class="fw-semibold">${escapeHtml(runner.horse_name)}</span><br><small class="text-secondary">聯合排名 #${escapeHtml(runner.joint_rank)} · Neural ${decimal(runner.n6_neural_score, 2)}</small></div>
              </div>
              <div class="text-end"><small class="d-block text-secondary">V10 EV ${decimal(runner.v10_ev_per_unit)}</small><small class="odds-movement ${oddsMovementClass(status)}">${escapeHtml(label)} ${escapeHtml(signedPercent(movement.odds_change_ratio))}</small><small class="d-block text-secondary">T-15 ${oddsNumber(movement.odds_t_minus_15)} → T-5 ${oddsNumber(movement.odds_t_minus_5)}</small></div>
            </div>`;
        }).join('');
        return `<div class="col-lg-6"><section class="double-trio-leg"><div class="d-flex justify-content-between align-items-center gap-2 mb-2"><div class="double-trio-leg-title">第 ${escapeHtml(leg.leg_no)} 關 · 第 ${escapeHtml(leg.race_no)} 場</div>${snapshotMeta}</div>${selectionHtml}</section></div>`;
      }).join('');
      eventCard.innerHTML = `
        <div class="d-flex flex-wrap justify-content-between gap-2 mb-3">
          <div><span class="badge text-bg-light border me-2">${escapeHtml(event.display_label || '官方孖T')}</span><span class="small text-secondary">${escapeHtml(event.pool_event_code || '')}</span></div>
          <div class="d-flex flex-wrap align-items-center justify-content-end gap-2"><span class="small text-secondary">每關 4 匹 → C(4,3) = 4 組</span>${alertSummary}</div>
        </div>
        <div class="row g-3">${legHtml}</div>
        <div class="double-trio-capital mt-3 d-flex flex-wrap justify-content-between align-items-center gap-2">
          <div><strong>精選四匹複式 · ${escapeHtml(plan.total_bet_combinations)} 注</strong><br><small>每關四組三馬組合，兩關交叉組合。</small></div>
          <div class="text-lg-end"><small class="d-block">每注 ${hkd(plan.unit_stake_hkd)}</small><strong class="fs-5">總建議本金 ${hkd(plan.total_suggested_capital_hkd)}</strong></div>
        </div>`;
      elements.doubleTrioContent.append(eventCard);
    });
    const disclaimer = document.createElement('p');
    disclaimer.className = 'double-trio-disclaimer mb-0';
    disclaimer.textContent = '賠率監控只呈現既有官方 T-15／T-5 快照的公開變動，並不代表資金來源、內幕資訊、勝出保證或投注指令。本區不會提交、傳送或執行投注。';
    elements.doubleTrioContent.append(disclaimer);
    elements.doubleTrioContent.append(renderDoubleTrioBacktest(backtest));
    elements.doubleTrioSection.classList.remove('d-none');
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
    setN6Status('pending');
    state.activeRaceKey = raceKey;
    button.disabled = true;
    button.textContent = `${race.course} 第 ${race.race_no} 場 · 載入中`;
    try {
      const base = `/api/prediction/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}/${encodeURIComponent(race.race_no)}`;
      const reportUrl = `/api/report/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}/${encodeURIComponent(race.race_no)}`;
      const doubleTrioUrl = `/api/double-trio/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}`;
      const doubleTrioBacktestUrl = '/api/double-trio/backtest';
      const [prediction, report, doubleTrio, doubleTrioBacktest] = await Promise.all([
        request(base),
        request(reportUrl, 'text'),
        request(doubleTrioUrl).catch(() => ({ status: 'official_data_unavailable', events: [], message: '孖T策略資料暫不可讀取。' })),
        request(doubleTrioBacktestUrl).catch(() => ({ readiness: 'not_ready', cohorts: {}, notice: '歷史回測摘要暫不可讀取；不會顯示未驗證數值。' })),
      ]);
      if (state.activeRaceKey !== raceKey) return;
      renderPrediction(prediction, race);
      renderDoubleTrio(doubleTrio, doubleTrioBacktest);
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
