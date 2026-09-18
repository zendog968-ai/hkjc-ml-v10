from pathlib import Path

root = Path('/home/ubuntu/hkjc_v10_database/frontend')
html_path = root / 'index.html'
js_path = root / 'app.js'
css_path = root / 'style.css'

html = html_path.read_text(encoding='utf-8')
needle = '''              <button id="doubleTrioHubButton" class="btn btn-outline-primary daily-tool-button" type="button" disabled>
                <span class="button-text">雙 T／孖 T 策略矩陣</span><span class="spinner-border spinner-border-sm d-none" aria-hidden="true"></span>
              </button>
'''
replacement = needle + '''              <button id="brierAuditButton" class="btn btn-outline-dark daily-tool-button" type="button" disabled>
                <span class="button-text">賽後 Brier 審計</span><span class="spinner-border spinner-border-sm d-none" aria-hidden="true"></span>
              </button>
'''
if needle not in html:
    raise SystemExit('brier toolbar anchor not found')
html = html.replace(needle, replacement, 1)
header_anchor = '''            <p id="predictionMeta" class="text-secondary small mb-0 mt-1"></p>
            <p id="n6ModelSummary" class="text-secondary small mb-0 mt-1"></p>
'''
header_replacement = '''            <p id="predictionMeta" class="text-secondary small mb-0 mt-1"></p>
            <div id="defensiveBadges" class="defensive-badges mt-2" aria-live="polite"></div>
            <p id="n6ModelSummary" class="text-secondary small mb-0 mt-1"></p>
'''
if header_anchor not in html:
    raise SystemExit('defensive badge anchor not found')
html = html.replace(header_anchor, header_replacement, 1)
html_path.write_text(html, encoding='utf-8')

