from pathlib import Path

root = Path('/home/ubuntu/hkjc_v10_database/frontend')
html_path = root / 'index.html'
js_path = root / 'app.js'
css_path = root / 'style.css'

html = html_path.read_text(encoding='utf-8')
needle = '''            </div>
            <p class="form-text mb-0 mt-2">只會列出已成功生成 <code>prediction.json</code> 的場次。</p>
'''
replacement = '''            </div>
            <div class="daily-toolbar d-flex flex-wrap gap-2 mt-3" aria-label="全日唯讀聚合工具">
              <button id="dailyValueButton" class="btn btn-outline-success daily-tool-button" type="button" disabled>
                <span class="button-text">全日高 EV 戰報</span><span class="spinner-border spinner-border-sm d-none" aria-hidden="true"></span>
              </button>
              <button id="doubleTrioHubButton" class="btn btn-outline-primary daily-tool-button" type="button" disabled>
                <span class="button-text">雙 T／孖 T 策略矩陣</span><span class="spinner-border spinner-border-sm d-none" aria-hidden="true"></span>
              </button>
            </div>
            <p id="dailyToolbarHint" class="form-text mb-0 mt-2">請先載入賽日；工具只讀取當日已保存的預測及官方孖T工件。</p>
'''
if needle not in html:
    raise SystemExit('HTML toolbar anchor not found')
html = html.replace(needle, replacement, 1)
modal_anchor = '''  <div id="notification" class="toast align-items-center border-0 position-fixed bottom-0 end-0 m-3" role="status" aria-live="polite" aria-atomic="true">'''
modal = '''  <div class="modal fade" id="dailyAggregationModal" tabindex="-1" aria-labelledby="dailyAggregationTitle" aria-hidden="true">
    <div class="modal-dialog modal-dialog-scrollable modal-lg modal-fullscreen-sm-down">
      <div class="modal-content daily-aggregation-modal">
        <div class="modal-header">
          <div><p id="dailyAggregationKicker" class="section-kicker mb-1">READ-ONLY DAILY VIEW</p><h2 id="dailyAggregationTitle" class="h5 mb-0">全日工具</h2></div>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="關閉"></button>
        </div>
        <div id="dailyAggregationContent" class="modal-body" aria-live="polite"></div>
        <div class="modal-footer py-2"><button type="button" class="btn btn-outline-secondary daily-modal-close" data-bs-dismiss="modal">關閉</button></div>
      </div>
    </div>
  </div>

'''
if modal_anchor not in html:
    raise SystemExit('HTML modal anchor not found')
html = html.replace(modal_anchor, modal + modal_anchor, 1)
html_path.write_text(html, encoding='utf-8')

