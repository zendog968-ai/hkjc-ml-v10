#!/usr/bin/env python3
"""Regression checks for fail-closed S2 monitoring validation."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from monitor_s2_overseas import load_manifest, validate_event


def main() -> int:
    valid, reason = validate_event({
        'race_no': 1,
        'local_start_time': '14:00 BST',
        'hkt_start_time': '21:00 HKT',
        'racing_post_url': 'https://www.racingpost.com/racecards/1/example/2026-08-20/123456/',
        'at_the_races_url': 'https://www.attheraces.com/racecards/Example/20-August-2026',
        'hkjc_win_place_url': 'https://bet.hkjc.com/en/racing/wp/2026-08-20/S2/1',
    })
    assert valid and reason == 'ready'
    valid, reason = validate_event({'race_no': 1, 'racing_post_url': 'http://untrusted.invalid/'})
    assert not valid and reason == 'missing_start_time'
    valid, reason = validate_event({
        'race_no': 1,
        'local_start_time': '14:00 BST',
        'hkt_start_time': '21:00 HKT',
        'racing_post_url': 'https://evil.example/race',
        'at_the_races_url': 'https://www.attheraces.com/racecards/Example/20-August-2026',
    })
    assert not valid and reason == 'unverified_racing_post_url'
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'manifest.json'
        path.write_text(json.dumps({'simulcast_code': 'S1', 'meeting_date': '2026-08-20', 'venue': 'Example', 'events': []}), encoding='utf-8')
        try:
            load_manifest(path)
        except SystemExit:
            pass
        else:
            raise AssertionError('non-S2 manifest must stop')
    print('PASS: S2 manifest validation is HTTPS allowlisted and fail-closed before any fetch')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