js = js_path.read_text(encoding='utf-8')
js = js.replace("    doubleTrioHubButton: document.getElementById('doubleTrioHubButton'),", "    doubleTrioHubButton: document.getElementById('doubleTrioHubButton'),\n    brierAuditButton: document.getElementById('brierAuditButton'),", 1)
js = js.replace("    predictionMeta: document.getElementById('predictionMeta'),", "    predictionMeta: document.getElementById('predictionMeta'),\n    defensiveBadges: document.getElementById('defensiveBadges'),", 1)
js = js.replace("    [elements.dailyValueButton, elements.doubleTrioHubButton].forEach((button) => {", "    [elements.dailyValueButton, elements.doubleTrioHubButton, elements.brierAuditButton].forEach((button) => {", 1)
clear_anchor = "    elements.n6ModelSummary.textContent = '';"
js = js.replace(clear_anchor, clear_anchor + "\n    elements.defensiveBadges?.replaceChildren();", 1)
helper_anchor = "  function clearRaceView() {"
helpers = r'''  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function collectionCount(value) {
    if (Array.isArray(value)) return value.length;
    const number = toFiniteNumber(value);
    return number !== null && number > 0 ? Math.trunc(number) : 0;
  }

  function defensiveRootCandidates(prediction) {
    const race = prediction?.race && typeof prediction.race === 'object' ? prediction.race : {};
    return [prediction || {}, race, prediction?.lineup_validation || {}, prediction?.dynamic_validation || {}, prediction?.dynamic_updates || {}];
  }

  function firstCollection(roots, keys) {
    for (const root of roots) {
      if (!root || typeof root !== 'object') continue;
      for (const key of keys) {
        if (root[key] !== undefined && root[key] !== null) return root[key];
      }
    }
    return null;
  }

  function renderDefensiveBadges(prediction) {
    if (!elements.defensiveBadges) return;
    const roots = defensiveRootCandidates(prediction);
    const rows = asArray(prediction?.predictions);
    const scratchedValue = firstCollection(roots, ['scratched', 'scratched_horses', 'scratching_list']);
    const scratchedFromRows = rows.filter((row) => String(row?.status || '').toUpperCase() === 'SCR').length;
    const scratchedCount = Math.max(collectionCount(scratchedValue), scratchedFromRows, collectionCount(firstCollection(roots, ['scratched_count', 'scratch_count'])));
    const changesValue = firstCollection(roots, ['jockey_changes', 'jockey_change_records', 'jockey_replacements']);
    const jockeyCount = Math.max(collectionCount(changesValue), collectionCount(firstCollection(roots, ['jockey_change_count', 'jockey_changes_count'])));
    const race = prediction?.race && typeof prediction.race === 'object' ? prediction.race : {};
    const course = firstCollection([race, prediction || {}], ['track_course', 'course_configuration', 'course_variant', 'track']);
    const going = firstCollection([race, prediction || {}], ['going', 'going_description', 'track_condition']);
    const weather = firstCollection([race, prediction || {}], ['weather', 'weather_description']);
    const badges = [];
    if (scratchedCount > 0) badges.push(`<span class="badge defensive-badge defensive-scratch">已剔除 ${scratchedCount} 匹退賽馬（機率已重算）</span>`);
    if (jockeyCount > 0) badges.push('<span class="badge defensive-badge defensive-jockey">騎師更換（已套用保守 Profile）</span>');
    const trackText = [course, going, weather].filter((value) => value !== null && value !== undefined && value !== '').map(escapeHtml).join(' ｜ ');
    if (trackText) badges.push(`<span class="badge defensive-badge defensive-track">${trackText}</span>`);
    elements.defensiveBadges.innerHTML = badges.join('');
  }

  function locateBrierAudit(payload) {
    const prediction = payload?.prediction && typeof payload.prediction === 'object' ? payload.prediction : {};
    const roots = [payload || {}, prediction, prediction.post_race || {}, prediction.audit || {}];
    for (const root of roots) {
      if (!root || typeof root !== 'object') continue;
      for (const key of ['post_race_brier_audit', 'post_race_audit', 'brier_audit', 'daily_brier_audit']) {
        const candidate = root[key];
        if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) return candidate;
      }
    }
    return null;
  }

  function auditRaceRows(audit) {
    const values = [audit?.race_audits, audit?.races, audit?.race_results, audit?.results];
    for (const value of values) if (Array.isArray(value)) return value.filter((item) => item && typeof item === 'object');
    return [];
  }

  function brierEmpty() {
    return dailyEmpty('當前賽日尚未產出賽後審計報告（待賽事完結）');
  }

  function renderBrierAuditModal(audits) {
    if (!audits.length) return brierEmpty();
    const normalized = audits.map(({ audit, race }) => ({
      audit,
      race,
      score: toFiniteNumber(audit.daily_brier_score ?? audit.brier_score),
      count: toFiniteNumber(audit.scored_race_count),
    }));
    const summary = normalized.sort((left, right) => (right.count ?? -1) - (left.count ?? -1))[0];
    const auditRows = normalized.flatMap(({ audit, race }) => auditRaceRows(audit).map((row) => ({ row, fallbackRace: race })));
    const rowsHtml = auditRows.length ? auditRows.map(({ row, fallbackRace }) => {
      const raceNo = firstDefined(row, ['race_no', 'race.race_no']) ?? fallbackRace?.race_no;
      const course = firstDefined(row, ['racecourse', 'course', 'race.racecourse']) ?? fallbackRace?.course;
      const top = firstDefined(row, ['model_top_horse_no', 'top_pick_horse_no', 'predicted_top_horse_no', 'model_top.horse_no']);
      const winner = firstDefined(row, ['official_winner_horse_no', 'winner_horse_no', 'official_first_horse_no', 'official_winner.horse_no']);
      const aligned = (top !== null && winner !== null) ? String(top) === String(winner) : null;
      const status = aligned === true ? '<span class="badge audit-aligned">對齊</span>' : (aligned === false ? '<span class="badge audit-misaligned">未對齊</span>' : '<span class="text-secondary small">—</span>');
      return `<tr><td>${displayValue(course)}</td><td>${displayValue(raceNo)}</td><td>${displayValue(top)}</td><td>${displayValue(winner)}</td><td>${status}</td></tr>`;
    }).join('') : '<tr><td colspan="5" class="text-center text-secondary py-3">審計摘要未提供逐場首選／冠軍對齊資料。</td></tr>';
    return `<div class="brier-summary"><div><small class="text-secondary">全日 Brier Score</small><strong>${summary.score === null ? '—' : summary.score.toFixed(6)}</strong></div><div><small class="text-secondary">已審計場次</small><strong>${summary.count === null ? '—' : Math.trunc(summary.count)}</strong></div></div><p class="small text-secondary mt-3">數值只讀自既有賽後審計 JSON；頁面不會重新結算、補建賽果或改寫歷史紀錄。</p><div class="table-responsive"><table class="table table-sm table-hover brier-audit-table"><thead><tr><th>馬場</th><th>場次</th><th>模型首選馬</th><th>官方第一名</th><th>對齊</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>`;
  }

  async function openBrierAudit() {
    if (!state.date) {
      showNotification('請先載入有效賽日。', true);
      return;
    }
    setLoading(elements.brierAuditButton, true);
    try {
      const jobs = state.loadedRaces.slice();
      if (!jobs.length) {
        showDailyModal('賽後 Brier 審計', 'READ-ONLY POST-RACE AUDIT', brierEmpty());
        return;
      }
      const responses = await Promise.all(jobs.map(async (race) => {
        const url = `/api/prediction/${encodeURIComponent(race.date)}/${encodeURIComponent(race.course)}/${encodeURIComponent(race.race_no)}`;
        try { return { race, payload: await request(url) }; } catch (error) { return { race, error }; }
      }));
      const audits = responses.map(({ race, payload }) => ({ race, audit: locateBrierAudit(payload) })).filter((item) => item.audit);
      showDailyModal('賽後 Brier 審計', 'READ-ONLY POST-RACE AUDIT', renderBrierAuditModal(audits));
    } catch (error) {
      showDailyModal('賽後 Brier 審計', 'READ-ONLY POST-RACE AUDIT', brierEmpty());
    } finally {
      setLoading(elements.brierAuditButton, false);
    }
  }

'''
if helper_anchor not in js:
    raise SystemExit('JS helper anchor not found')