js = js_path.read_text(encoding='utf-8')
js = js.replace("    loadRacesButton: document.getElementById('loadRacesButton'),", "    loadRacesButton: document.getElementById('loadRacesButton'),\n    dailyValueButton: document.getElementById('dailyValueButton'),\n    doubleTrioHubButton: document.getElementById('doubleTrioHubButton'),\n    dailyToolbarHint: document.getElementById('dailyToolbarHint'),\n    dailyAggregationModal: document.getElementById('dailyAggregationModal'),\n    dailyAggregationTitle: document.getElementById('dailyAggregationTitle'),\n    dailyAggregationKicker: document.getElementById('dailyAggregationKicker'),\n    dailyAggregationContent: document.getElementById('dailyAggregationContent'),", 1)
js = js.replace("  const state = { date: null, activeRaceKey: null, activePredictionTab: 'summary', predictionPayload: null, predictionRace: null };", "  const state = { date: null, loadedRaces: [], activeRaceKey: null, activePredictionTab: 'summary', predictionPayload: null, predictionRace: null };", 1)
js = js.replace("  const toast = window.bootstrap ? new window.bootstrap.Toast(elements.notification, { delay: 5000 }) : null;", "  const toast = window.bootstrap ? new window.bootstrap.Toast(elements.notification, { delay: 5000 }) : null;\n  const dailyModal = window.bootstrap && elements.dailyAggregationModal ? new window.bootstrap.Modal(elements.dailyAggregationModal) : null;", 1)
helper_anchor = "  function clearRaceView() {"
helpers = r'''  function setDailyToolsEnabled(enabled) {
    [elements.dailyValueButton, elements.doubleTrioHubButton].forEach((button) => {
      if (button) button.disabled = !enabled;
    });
    if (elements.dailyToolbarHint) {
      elements.dailyToolbarHint.textContent = enabled
        ? `已載入 ${state.loadedRaces.length} 場；工具只讀取既有賽前工件。`
        : '請先載入賽日；工具只讀取當日已保存的預測及官方孖T工件。';
    }
  }

  function showDailyModal(title, kicker, html) {
    if (!elements.dailyAggregationContent || !dailyModal) {
      showNotification('全日工具暫未能開啟，請重新整理頁面。', true);
      return;
    }
    elements.dailyAggregationTitle.textContent = title;
    elements.dailyAggregationKicker.textContent = kicker;
    elements.dailyAggregationContent.innerHTML = html;
    dailyModal.show();
  }

  function raceLabel(race) {
    return `${escapeHtml(race.course || '—')} 第 ${escapeHtml(race.race_no ?? '—')} 場`;
  }

  function dailyEmpty(message) {
    return `<div class="daily-empty-state"><strong>${escapeHtml(message)}</strong><p class="mb-0 mt-2 small text-secondary">本工具只讀取當日已保存 JSON；不會重新計算機率、EV、Kelly 或變更任何賽前工件。</p></div>`;
  }

  function dailyEv(row) {
    return toFiniteNumber(firstDefined(row, ['ev_per_unit', 'win_ev_per_unit', 'win_ev', 'kelly_staking.ev']));
  }

  function dailyKelly(row) {
    return toFiniteNumber(firstDefined(row, ['fractional_kelly_stake_fraction', 'kelly_quarter_fraction_capped', 'kelly_fraction', 'kelly_staking.stake_fraction']));
  }

  async function openDailyValueSheet() {
    if (!state.date) {
      showNotification('請先載入有效賽日。', true);
      return;
    }
    setLoading(elements.dailyValueButton, true);
    try {
      const jobs = state.loadedRaces.slice();
      if (!jobs.length) {
        showDailyModal('全日高 EV 戰報', 'READ-ONLY DAILY VALUE SHEET', dailyEmpty('當前賽日暫無符合 EV > 0.05 的精選馬匹'));
        return;
      }
      const responses = await Promise.all(jobs.map(async (race) => {
        const url = `/api/prediction/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}/${encodeURIComponent(race.race_no)}`;
        try { return { race, payload: await request(url) }; } catch (error) { return { race, error }; }
      }));
      const candidates = [];
      let unreadable = 0;
      responses.forEach(({ race, payload, error }) => {
        if (error || !payload) { unreadable += 1; return; }
        const rows = Array.isArray(payload?.prediction?.predictions) ? payload.prediction.predictions : [];
        rows.forEach((row) => {
          const ev = dailyEv(row);
          const stake = dailyKelly(row);
          if ((ev !== null && ev > 0.05) || (stake !== null && stake > 0)) {
            candidates.push({ race, row, ev, stake });
          }
        });
      });
      candidates.sort((left, right) => (left.race.course || '').localeCompare(right.race.course || '') || Number(left.race.race_no) - Number(right.race.race_no) || (right.ev ?? -Infinity) - (left.ev ?? -Infinity));
      if (!candidates.length) {
        showDailyModal('全日高 EV 戰報', 'READ-ONLY DAILY VALUE SHEET', `${dailyEmpty('當前賽日暫無符合 EV > 0.05 的精選馬匹')}${unreadable ? `<p class="small text-secondary mt-3 mb-0">另有 ${unreadable} 場工件暫不可讀取，未被納入彙整。</p>` : ''}`);
        return;
      }
      const body = candidates.map(({ race, row, ev, stake }) => `<tr><td>${raceLabel(race)}</td><td>${displayValue(row.horse_no)}</td><td><strong>${displayValue(row.horse_name, '未命名')}</strong></td><td class="text-end">${percent(firstDefined(row, ['calibrated_win_probability', 'predicted_win_probability']))}</td><td class="text-end">${oddsNumber(firstDefined(row, ['latest_win_odds', 'win_odds', 'market_odds']))}</td><td class="text-end ${ev !== null && ev > 0 ? 'ev-positive' : ''}">${decimal(ev)}</td><td class="text-end">${percent(stake, 2)}</td></tr>`).join('');
      showDailyModal('全日高 EV 戰報', 'READ-ONLY DAILY VALUE SHEET', `<p class="small text-secondary">篩選條件：已保存 EV &gt; 0.05，或已有正數 0.25x Kelly 建議注碼。僅供研究展示，不構成投注指令。</p><div class="table-responsive"><table class="table table-sm table-hover daily-value-table"><thead><tr><th>場次</th><th>馬號</th><th>馬名</th><th class="text-end">預測勝率</th><th class="text-end">賠率</th><th class="text-end">EV</th><th class="text-end">0.25x Kelly</th></tr></thead><tbody>${body}</tbody></table></div>${unreadable ? `<p class="small text-secondary mb-0">${unreadable} 場工件暫不可讀取，已安全略過。</p>` : ''}`);
    } catch (error) {
      showDailyModal('全日高 EV 戰報', 'READ-ONLY DAILY VALUE SHEET', dailyEmpty(`無法讀取全日工件：${error.message}`));
    } finally {
      setLoading(elements.dailyValueButton, false);
    }
  }

  function renderDoubleTrioHub(payloads) {
    const ready = [];
    const states = [];
    payloads.forEach(({ course, payload, error }) => {
      const events = Array.isArray(payload?.events) ? payload.events : [];
      events.filter((event) => event?.status === 'ready').forEach((event) => ready.push({ course, event }));
      if (!events.some((event) => event?.status === 'ready')) states.push(`${course}：${escapeHtml(payload?.message || payload?.status || (error ? '暫不可讀取' : '官方孖T工件待產出'))}`);
    });
    if (!ready.length) return `${dailyEmpty('官方孖 T 工件待產出 / 尚無已確認組合')}<p class="small text-secondary mt-3 mb-0">${states.join('；') || '尚未有可讀取的當日場次工件。'}</p>`;
    const eventsHtml = ready.map(({ course, event }) => {
      const legs = Array.isArray(event.legs) ? event.legs : [];
      const legHtml = legs.map((leg) => {
        const selections = Array.isArray(leg.selections) ? leg.selections : [];
        const horses = selections.length ? selections.map((runner) => `<span class="daily-horse-chip">${displayValue(runner.horse_no)} ${displayValue(runner.horse_name, '')}</span>`).join('') : '<span class="text-secondary">—</span>';
        return `<section class="daily-double-trio-leg"><strong>第 ${displayValue(leg.leg_no)} 關 · 第 ${displayValue(leg.race_no)} 場</strong><div class="mt-2">${horses}</div></section>`;
      }).join('');
      const plan = event.combination_plan || {};
      return `<article class="daily-double-trio-event"><div class="d-flex flex-wrap justify-content-between gap-2"><div><strong>${displayValue(event.display_label, '官方孖T')}</strong><small class="d-block text-secondary">${course} · ${displayValue(event.pool_event_code, '')}</small></div><div class="text-end small">${displayValue(plan.total_bet_combinations)} 注<br>每注 ${hkd(plan.unit_stake_hkd)} · 合計 ${hkd(plan.total_suggested_capital_hkd)}</div></div><div class="row g-2 mt-1">${legHtml}</div></article>`;
    }).join('');
    return `<p class="small text-secondary">只展示 HKJC 官方工件明示的關次及已保存選馬；未有 verified 工件時不會自行指定場次。</p>${eventsHtml}<p class="small text-secondary mb-0">四匹複式內容只作唯讀研究展示，不會提交、傳送或執行投注。</p>`;
  }

  async function openDoubleTrioHub() {
    if (!state.date) {
      showNotification('請先載入有效賽日。', true);
      return;
    }
    setLoading(elements.doubleTrioHubButton, true);
    try {
      const courses = [...new Set(state.loadedRaces.map((race) => race.course).filter(Boolean))];
      if (!courses.length) {
        showDailyModal('雙 T／孖 T 策略矩陣', 'READ-ONLY DOUBLE TRIO HUB', dailyEmpty('官方孖 T 工件待產出 / 尚無已確認組合'));
        return;
      }
      const payloads = await Promise.all(courses.map(async (course) => {
        try { return { course, payload: await request(`/api/double-trio/${encodeURIComponent(state.date)}/${encodeURIComponent(course)}`) }; }
        catch (error) { return { course, error }; }
      }));
      showDailyModal('雙 T／孖 T 策略矩陣', 'READ-ONLY DOUBLE TRIO HUB', renderDoubleTrioHub(payloads));
    } catch (error) {
      showDailyModal('雙 T／孖 T 策略矩陣', 'READ-ONLY DOUBLE TRIO HUB', dailyEmpty(`無法讀取官方孖T工件：${error.message}`));
    } finally {
      setLoading(elements.doubleTrioHubButton, false);
    }
  }

'''
if helper_anchor not in js:
    raise SystemExit('JS helper anchor not found')
