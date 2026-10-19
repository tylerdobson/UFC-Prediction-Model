"""Verify retained official athlete facts and attach dated research reach.

Current career rates are descriptive context. They never substitute for
earlier-fight observations in a historical feature replay.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from .model_lab import file_sha256
from .pipeline import parse_utc

FACT_KEYS = (
    'significant_strikes_landed_per_minute', 'significant_strikes_absorbed_per_minute',
    'significant_strike_accuracy_display_pct', 'significant_strike_defense_pct',
    'takedowns_average_per_15_minutes', 'takedown_accuracy_display_pct',
    'takedown_defense_pct', 'submissions_average_per_15_minutes',
    'knockdowns_average_per_15_minutes',
)


def verify_retained_receipt(source: dict, root: str | Path, cutoff) -> dict:
    """Bind source metadata and capture time to its actual retained receipt.

    This also verifies all three retained files and can be called after a
    caller's existing file checks. Capture must be strictly before cutoff.
    The generic helper does not choose a publisher or establish fighter ID.
    """
    root = Path(root).resolve()
    raw_root = (root / 'data/raw').resolve()
    paths = {}
    try:
        for path_key, hash_key in (('response_path', 'response_sha256'),
                                   ('headers_path', 'headers_sha256'),
                                   ('receipt_path', 'receipt_sha256')):
            payload = root / source[path_key]
            if (payload.is_symlink() or not payload.is_file()
                    or not payload.resolve().is_relative_to(raw_root)
                    or file_sha256(payload) != source[hash_key]):
                raise ValueError('Retained source evidence is missing, changed, or outside retained storage')
            paths[path_key] = payload
        receipt = json.loads(paths['receipt_path'].read_text())
        if not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1:
            raise ValueError('Retained source receipt must be a schema 1 JSON object')
        required = ('request_url', 'response_url', 'http_status', 'fetched_at_utc',
                    'response_path', 'response_sha256', 'headers_path', 'headers_sha256')
        if any(key not in source or key not in receipt or source[key] != receipt[key] for key in required):
            raise ValueError('Retained receipt does not match source metadata')
        if receipt['http_status'] != 200:
            raise ValueError('Retained source receipt is not a successful capture')
        for key in ('request_url', 'response_url'):
            parsed = urlsplit(receipt[key])
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Retained source URLs must identify a public HTTP source')
        if parse_utc(receipt['fetched_at_utc']) >= cutoff:
            raise ValueError('Retained source capture must precede the forecast cutoff')
        if 'response_bytes' in receipt:
            if (type(receipt['response_bytes']) is not int
                    or receipt['response_bytes'] != paths['response_path'].stat().st_size
                    or 'response_bytes' in source and source['response_bytes'] != receipt['response_bytes']):
                raise ValueError('Retained response byte count does not match its receipt')
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError('Retained source receipt is invalid or incomplete') from exc
    return receipt


def load_athlete_context(path: str | Path, root: str | Path, cutoff) -> dict:
    root = Path(root).resolve()
    document = json.loads(Path(path).read_text())
    if document.get('schema_version') != 1 or not isinstance(document.get('fighters'), list):
        raise ValueError('Invalid official athlete statistics document')
    result = {}
    for fighter in document['fighters']:
        identity, source = fighter['fighter_id'], fighter['source']
        if identity in result:
            raise ValueError('Duplicate official athlete identity')
        verify_retained_receipt(source, root, cutoff)
        profile = urlsplit(fighter['official_profile_url'])
        if (profile.scheme != 'https' or profile.hostname not in {'ufc.com', 'www.ufc.com'}
                or profile.username or profile.password or profile.query or profile.fragment
                or not profile.path.startswith('/athlete/') or not profile.path.removeprefix('/athlete/').strip('/')
                or fighter['official_profile_url'] != source['request_url']
                or fighter['official_profile_url'] != source['response_url']):
            raise ValueError('Official athlete profile identity does not match its captured source URLs')
        if (source.get('http_status') != 200
                or parse_utc(source['fetched_at_utc']) >= cutoff
                or parse_utc(fighter['observed_at_utc']) > parse_utc(source['fetched_at_utc'])):
            raise ValueError('Official athlete capture must precede the forecast cutoff')
        reach = fighter['reach_cm']
        if not isinstance(reach, (int, float)) or not math.isfinite(reach) or not 100 <= reach <= 250:
            raise ValueError('Official reach must be a plausible finite centimeter measurement')
        if abs(reach - fighter['reach_inches'] * 2.54) > 1e-7:
            raise ValueError('Official reach unit conversion does not match its source')
        for key in FACT_KEYS:
            value = fighter.get('stats', {}).get(key)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)
                                      or value < 0 or key.endswith('_pct') and value > 100):
                raise ValueError('Official fight statistics must have valid values and units')
        result[identity] = fighter
    return result


def attach_reach_observations(connection, fighters: dict, root: str | Path) -> None:
    """Attach verified reach only to the transient research target card."""
    for identity, fighter in fighters.items():
        if connection.execute('SELECT 1 FROM fighters WHERE fighter_id=?', (identity,)).fetchone() is None:
            continue
        source = fighter['source']
        run = connection.execute(
            'INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES (?,?,?,?)',
            ('official-ufc-profile-research', source['fetched_at_utc'],
             str(Path(root) / source['response_path']), source['response_sha256']))
        connection.execute(
            'INSERT INTO fighter_profile_observations(fighter_id,source,source_evidence_uri,'
            'source_license_uri,observed_at_utc,reach_cm,ingestion_run_id) VALUES (?,?,?,?,?,?,?)',
            (identity, 'official-ufc-profile-research', fighter['official_profile_url'],
             'https://www.ufc.com/news/terms-use', fighter['observed_at_utc'],
             fighter['reach_cm'], run.lastrowid))
    connection.commit()


def measurements(fighter: dict, *, reach_used_by_model: bool) -> dict:
    facts = [{'key': 'reach_cm', 'value': fighter['reach_cm'], 'used_by_model': reach_used_by_model}]
    facts.extend({'key': key, 'value': fighter['stats'][key], 'used_by_model': False}
                 for key in FACT_KEYS if fighter.get('stats', {}).get(key) is not None)
    return {'source_url': fighter['official_profile_url'],
            'captured_at_utc': fighter['source']['fetched_at_utc'],
            'source_label': 'Official UFC athlete profile', 'source_verified': True,
            'facts': facts}
