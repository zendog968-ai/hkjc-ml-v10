from pathlib import Path

ROOT = Path('/home/ubuntu/hkjc_v10_database/frontend')
index = ROOT / 'index.html'
app = ROOT / 'app.js'
css = ROOT / 'style.css'

html = index.read_text(encoding='utf-8')
html = html.replace(
    '        <div class="table-responsive">\n          <table class="table table-hover align-middle mb-0 prediction-table">\n            <thead>',
    '''        <div class="prediction-tabs-wrap px-3 px-md-4 pt-3" aria-label="單場預測視圖切換">
          <div id="predictionTabs" class="prediction-tabs nav nav-pills flex-nowrap overflow-auto gap-2" role="tablist">
            <button type="button" class="nav-link active" data-prediction-tab="summary" role="tab" aria-selected="true">綜合預測</button>
            <button type="button" class="nav-link" data-prediction-tab="drift" role="tab" aria-selected="false">臨場落飛</button>
            <button type="button" class="nav-link" data-prediction-tab="kelly" role="tab" aria-selected="false">凱利注碼</button>
            <button type="button" class="nav-link" data-prediction-tab="qualitative" role="tab" aria-selected="false">晨操質性</button>
          </div>
        </div>
        <div class="table-responsive">
          <table class="table table-hover align-middle mb-0 prediction-table">
            <thead id="predictionTableHead">''',
)
index.write_text(html, encoding='utf-8')

