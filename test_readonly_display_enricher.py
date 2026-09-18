from copy import deepcopy
import math

from readonly_display_enricher import enrich_prediction_for_display


def main() -> None:
    source = {
        'predictions': [
            {
                'horse_no': 1,
                'win_odds_t15': 4.0,
                'win_odds_t5': 3.2,
                'ev_per_unit': 0.28,
                'kelly_quarter_fraction_capped': 0.04,
                'kelly_status': 'capped',
                'condition_score': 0.9,
                'trackwork_comment': 'good',
                'effort_level': 'EASY',
                'surge_rating': 'STRONG',
                'ambition_flag': True,
            },
            {
                'horse_no': 2,
                'win_odds_t15': None,
                'win_odds_t5': 'bad',
                'ev_per_unit': None,
                'kelly_quarter_fraction_capped': None,
                'condition_score': None,
            },
        ],
    }
    before = deepcopy(source)
    output = enrich_prediction_for_display(source)
    assert source == before, 'source prediction was mutated'
    first = output['predictions'][0]
    assert math.isclose(first['odds_drift']['win_drift_ratio'], -0.2, rel_tol=0.0, abs_tol=1e-12)
    assert first['odds_drift']['smart_money_flag'] is True
    assert first['kelly_staking']['stake_fraction'] == 0.04
    assert first['qualitative_intel']['condition_score'] == 0.9
    second = output['predictions'][1]
    assert second['odds_drift']['status'] == 'not_available'
    assert second['odds_drift']['win_drift_ratio'] is None
    assert second['kelly_staking']['status'] == 'not_available'
    assert second['qualitative_intel']['status'] == 'not_available'
    assert output['display_extensions']['status'] == 'read_only'
    assert output['display_extensions']['fail_closed'] is True
    print('source_unchanged=PASS')
    print('complete_source_mapping=PASS')
    print('missing_source_fail_closed=PASS')
    print('null_outputs=PASS')


if __name__ == '__main__':
    main()
