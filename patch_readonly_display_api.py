from pathlib import Path

p = Path('/home/ubuntu/hkjc_v10_database/web_api.py')
s = p.read_text(encoding='utf-8')
old_import = 'from n6_integration import enrich_prediction\n'
new_import = old_import + 'from readonly_display_enricher import enrich_prediction_for_display\n'
if 'from readonly_display_enricher import enrich_prediction_for_display' not in s:
    if old_import not in s:
        raise SystemExit('n6 import anchor not found')
    s = s.replace(old_import, new_import, 1)
old = '''        enriched_prediction["n6_integration"] = {
            "status": "unavailable",
            "message": "N6 輔助服務暫不可用；V10 原有分析維持不變。",
            "notice": "未改寫 V10 已保存的勝率、EV、Kelly 或既有風險提示。",
        }
    filter_path = job_dir / "high_probability_filter.json"
    return {
        "date": requested_date.isoformat(),
        "course": normalized_course,
        "race_no": normalized_race_no,
        "prediction": enriched_prediction,
'''
new = '''        enriched_prediction["n6_integration"] = {
            "status": "unavailable",
            "message": "N6 輔助服務暫不可用；V10 原有分析維持不變。",
            "notice": "未改寫 V10 已保存的勝率、EV、Kelly 或既有風險提示。",
        }
    # Display-only copy: prediction.json remains byte-for-byte untouched.
    enriched_prediction = enrich_prediction_for_display(enriched_prediction)
    filter_path = job_dir / "high_probability_filter.json"
    return {
        "date": requested_date.isoformat(),
        "course": normalized_course,
        "race_no": normalized_race_no,
        "prediction": enriched_prediction,
'''
if old not in s:
    raise SystemExit('prediction return anchor not found')
s = s.replace(old, new, 1)
p.write_text(s, encoding='utf-8')
print('patched web_api display-only route')