text = app.read_text(encoding='utf-8')
text = text.replace(
    "    predictionTableBody: document.getElementById('predictionTableBody'),",
    "    predictionTableHead: document.getElementById('predictionTableHead'),\n    predictionTableBody: document.getElementById('predictionTableBody'),\n    predictionTabs: document.getElementById('predictionTabs'),",
)
text = text.replace(
    "  const state = { date: null, activeRaceKey: null };",
    "  const state = { date: null, activeRaceKey: null, activePredictionTab: 'summary', predictionPayload: null, predictionRace: null };",
)
text = text.replace(
    "    state.activeRaceKey = null;\n    elements.predictionSection.classList.add('d-none');",
    "    state.activeRaceKey = null;\n    state.activePredictionTab = 'summary';\n    state.predictionPayload = null;\n    state.predictionRace = null;\n    elements.predictionSection.classList.add('d-none');",
)
start = text.index('  function renderPrediction(payload, race) {')
end = text.index('\n  function hkd(value) {', start)
replacement = r'''  function displayValue(value, fallback = '—') {
    return value === null || value === undefined || value === '' ? fallback : escapeHtml(value);
  }

  function firstDefined(row, keys) {
    for (const key of keys) {
      const parts = key.split('.');
      let value = row;
      for (const part of parts) value = value && typeof value === 'object' ? value[part] : undefined;
      if (value !== null && value !== undefined && value !== '') return value;
    }
    return null;
  }

  function smartMoneyLabel(row) {
    const flag = firstDefined(row, ['smart_money_flag', 'gate_money_drop_flag', 'odds_drift.smart_money_flag']);
    if (flag === true || String(flag).toLowerCase() === 'true') return '<span class="badge smart-money-badge">Smart Money</span>';
    if (flag === false || String(flag).toLowerCase() === 'false') return '<span class="text-secondary small">未觸發</span>';
    return '<span class="text-secondary small">—</span>';
  }

  function renderPredictionTable(payload) {
    const prediction = payload?.prediction || {};
    const rows = Array.isArray(prediction.predictions) ? prediction.predictions : [];
    const n6 = prediction.n6_integration || {};
    const tab = state.activePredictionTab;
    const headers = {
      summary: '<th>馬號</th><th>馬匹</th><th class="text-end">Win 機率</th><th class="text-end">Place 機率</th><th class="text-end">EV</th><th class="text-end">Kelly 注碼比例</th><th class="text-end">Neural Score</th><th class="text-end">N6 排名</th><th>綜合聯合推薦</th><th>模型提示</th>',
      drift: '<th>馬號</th><th>馬匹</th><th class="text-end">T-15 Win</th><th class="text-end">T-5 Win</th><th class="text-end">跌幅率</th><th class="text-end">WIN/PLA 背馳</th><th>Smart Money</th>',
      kelly: '<th>馬號</th><th>馬匹</th><th class="text-end">校準勝率</th><th class="text-end">最新獨贏</th><th class="text-end">EV</th><th class="text-end">0.25x Kelly</th><th class="text-end">風控狀態</th>',
      qualitative: '<th>馬號</th><th>馬匹</th><th class="text-end">晨操分</th><th>晨操評語</th><th>試閘強度</th><th>末段加速</th><th>馬房旗標</th>',
    };
    elements.predictionTableHead.innerHTML = headers[tab] || headers.summary;
    elements.predictionTableBody.replaceChildren();
    if (!rows.length) {
      const colspan = tab === 'summary' ? 10 : (tab === 'drift' ? 7 : 7);
      elements.predictionTableBody.innerHTML = `<tr><td colspan="${colspan}" class="text-center text-secondary py-4">目前沒有可呈現的資料。</td></tr>`;
      return;
    }
    rows.forEach((row) => {
      const ev = toFiniteNumber(row.ev_per_unit ?? row.win_ev_per_unit ?? row.win_ev);
      const kelly = row.kelly_quarter_fraction_capped ?? row.kelly_fraction ?? row.kelly_full_fraction;
      const isPositiveEv = ev !== null && ev > 0;
      const tr = document.createElement('tr');
      if (isPositiveEv) tr.classList.add('positive-ev');
      if (row.joint_consensus === true) tr.classList.add('joint-consensus');
      if (tab === 'drift') {
        const t15 = firstDefined(row, ['win_odds_t15', 'odds_t_minus_15', 'odds_drift.win_t15']);
        const t5 = firstDefined(row, ['win_odds_t5', 'odds_t_minus_5', 'odds_drift.win_t5', 'win_odds']);
        const drift = firstDefined(row, ['win_drift_ratio', 'odds_drop_ratio', 'odds_drift.win_drift_ratio']);
        const divergence = firstDefined(row, ['win_place_divergence', 'odds_drift.win_place_divergence']);
        tr.innerHTML = `<td class="fw-semibold">${displayValue(row.horse_no)}</td><td><span class="fw-semibold">${displayValue(row.horse_name, '未命名')}</span><br><small class="text-secondary">V10 排名 ${displayValue(row.rank)}</small></td><td class="text-end">${oddsNumber(t15)}</td><td class="text-end">${oddsNumber(t5)}</td><td class="text-end ${toFiniteNumber(drift) !== null && Number(drift) < 0 ? 'odds-drift-down' : ''}">${signedPercent(drift)}</td><td class="text-end">${signedPercent(divergence)}</td><td>${smartMoneyLabel(row)}</td>`;
      } else if (tab === 'kelly') {
        const probability = firstDefined(row, ['calibrated_win_probability', 'predicted_win_probability']);
        const odds = firstDefined(row, ['latest_win_odds', 'win_odds', 'market_odds']);
        const stake = firstDefined(row, ['fractional_kelly_stake_fraction', 'kelly_quarter_fraction_capped', 'kelly_fraction']);
        const status = firstDefined(row, ['kelly_status', 'risk_status']);
        tr.innerHTML = `<td class="fw-semibold">${displayValue(row.horse_no)}</td><td><span class="fw-semibold">${displayValue(row.horse_name, '未命名')}</span></td><td class="text-end">${percent(probability)}</td><td class="text-end">${oddsNumber(odds)}</td><td class="text-end ${isPositiveEv ? 'ev-positive' : ''}">${decimal(ev)}</td><td class="text-end">${percent(stake, 2)}</td><td class="text-end">${displayValue(status, stake === 0 ? '不下注' : '—')}</td>`;
      } else if (tab === 'qualitative') {
        const condition = firstDefined(row, ['condition_score', 'trackwork_condition_score', 'qualitative.condition_score']);
        const comment = firstDefined(row, ['trackwork_comment', 'morning_trackwork', 'qualitative.trackwork_comment']);
        const effort = firstDefined(row, ['effort_level', 'barrier_trial_effort_level', 'qualitative.effort_level']);
        const surge = firstDefined(row, ['surge_rating', 'barrier_trial_surge_rating', 'qualitative.surge_rating']);
        const ambition = firstDefined(row, ['ambition_flag', 'stable_ambition_flag', 'qualitative.ambition_flag']);
        const ambitionLabel = ambition === true || String(ambition).toLowerCase() === 'true' ? '<span class="badge stable-flag-badge">進取</span>' : (ambition === false || String(ambition).toLowerCase() === 'false' ? '<span class="text-secondary small">未標記</span>' : '—');
        tr.innerHTML = `<td class="fw-semibold">${displayValue(row.horse_no)}</td><td><span class="fw-semibold">${displayValue(row.horse_name, '未命名')}</span></td><td class="text-end">${decimal(condition, 2)}</td><td><small>${displayValue(comment)}</small></td><td>${displayValue(effort)}</td><td>${displayValue(surge)}</td><td>${ambitionLabel}</td>`;
      } else {
        const winProbability = toFiniteNumber(row.predicted_win_probability);
        const neuralScore = toFiniteNumber(row.n6_neural_score);
        const n6Rank = toFiniteNumber(row.n6_rank);
        const jointLabel = n6.status !== 'available'
          ? '<span class="text-secondary small">N6 暫不可用</span>'
          : (row.joint_consensus === true ? '<span class="badge joint-badge">綜合聯合推薦</span>' : '<span class="text-secondary small">聯合觀察</span>');
        tr.innerHTML = `<td class="fw-semibold">${displayValue(row.horse_no)}</td><td><span class="fw-semibold">${displayValue(row.horse_name, '未命名')}</span><br><small class="text-secondary">V10 排名 ${displayValue(row.rank)}</small></td><td class="text-end"><span class="fw-semibold">${percent(winProbability)}</span><div class="probability-bar ms-auto mt-1"><span style="width:${Math.max(0, Math.min(100, (winProbability || 0) * 100))}%"></span></div></td><td class="text-end">${percent(row.predicted_place_probability)}</td><td class="text-end ${isPositiveEv ? 'ev-positive' : ''}">${decimal(ev)}</td><td class="text-end">${percent(kelly, 2)}</td><td class="text-end neural-score">${neuralScore === null ? '—' : neuralScore.toFixed(2)}</td><td class="text-end">${n6Rank === null ? '—' : `#${Math.trunc(n6Rank)}`}</td><td>${jointLabel}</td><td><small>${displayValue(row.win_suggestion || row.suggestion)}</small></td>`;
      }
      elements.predictionTableBody.append(tr);
    });
  }

  function renderPrediction(payload, race) {
    state.predictionPayload = payload;
    state.predictionRace = race;
    state.activePredictionTab = 'summary';
    elements.predictionTabs?.querySelectorAll('[data-prediction-tab]').forEach((button) => {
      const active = button.dataset.predictionTab === state.activePredictionTab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    const prediction = payload.prediction || {};
    const n6 = prediction.n6_integration || {};
    const n6Model = n6.model && typeof n6.model === 'object' ? n6.model : null;
    setN6Status(n6.status, n6.message);
    renderPredictionTable(payload);
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
'''
text = text[:start] + replacement + text[end:]
marker = "  async function loadRace(race, button) {"
listener = """  elements.predictionTabs?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-prediction-tab]');
    if (!button || !state.predictionPayload) return;
    state.activePredictionTab = button.dataset.predictionTab || 'summary';
    elements.predictionTabs.querySelectorAll('[data-prediction-tab]').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-selected', String(active));
    });
    renderPredictionTable(state.predictionPayload);
  });

"""
text = text.replace(marker, listener + marker)
app.write_text(text, encoding='utf-8')

style = css.read_text(encoding='utf-8')
style += '''
.prediction-tabs-wrap { background: #fbfdff; }
.prediction-tabs { scrollbar-width: thin; }
.prediction-tabs .nav-link {
  flex: 0 0 auto;
  min-height: 2.75rem;
  padding: .65rem .9rem;
  border: 1px solid #dbe5ef;
  color: #334155;
  background: #fff;
  font-weight: 700;
  white-space: nowrap;
}
.prediction-tabs .nav-link.active { color: #fff; background: #1d4ed8; border-color: #1d4ed8; }
.smart-money-badge { color: #7c2d12; background: #ffedd5; border: 1px solid #fdba74; }
.stable-flag-badge { color: #065f46; background: #d1fae5; border: 1px solid #6ee7b7; }
.odds-drift-down { color: #b91c1c; font-weight: 700; }
@media (max-width: 575.98px) {
  .prediction-tabs-wrap { padding-left: .75rem !important; padding-right: .75rem !important; }
  .prediction-tabs .nav-link { min-height: 3rem; padding: .75rem .85rem; font-size: .88rem; }
  .prediction-table { min-width: 680px; }
}
'''
css.write_text(style, encoding='utf-8')
print('patched index.html app.js style.css')
પ = None