js = js.replace(helper_anchor, helpers + helper_anchor, 1)
render_anchor = "    setN6Status(n6.status, n6.message);\n    renderPredictionTable(payload);"
if render_anchor not in js:
    raise SystemExit('renderPrediction anchor not found')
js = js.replace(render_anchor, "    setN6Status(n6.status, n6.message);\n    renderDefensiveBadges(prediction);\n    renderPredictionTable(payload);", 1)
listener_anchor = "  elements.dailyValueButton?.addEventListener('click', openDailyValueSheet);"
if listener_anchor not in js:
    raise SystemExit('button listener anchor not found')
js = js.replace(listener_anchor, listener_anchor + "\n  elements.brierAuditButton?.addEventListener('click', openBrierAudit);", 1)
js_path.write_text(js, encoding='utf-8')

css = css_path.read_text(encoding='utf-8')
css += '''
.defensive-badges { display: flex; flex-wrap: wrap; gap: .35rem; }
.defensive-badge { padding: .34rem .5rem; font-size: .72rem; font-weight: 700; line-height: 1.25; white-space: normal; }
.defensive-scratch { color: #075b34; background: #d1fae5; border: 1px solid #6ee7b7; }
.defensive-jockey { color: #7a4a00; background: #fef3c7; border: 1px solid #fcd34d; }
.defensive-track { color: #475569; background: #f1f5f9; border: 1px solid #cbd5e1; }
.brier-summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; }
.brier-summary > div { padding: .8rem; background: #f8fafc; border: 1px solid #dbe5ef; }
.brier-summary small, .brier-summary strong { display: block; }
.brier-summary strong { margin-top: .2rem; color: var(--ink); font-size: 1.15rem; }
.audit-aligned { color: #075b34; background: #d1fae5; border: 1px solid #6ee7b7; }
.audit-misaligned { color: #8a1c1c; background: #fee2e2; border: 1px solid #fca5a5; }
.brier-audit-table th { white-space: nowrap; }
@media (max-width: 575.98px) {
  .daily-toolbar { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .defensive-badge { max-width: 100%; font-size: .68rem; }
  .brier-summary { gap: .5rem; }
  .brier-summary > div { padding: .7rem; }
  .brier-audit-table { min-width: 550px; }
}
'''
css_path.write_text(css, encoding='utf-8')
print('patched brier modal and defensive badges')