js = js.replace(helper_anchor, helpers + helper_anchor, 1)
js = js.replace("    state.predictionRace = null;\n    elements.predictionSection.classList.add('d-none');", "    state.predictionRace = null;\n    elements.predictionSection.classList.add('d-none');", 1)
js = js.replace("      state.date = requestedDate;\n      renderRaceButtons(Array.isArray(payload.races) ? payload.races : []);", "      state.date = requestedDate;\n      state.loadedRaces = Array.isArray(payload.races) ? payload.races : [];\n      renderRaceButtons(state.loadedRaces);\n      setDailyToolsEnabled(true);", 1)
js = js.replace("      elements.raceList.innerHTML = '<p class=\"text-danger mb-0\">無法讀取賽程。</p>';", "      state.loadedRaces = [];\n      setDailyToolsEnabled(false);\n      elements.raceList.innerHTML = '<p class=\"text-danger mb-0\">無法讀取賽程。</p>';", 1)
js = js.replace("  elements.raceDate.value = hongKongToday();\n  elements.loadRacesButton.addEventListener('click', loadRaces);", "  elements.raceDate.value = hongKongToday();\n  setDailyToolsEnabled(false);\n  elements.loadRacesButton.addEventListener('click', loadRaces);\n  elements.dailyValueButton?.addEventListener('click', openDailyValueSheet);\n  elements.doubleTrioHubButton?.addEventListener('click', openDoubleTrioHub);", 1)
js_path.write_text(js, encoding='utf-8')

