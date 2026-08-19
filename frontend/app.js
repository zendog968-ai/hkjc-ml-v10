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
    overseasDeepSection: document.getElementById('overseasDeepSection'),
    overseasDeepContent: document.getElementById('overseasDeepContent'),
    overseasRaceSelector: document.getElementById('overseasRaceSelector'),
    loadOverseasDeepButton: document.getElementById('loadOverseasDeepButton'),
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

  function overseasScore(value) {
    const number = toFiniteNumber(value);
    return number === null ? '—' : number.toFixed(2);
  }

  function overseasProbability(value) {
    const number = toFiniteNumber(value);
    return number === null ? '—' : `${(number * 100).toFixed(1)}%`;
  }

  function overseasEv(value) {
    const number = toFiniteNumber(value);
    return number === null ? '—' : `${number > 0 ? '+' : ''}${(number * 100).toFixed(1)}%`;
  }

  function overseasAvailabilityLabel(value) {
    const labels = {
      available_public: '公開可用',
      unavailable_paid_or_restricted: '訂閱／受限',
      unavailable_parse: '暫未取得',
      not_requested: '未請求',
    };
    return labels[value] || '未提供';
  }

  function renderOverseasDeepBacktest(summary) {
    const status = summary?.status;
    if (status !== 'ready' || !Array.isArray(summary?.cohorts) || !summary.cohorts.length) {
      return `<section class="overseas-backtest mt-3"><div class="d-flex flex-wrap justify-content-between gap-2 align-items-center"><div><strong>海外深度研究代理歷史回測</strong><br><small class="text-secondary">預測準確度：N/A</small></div><span class="badge backtest-na">資料不足</span></div><p class="small text-secondary mb-0 mt-2">${escapeHtml(summary?.warning || '尚未有不可變賽前深度決策與官方賽果配對；系統不以賽後重抓的RPR／TS資料回測。')}</p></section>`;
    }
    const rows = summary.cohorts.map((cohort) => `<tr><td><code>${escapeHtml(cohort.proxy_version || '—')}</code></td><td>${escapeHtml(cohort.events)}</td><td>${backtestPercent(cohort.top1_win_rate)}</td><td>${backtestPercent(cohort.top3_contains_winner_rate)}</td><td>${decimal(cohort.multi_class_brier, 4)}</td><td>${decimal(cohort.log_loss, 4)}</td><td><span class="badge ${cohort.sample_status === 'exploratory' ? 'backtest-exploratory' : 'backtest-ready'}">${escapeHtml(cohort.sample_status === 'exploratory' ? '探索性' : '資料充足')}</span></td></tr>`).join('');
    return `<section class="overseas-backtest mt-3"><div class="d-flex flex-wrap justify-content-between gap-2 align-items-center"><div><strong>海外深度研究代理歷史回測</strong><br><small class="text-secondary">只按不可變賽前決策、來源雜湊與官方賽果配對；不跨代理版本混合。</small></div><span class="badge backtest-ready">合格事件 ${escapeHtml(summary.strict_event_count)}</span></div><div class="table-responsive mt-2"><table class="table table-sm mb-1"><thead><tr><th>代理版本</th><th>事件</th><th>Top-1</th><th>Top-3</th><th>Brier</th><th>Log loss</th><th>樣本</th></tr></thead><tbody>${rows}</tbody></table></div><p class="small text-secondary mb-0">${escapeHtml(summary.warning || '')}</p></section>`;
  }

  function renderOverseasDeep(payload, backtestSummary = null) {
    elements.overseasDeepContent.replaceChildren();
    const race = payload?.race && typeof payload.race === 'object' ? payload.race : {};
    const starters = Array.isArray(payload?.starters) ? payload.starters.slice() : [];
    const availability = payload?.field_availability && typeof payload.field_availability === 'object' ? payload.field_availability : {};
    const market = payload?.market_research && typeof payload.market_research === 'object' ? payload.market_research : {};
    const marketReady = market.status === 'complete' && market.n6_status === 'disabled_non_hk' && market.research_only === true;
    const scrapeStatus = payload?.scrape_run?.status;
    if (!['complete', 'partial'].includes(scrapeStatus) || payload?.n6_integration?.status !== 'disabled_non_hk' || !starters.length) {
      const detail = payload?.n6_integration?.status !== 'disabled_non_hk'
        ? '海外資料的 N6 隔離狀態無效；系統已安全停止呈現。'
        : (payload?.scrape_run?.source_notes || '等待可驗證的公開海外賽事資料工件。');
      elements.overseasDeepContent.innerHTML = `<div class="overseas-deep-awaiting rounded-3"><strong>S1海外深度資料暫未就緒</strong><br><span>${escapeHtml(detail)}</span></div>${renderOverseasDeepBacktest(backtestSummary)}`;
      elements.overseasDeepSection.classList.remove('d-none');
      return;
    }
    starters.sort((left, right) => (toFiniteNumber(left.deep_rank) || 9999) - (toFiniteNumber(right.deep_rank) || 9999));
    const rows = starters.map((row) => {
      const distance = row.distance_runs === null || row.distance_runs === undefined ? '—' : `${escapeHtml(row.distance_wins ?? 0)}/${escapeHtml(row.distance_runs)}`;
      const going = row.similar_going_runs === null || row.similar_going_runs === undefined ? '—' : `${escapeHtml(row.similar_going_wins ?? 0)}/${escapeHtml(row.similar_going_runs)}`;
      const pedigree = [row.sire, row.dam, row.damsire].filter(Boolean).map(escapeHtml).join(' / ') || '—';
      const entry = row.market_research && typeof row.market_research === 'object' ? row.market_research : {};
      const positiveEv = marketReady && toFiniteNumber(entry.win_ev) !== null && toFiniteNumber(entry.win_ev) > 0;
      return `<tr class="${positiveEv ? 'overseas-positive-ev' : ''}"><td class="fw-semibold">#${escapeHtml(row.deep_rank ?? '—')}</td><td>${escapeHtml(row.runner_no ?? '—')}</td><td><strong>${escapeHtml(row.horse_name || '未命名')}</strong><br><small class="text-secondary">檔 ${escapeHtml(row.draw_no ?? '—')} · ${escapeHtml(row.data_completeness || 'partial')}</small></td><td class="text-end">${escapeHtml(row.racing_post_rating ?? '—')}</td><td class="text-end">${escapeHtml(row.top_speed_rating ?? '—')}</td><td>${escapeHtml(distance)}</td><td>${escapeHtml(going)}</td><td><small>${pedigree}</small></td><td class="text-end overseas-score">${overseasScore(row.deep_composite_score)}</td><td class="text-end">${marketReady ? oddsNumber(entry.win_odds) : '—'}</td><td class="text-end ${positiveEv ? 'overseas-ev-positive' : ''}">${marketReady ? overseasEv(entry.win_ev) : '—'}</td><td class="text-end">${marketReady ? oddsNumber(entry.place_odds) : '—'}</td><td class="text-end">${marketReady ? overseasEv(entry.place_ev) : '—'}</td><td class="text-end">${marketReady ? overseasProbability(entry.kelly_fraction) : '—'}</td></tr>`;
    }).join('');
    const warnings = Array.isArray(payload?.scrape_run?.source_notes?.split(' | ')) ? payload.scrape_run.source_notes.split(' | ') : [];
    elements.overseasDeepContent.innerHTML = `
      <div class="overseas-deep-meta mb-3 d-flex flex-wrap justify-content-between gap-2"><div><strong>${escapeHtml(race.venue || '海外賽事')} ${escapeHtml(race.simulcast_code || 'S1')}-${escapeHtml(race.race_no || '—')}</strong><br><small class="text-secondary">${escapeHtml(race.hkt_start_time || '開跑時間待確認')} · ${escapeHtml(race.distance_text || '—')} · ${escapeHtml(race.going || 'Going 待確認')} · ${escapeHtml(race.declared_runners || starters.length)} 匹</small></div><div class="d-flex gap-2"><span class="badge ${scrapeStatus === 'complete' ? 'overseas-complete' : 'overseas-partial'}">${scrapeStatus === 'complete' ? '公開賽卡完整' : '公開賽卡部分可用'}</span><span class="badge overseas-n6-disabled">N6 已停用（海外賽事）</span></div></div>
      <div class="overseas-deep-status mb-3"><span>RPR：${escapeHtml(overseasAvailabilityLabel(availability.rpr))}</span><span>TS：${escapeHtml(overseasAvailabilityLabel(availability.top_speed))}</span><span>步速：${escapeHtml(overseasAvailabilityLabel(availability.pace_setup))}</span><span>HKJC 賠率：${marketReady ? '官方完整匹配' : '未能安全計算'}</span></div>
      ${marketReady ? `<div class="overseas-market-status mb-3"><strong>HKJC 官方 Win／Place 快照：</strong>${escapeHtml(market.captured_at_utc || '—')}；${escapeHtml(market.matched_runner_count || '—')}/${escapeHtml(market.expected_runner_count || '—')} 匹身份匹配；位置派彩 ${escapeHtml(market.place_dividends || '—')} 個。</div>` : '<div class="overseas-market-status overseas-market-blocked mb-3">HKJC 市場資料未完成嚴格身份／隔離檢查；Win、Place、EV 與 Kelly 保持空白。</div>'}
      <div class="table-responsive"><table class="table table-hover align-middle overseas-deep-table"><thead><tr><th>研究排名</th><th>馬號</th><th>馬匹</th><th class="text-end">RPR</th><th class="text-end">TS</th><th>路程勝／跑</th><th>相近 Going 勝／跑</th><th>血統（父／母／外祖父）</th><th class="text-end">公開綜合分</th><th class="text-end">HKJC Win</th><th class="text-end">Win EV</th><th class="text-end">HKJC Place</th><th class="text-end">Place EV</th><th class="text-end">Kelly</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="overseas-deep-disclaimer mb-0">${scrapeStatus === 'partial' ? '此場公開出馬或評分欄位未完整，僅展示已核實馬匹；未取得資料不作推斷。 ' : ''}${marketReady ? 'EV／Kelly 使用未校準海外公開深度分數所生成的研究性場內機率代理；正值只表示此代理相對當刻 HKJC 賠率的數學結果，不構成勝率、回報或投注保證。 ' : ''}公開綜合分只按當次可驗證的 RPR、TS 及公開 At The Races 路程、相近 Going、馬場平滑勝率正規化；缺失欄位重新加權而不填補。它不是 V10.2 機率、EV、Kelly 或 N6 Neural Score，亦不構成投注指令。${warnings.length ? ` 來源狀態：${escapeHtml(warnings.join('；'))}` : ''}</p>${renderOverseasDeepBacktest(backtestSummary)}`;
    elements.overseasDeepSection.classList.remove('d-none');
  }

  async function loadOverseasDeep(requestedDate, requestedRaceNo = Number(elements.overseasRaceSelector?.value || 1)) {
    const raceNo = Number.isInteger(requestedRaceNo) && requestedRaceNo >= 1 && requestedRaceNo <= 20 ? requestedRaceNo : 1;
    try {
      const [payload, backtestSummary] = await Promise.all([
        request(`/api/overseas-deep/${encodeURIComponent(requestedDate)}/S1/${encodeURIComponent(raceNo)}`),
        request('/api/overseas-deep/backtest').catch(() => ({ status: 'not_available', strict_event_count: 0, cohorts: [], warning: '海外深度回測摘要暫不可讀取；不會顯示未驗證數值。' })),
      ]);
      if (elements.raceDate.value === requestedDate) renderOverseasDeep(payload, backtestSummary);
    } catch (error) {
      elements.overseasDeepContent.innerHTML = `<div class="overseas-deep-awaiting rounded-3"><strong>S1海外深度資料暫不可讀取</strong><br><span>${escapeHtml(error.message)}</span></div>`;
      elements.overseasDeepSection.classList.remove('d-none');
    }
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
      void loadOverseasDeep(requestedDate);
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
  elements.loadOverseasDeepButton.addEventListener('click', async () => {
    const requestedDate = elements.raceDate.value;
    if (!requestedDate) {
      showNotification('請先選擇有效賽日。', true);
      return;
    }
    setLoading(elements.loadOverseasDeepButton, true);
    try {
      await loadOverseasDeep(requestedDate, Number(elements.overseasRaceSelector.value));
    } finally {
      setLoading(elements.loadOverseasDeepButton, false);
    }
  });
  checkHealth();
  void loadOverseasDeep(elements.raceDate.value);
})();
