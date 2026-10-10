"""A shared, explicit threshold for verified model/market disagreement."""
import math


def validate_threshold(value):
    if not isinstance(value,(float,int)) or isinstance(value,bool) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("max_market_disagreement must be finite and within [0, 1]")
    return float(value)


def disagreement(model, market, threshold=.10):
    threshold = validate_threshold(threshold)
    if market is None or market == "":
        return None, False
    if not all(isinstance(p,(float,int)) and not isinstance(p,bool) and math.isfinite(p) and 0 <= p <= 1
               for p in (model,market)):
        raise ValueError("Model/market probabilities must be finite and within [0, 1]")
    gap = abs(model-market)
    return gap, gap > threshold


def verified_market(connection, bout, cutoff, max_age_hours, *, snapshot=None, bookmaker=None):
    from .features import FeatureRow
    from .evaluation import _bookmaker_probability
    row = FeatureRow(str(bout["bout_id"]),str(bout["event_id"]),"",str(bout["fighter_a_id"]),
                     str(bout["fighter_b_id"]),())
    return _bookmaker_probability(connection,row,cutoff,max_age_hours,{},
                                 required_snapshot_at_utc=snapshot,bookmaker=bookmaker)
