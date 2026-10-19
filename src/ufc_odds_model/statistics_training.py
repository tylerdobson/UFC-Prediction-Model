"""Apply the evaluated statistics policy to any scheduled research card."""
from __future__ import annotations

from datetime import datetime

from .model_lab import read_only
from .pipeline import parse_utc
from .research_stats import load_research_stats
from .research_training import prepare_research_logistic
from .research_upgrade import TrainingPolicy


def prepare_evaluated_statistics(connection, event_id: str, as_of: datetime, history_path,
                                 *, stats_db, archive_manifest, selection_path,
                                 evaluation_path):
    """Return a normal training run after verifying the frozen experiment.

    A rejected rich-feature candidate falls back to the evaluated V5 baseline.
    This explicit opt-in does not change the operating workflow's defaults.
    """
    from .research_v6 import (load_v6_selection, load_verified_v6_report,
                              load_rich_overlay, prepare_v6_research)
    selection = load_v6_selection(selection_path, history_path, archive_manifest, stats_db)
    evaluation = load_verified_v6_report(evaluation_path, selection_path, history_path,
                                         archive_manifest, stats_db)
    if parse_utc(evaluation['created_at_utc']) > as_of:
        raise ValueError('Statistics model evaluation must precede the forecast cutoff')
    with read_only(history_path) as history:
        overlay = load_research_stats(history, archive_manifest)
    if evaluation['research_champion'] == 'logistic-prior-v6':
        rich_overlay = load_rich_overlay(stats_db)
        return prepare_v6_research(connection, event_id, as_of, history_path,
                                   overlay, rich_overlay, selection)
    if evaluation['research_champion'] != 'logistic-prior-v5':
        raise ValueError('Unrecognized evaluated statistics model')
    return prepare_research_logistic(
        connection, event_id, as_of, history_path,
        model_policy=TrainingPolicy(**selection['selected_policy']),
        selection_manifest_sha256=selection['manifest_sha256'], stats_overlay=overlay)
