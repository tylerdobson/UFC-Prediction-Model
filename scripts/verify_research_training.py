"""Verify v3 on the full retained history and an explicitly synthetic live card."""
import argparse
import json
import tempfile
from pathlib import Path

from ufc_odds_model import db
from ufc_odds_model.alerts import _ready_model_probabilities
from ufc_odds_model.model_lab import file_sha256, read_only, write_new_json
from ufc_odds_model.pipeline import parse_utc, score_event
from ufc_odds_model.wikidata_profiles import static_profiles


def verify(history, output, *, cutoff='2026-10-10T01:00:00Z'):
    as_of=parse_utc(cutoff)
    before=file_sha256(history)
    with read_only(history) as research:
        profiles=static_profiles(research,cutoff_at_utc=cutoff)
        fighters=sorted(profiles)[:2]
        if len(fighters)<2:
            raise ValueError('Verification needs two retained pre-cutoff birth dates')
        names={r['fighter_id']:r['canonical_name'] for r in research.execute('SELECT * FROM fighters')}
    with tempfile.TemporaryDirectory(prefix='ufc-training-check-') as temporary:
        root=Path(temporary)
        with db.connect(root/'operating.sqlite') as operating:
            db.init_db(operating)
            for fighter in fighters:
                db.upsert_fighter(operating,fighter,names[fighter],source='validation_fixture')
            db.upsert_event(operating,'synthetic-training-check','Synthetic upcoming training fixture',
                            '2026-10-17','scheduled',source='validation_fixture',start_time_utc='2026-10-17T20:00:00Z')
            db.upsert_bout(operating,'synthetic-training-bout','synthetic-training-check',*fighters,'scheduled')
            operating.commit()
            try:
                score_event(operating,'synthetic-training-check',as_of,root/'default-reports',model_kind='logistic')
            except ValueError as error:
                default_refusal=str(error)
            else:
                raise ValueError('Default scoring unexpectedly bypassed operating history')
            _,rows=score_event(operating,'synthetic-training-check',as_of,root/'reports',
                               model_kind='logistic',model_dir=root/'models',training_history=history)
            artifact=json.loads(next((root/'models').glob('*.json')).read_text())
            ready,_=_ready_model_probabilities(operating,'synthetic-training-check',rows[0]['model_version'],cutoff)
            if ready or operating.execute('SELECT COUNT(*) FROM results').fetchone()[0]:
                raise ValueError('Retrospective history was promoted into operating evidence')
            result={'synthetic_target_only':True,'source_database_sha256':before,
                    'cutoff_at_utc':cutoff,'model_version':rows[0]['model_version'],
                    'training_history':rows[0]['training_history'],'default_refusal':default_refusal,
                    'prediction_metadata':json.loads(rows[0]['prediction_metadata_json']),
                    'model_probability':rows[0]['model_probability_fighter_a'],
                    'training_bouts':artifact['training_bouts'],'training_last_date':artifact['training_last_date'],
                    'calibration_bouts':artifact['calibration_bouts'],
                    'calibration_first_date':artifact['calibration_first_date'],
                    'calibration_last_date':artifact['calibration_last_date'],
                    'operating_result_rows':0,'operating_readiness':False,
                    'training_source_unchanged':before==file_sha256(history)}
    if not result['training_source_unchanged']:
        raise ValueError('Training source changed')
    write_new_json(output,result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--cutoff',default='2026-10-10T01:00:00Z')
    args=parser.parse_args()
    result=verify(args.history,args.output,cutoff=args.cutoff)
    print(json.dumps({k:v for k,v in result.items() if k!='prediction_metadata'},indent=2))
