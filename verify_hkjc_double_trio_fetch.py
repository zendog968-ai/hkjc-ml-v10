#!/usr/bin/env python3
"""Offline parser checks for HKJC official Double Trio event artifacts."""
from __future__ import annotations

import json

from fetch_hkjc_double_trio import build_payload, parse_double_trio_events


def main() -> int:
    active_html = """
      <main><p>06/09/2026, Sunday, Sha Tin</p><h2>Double Trio</h2><h3>1st Double Trio</h3>
      <section>First Leg - Race 4</section><section>Second Leg - Race 5</section>
      <h3>2nd Double Trio</h3><section>First Leg - Race 7</section><section>Second Leg - Race 8</section></main>
    """
    events = parse_double_trio_events(active_html)
    assert [(item["legs"][0]["race_no"], item["legs"][1]["race_no"]) for item in events] == [(4, 5), (7, 8)], events
    confirmed = build_payload("2026-09-06", "ST", "https://hkjc.example/dt", "offline_fixture", active_html)
    assert confirmed["status"] == "official_confirmed" and len(confirmed["events"]) == 2, confirmed
    pending = build_payload("2026-09-06", "ST", "https://hkjc.example/dt", "offline_fixture", "<p>Double Trio coming soon</p>")
    assert pending["status"] == "pending" and not pending["events"], pending
    wrong_meeting = build_payload("2026-09-06", "ST", "https://hkjc.example/dt", "offline_fixture", "<p>19/08/2026, Wednesday, United Kingdom</p><p>First Leg - Race 4 Second Leg - Race 5</p>")
    assert wrong_meeting["status"] == "pending" and not wrong_meeting["events"], wrong_meeting
    print(json.dumps({"status": "PASS", "official_events": len(events), "first_event": events[0], "wrong_meeting_rejected": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