css = css_path.read_text(encoding='utf-8')
css += '''
.daily-toolbar { align-items: stretch; }
.daily-tool-button { min-height: 2.75rem; font-weight: 700; }
.daily-aggregation-modal { border: 1px solid var(--line); }
.daily-aggregation-modal .modal-header { background: #f8fbff; }
.daily-empty-state { padding: 1.1rem; color: #475569; background: #f8fafc; border: 1px dashed #cbd5e1; }
.daily-value-table th { white-space: nowrap; }
.daily-double-trio-event { padding: 1rem 0; border-top: 1px solid var(--line); }
.daily-double-trio-event:first-of-type { border-top: 0; padding-top: 0; }
.daily-double-trio-leg { height: 100%; padding: .8rem; background: #f8fafc; border: 1px solid #dbe5ef; }
.daily-horse-chip { display: inline-block; margin: 0 .35rem .35rem 0; padding: .3rem .45rem; color: #0f4c5c; background: #e6fffb; border: 1px solid #b9d9d4; font-size: .82rem; }
@media (max-width: 575.98px) {
  .daily-toolbar { display: grid !important; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: .5rem !important; }
  .daily-tool-button { min-height: 3rem; padding: .5rem .55rem; font-size: .84rem; white-space: normal; }
  .modal-fullscreen-sm-down .modal-header { padding: .9rem 1rem; }
  .modal-fullscreen-sm-down .modal-body { padding: 1rem; }
  .daily-value-table { min-width: 650px; }
}
'''
css_path.write_text(css, encoding='utf-8')
print('patched daily toolbar')
