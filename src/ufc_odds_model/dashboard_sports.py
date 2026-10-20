"""Sports-style presentation of a verified saved report; no model or DB writes."""

from __future__ import annotations

import base64
import html
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st

from .dashboard_history import load_dashboard_history


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def font_css(root: Path) -> str:
    """Embed licensed fonts so typography works without a font CDN."""
    rules = []
    for filename, family, weight, style in (
        ('Barlow-Regular.ttf', 'Barlow', 400, 'normal'),
        ('Barlow-SemiBold.ttf', 'Barlow', 600, 'normal'),
        ('Barlow-BlackItalic.ttf', 'Barlow', 900, 'italic'),
        ('BarlowCondensed-BoldItalic.ttf', 'Barlow Condensed', 700, 'italic'),
    ):
        path = root / 'assets/fonts' / filename
        if path.is_file():
            encoded = base64.b64encode(path.read_bytes()).decode('ascii')
            rules.append(f"@font-face{{font-family:'{family}';font-style:{style};font-weight:{weight};"
                         f"font-display:swap;src:url(data:font/ttf;base64,{encoded}) format('truetype');}}")
    return ''.join(rules)


def _portraits(root: Path) -> dict:
    try:
        manifest = json.loads((root / 'assets/fighters/manifest.json').read_text())
        result = {}
        for item in manifest['fighters']:
            image = urlsplit(item['image_url'])
            page = urlsplit(item['source_page'])
            if (image.scheme == page.scheme == 'https'
                    and image.hostname in {'ufc.com', 'www.ufc.com'}
                    and page.hostname in {'ufc.com', 'www.ufc.com'}
                    and not image.username and not image.password):
                result[item['name']] = item
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _image(name: str, portraits: dict, *, miniature: bool = False) -> str:
    item = portraits.get(name)
    kind = 'fighter-thumb' if miniature else 'fighter-photo'
    if not item:
        initials = ''.join(part[0] for part in name.split()[:2])
        return f"<div class='{kind} portrait-unavailable' aria-label='Portrait unavailable'>{_esc(initials)}</div>"
    return (f"<div class='{kind}'><img src='{_esc(item['image_url'])}' "
            f"alt='{_esc(name)} — official UFC portrait' loading='eager' "
            "referrerpolicy='no-referrer'></div>")


def _surname(name: str) -> str:
    return name.split()[-1]


def _fact(bout: dict, side: str, key: str):
    facts = bout.get('measurements', {}).get(side, {}).get('facts', [])
    for fact in facts:
        value = fact.get('value')
        if fact.get('key') == key and isinstance(value, (int, float)) and math.isfinite(value):
            return value
    return None


def _numeric(value, digits=1) -> str:
    return 'Unavailable' if value is None else f'{value:,.{digits}f}'


def _match_strip(report: dict, portraits: dict, event_status: str) -> None:
    cards = []
    for bout in report['bouts']:
        rows = []
        for side in ('a', 'b'):
            name = bout[f'fighter_{side}']
            rows.append(f"<div class='match-strip-fighter'>{_image(name, portraits, miniature=True)}"
                        f"<span>{_esc(_surname(name))}</span><strong>{bout[f'p_fighter_{side}']:.1%}</strong></div>")
        cards.append("<article class='match-tile'>" + ''.join(rows) + '</article>')
    event_date = report['event']['event_date']
    from datetime import date
    day = date.fromisoformat(event_date).strftime('%b %d, %Y').upper()
    event_name = report['event']['name'].split(':')[0]
    status = 'SAVED RESEARCH FORECAST' if event_status == 'upcoming' else 'ARCHIVED RESEARCH FORECAST · EVENT PASSED'
    st.markdown(f"<section id='sports-overview' class='match-strip' aria-label='Saved fight probabilities'>"
                f"<div class='match-strip-heading'><h1>{_esc(event_name)} <span class='event-day'>/ {_esc(day)}</span></h1>"
                f"<h2>More matchups</h2><span>{status}</span></div><div class='match-tiles' tabindex='0' "
                f"aria-label='Scored matchups; scroll horizontally on small screens'>{''.join(cards)}</div></section>",
                unsafe_allow_html=True)


def _spotlight(bout: dict, portraits: dict, context: dict | None = None) -> str:
    cards = []
    roster = [(bout, 'a'), (bout, 'b')]
    if context:
        roster += [(context, 'a'), (context, 'b')]
    for index, (matchup, side) in enumerate(roster):
        name = matchup[f'fighter_{side}']
        reach = _fact(matchup, f'fighter_{side}', 'reach_cm')
        prior = matchup.get('coverage', {}).get(f'fighter_{side}', {}).get('prior_ufc_bouts')
        page = portraits.get(name, {}).get('source_page')
        credit = (f"<a href='{_esc(page)}' target='_blank' rel='noopener noreferrer'>UFC profile</a>"
                  if page else 'Portrait unavailable')
        selection = f' spotlight-selected-{side}' if index < 2 else ''
        cards.append(f"<article class='spotlight-card{selection}'><h3>{_esc(name)}</h3>{_image(name, portraits)}"
                     "<div class='spotlight-facts'>"
                     f"<div><span>Reach</span><strong>{_numeric(reach)}<small>{' cm' if reach is not None else ''}</small></strong></div>"
                     f"<div class='prior-count'><span>Earlier UFC fights</span><strong>{_esc(prior) if prior is not None else 'Unavailable'}</strong></div>"
                     f"</div><div class='portrait-credit'>{credit}</div></article>")
    return "<div class='spotlight-grid'>" + ''.join(cards) + '</div>'


def _rail(root: Path) -> str:
    links = []
    for target, label, icon in (
        ('sports-overview', 'Fight overview', 'karate'),
        ('sports-fighters', 'Fighter spotlight', 'users'),
        ('sports-history', 'Earlier-fight statistics', 'chart-bar'),
        ('sports-card', 'Complete card', 'database'),
        ('sports-record', 'Reports and archive', 'file-download'),
    ):
        data = base64.b64encode((root / 'assets/icons' / f'{icon}.svg').read_bytes()).decode('ascii')
        links.append(f"<a class='sports-rail-link' href='#{target}' aria-label='{label}' title='{label}'>"
                     f"<img alt='' src='data:image/svg+xml;base64,{data}'></a>")
    return "<nav class='sports-rail' aria-label='Overview sections'>" + ''.join(links) + '</nav>'


def _history_chart(history: dict, bout: dict, portraits: dict) -> None:
    if history.get('status') != 'available':
        st.info('Earlier-fight history is unavailable for this saved edition.')
        return
    identity, chart = st.columns([.18, .82], gap='small')
    with identity:
        rows = []
        for side in ('a', 'b'):
            name = bout[f'fighter_{side}']
            fights = history['fighters'].get(name, {}).get('fights', [])
            rows.append(f"<div class='chart-fighter chart-fighter-{side}'>{_image(name, portraits, miniature=True)}"
                        f"<strong>{_esc(_surname(name))}</strong><span>{len(fights)} earlier fights</span></div>")
        st.markdown("<div class='chart-identities'>" + ''.join(rows) + '</div>', unsafe_allow_html=True)
    points = []
    for side in ('a', 'b'):
        name = bout[f'fighter_{side}']
        number = 0
        for fight in history['fighters'].get(name, {}).get('fights', []):
            for observed in fight.get('rounds', []):
                number += 1
                rate = observed.get('significant_strikes_landed_per_minute')
                if rate is None:
                    continue
                points.append({'index': number, 'fighter': name, 'rate': rate,
                               'signed_rate': rate if side == 'a' else -rate,
                               'date': fight['event_date'], 'opponent': fight['opponent'],
                               'round': observed['round_number'], 'landed': observed['significant_strikes_landed'],
                               'seconds': observed['elapsed_seconds']})
    with chart:
        if not points:
            st.info('No earlier rounds have a known duration for this matchup.')
        else:
            names = [bout['fighter_a'], bout['fighter_b']]
            maximum = max(point['rate'] for point in points) or 1
            spec = {
                'data': {'values': points}, 'height': 156,
                'mark': {'type': 'bar', 'cornerRadiusTopLeft': 1, 'cornerRadiusTopRight': 1},
                'encoding': {
                    'x': {'field': 'index', 'type': 'ordinal', 'title': 'Earlier rounds · oldest to newest',
                          'axis': {'labelAngle': 0, 'labelFontSize': 10, 'titleFontSize': 10, 'tickSize': 0, 'domain': False}},
                    'y': {'field': 'signed_rate', 'type': 'quantitative', 'title': None,
                          'scale': {'domain': [-maximum * 1.08, maximum * 1.08]},
                          'axis': {'labelExpr': 'abs(datum.value)', 'labelFontSize': 10, 'tickCount': 5,
                                   'gridColor': '#f0f0f0', 'domain': False, 'tickSize': 0}},
                    'color': {'field': 'fighter', 'type': 'nominal', 'legend': None,
                              'scale': {'domain': names, 'range': ['#adff00', '#8d40ff']}},
                    'tooltip': [{'field': 'fighter', 'title': 'Fighter'}, {'field': 'date', 'title': 'Fight date'},
                                {'field': 'opponent', 'title': 'Opponent'}, {'field': 'round', 'title': 'Round'},
                                {'field': 'landed', 'title': 'Significant strikes landed'},
                                {'field': 'seconds', 'title': 'Observed seconds'},
                                {'field': 'rate', 'title': 'Strikes per minute', 'format': '.2f'}],
                },
                'config': {'view': {'stroke': None}, 'axis': {'labelFont': 'Barlow', 'titleFont': 'Barlow',
                                                           'labelColor': '#a1a1a6', 'titleColor': '#85858d'},
                           'background': 'white'},
            }
            st.vega_lite_chart(spec=spec, width='stretch', height='content', theme=None, key='sports_round_history')
    st.markdown("<p class='history-explanation'>Significant strikes landed per minute across up to five earlier fights. "
                "Each fighter has separate opponents and dates; paired bars are not the same bout or timeline.</p>",
                unsafe_allow_html=True)
    st.markdown(f"<div class='history-model-line'><span>Saved model probability</span>"
                f"<strong>{_esc(_surname(bout['fighter_a']))} {bout['p_fighter_a']:.1%}</strong>"
                f"<strong>{_esc(_surname(bout['fighter_b']))} {bout['p_fighter_b']:.1%}</strong></div>",
                unsafe_allow_html=True)


def _archive_insights(history: dict, lineup: list[dict], portraits: dict) -> str:
    tiles = []
    for bout in lineup:
        for side in ('a', 'b'):
            name = bout[f'fighter_{side}']
            fights = history.get('fighters', {}).get(name, {}).get('fights', [])
            if not fights:
                continue
            fight = fights[-1]
            source = urlsplit(fight['source_url'])
            if source.scheme not in {'http', 'https'} or source.hostname != 'ufcstats.com':
                continue
            tiles.append(f"<a class='archive-insight' href='{_esc(fight['source_url'])}' target='_blank' rel='noopener noreferrer'>"
                         f"{_image(name, portraits, miniature=True)}<div><strong>{_esc(_surname(name))} vs "
                         f"{_esc(fight['opponent'])}</strong><span>{_esc(fight['event_date'])} · UFCStats</span></div></a>")
    return "<div class='archive-insights'>" + ''.join(tiles[:4]) + '</div>'


def _comparison(bout: dict, label: str, key: str, *, unit='', digits=1) -> str:
    a, b = (_fact(bout, f'fighter_{side}', key) for side in ('a', 'b'))
    scale = max(a or 0, b or 0) or 1
    wa, wb = (90 * (value or 0) / scale for value in (a, b))
    return ("<div class='stat-comparison'>"
            f"<strong>{_numeric(a, digits)}{unit if a is not None else ''}</strong>"
            f"<div class='stat-track stat-track-a'><i style='width:{wa:.4f}%'></i></div>"
            f"<span>{_esc(label)}</span>"
            f"<div class='stat-track stat-track-b'><i style='width:{wb:.4f}%'></i></div>"
            f"<strong>{_numeric(b, digits)}{unit if b is not None else ''}</strong></div>")


def _breakdown(bout: dict, mode: str) -> str:
    a, b = bout['p_fighter_a'], bout['p_fighter_b']
    if mode == 'Win probability':
        fields = [('Reach', 'reach_cm', ' cm', 1),
                  ('Sig. strikes / min', 'significant_strikes_landed_per_minute', '', 2),
                  ('Takedowns / 15 min', 'takedowns_average_per_15_minutes', '', 2)]
    else:
        fields = [('Reach', 'reach_cm', ' cm', 1),
                  ('Sig. strikes / min', 'significant_strikes_landed_per_minute', '', 2),
                  ('Strikes absorbed / min', 'significant_strikes_absorbed_per_minute', '', 2),
                  ('Striking accuracy', 'significant_strike_accuracy_display_pct', '%', 0),
                  ('Striking defense', 'significant_strike_defense_pct', '%', 0),
                  ('Takedowns / 15 min', 'takedowns_average_per_15_minutes', '', 2),
                  ('Takedown defense', 'takedown_defense_pct', '%', 0),
                  ('Submissions / 15 min', 'submissions_average_per_15_minutes', '', 2)]
    comparisons = ''.join(_comparison(bout, label, key, unit=unit, digits=digits)
                          for label, key, unit, digits in fields)
    reach_used = any(fact.get('key') == 'reach_cm' and fact.get('used_by_model') is True
                     for side in ('fighter_a', 'fighter_b')
                     for fact in bout.get('measurements', {}).get(side, {}).get('facts', []))
    caption = ('Career strike and grappling averages are descriptive. Model statistics come from earlier fights.'
               + (' Dated reach is a model input in this edition.' if reach_used else ''))
    return (f"<div class='probability-bar' role='img' aria-label='Saved winner probabilities: "
            f"{_esc(bout['fighter_a'])} {a:.1%}, {_esc(bout['fighter_b'])} {b:.1%}'>"
            f"<span class='probability-a' style='width:{a*100:.5f}%'><strong>{a:.1%}</strong></span>"
            f"<span class='probability-b' style='width:{b*100:.5f}%'><strong>{b:.1%}</strong></span></div>"
            f"<div class='probability-names'><span>{_esc(bout['fighter_a'])}</span>"
            f"<span>{_esc(bout['fighter_b'])}</span></div>"
            f"<div class='stat-comparisons'>{comparisons}</div>"
            f"<p class='sports-caption'>{caption}</p>")


def _card_table(report: dict, selected: str) -> str:
    rows = []
    for index, bout in enumerate(report['bouts'], 1):
        pick = 'a' if bout['p_fighter_a'] >= .5 else 'b'
        highlight = " class='selected-matchup'" if bout['bout_id'] == selected else ''
        rows.append(f"<tr{highlight}><td>{index:02}</td><td>{_esc(_surname(bout['fighter_a']))} "
                    f"<span class='versus'>vs</span> {_esc(_surname(bout['fighter_b']))}</td>"
                    f"<td>{_esc(bout.get('weight_class') or 'Unspecified')}</td>"
                    f"<td>{_esc(_surname(bout[f'fighter_{pick}']))}</td>"
                    f"<td><strong>{bout[f'p_fighter_{pick}']:.1%}</strong></td></tr>")
    return ("<div class='sports-table-scroll'><table class='sports-table'><thead><tr>"
            "<th>#</th><th>Matchup</th><th>Weight class</th><th>Model pick</th><th>Probability</th>"
            "</tr></thead><tbody>" + ''.join(rows) + '</tbody></table></div>')


def _record(report: dict) -> str:
    evaluation = report.get('evaluation') or {}
    if not evaluation:
        return "<p class='sports-caption'>No verified evaluation accompanies this report.</p>"
    accuracy = evaluation.get('accuracy')
    loss = evaluation.get('log_loss')
    count = evaluation.get('bouts')
    metrics = [(f'{accuracy:.2%}' if accuracy is not None else 'Unavailable', 'Retrospective accuracy'),
               (_numeric(loss, 5), 'Log loss · lower is better'),
               (f'{count:,}' if count is not None else 'Unavailable', 'Evaluated fights')]
    boxes = ''.join(f"<div><strong>{value}</strong><span>{label}</span></div>" for value, label in metrics)
    recent = evaluation.get('recent_year') or {}
    caveat = (f"{recent.get('year')}: {recent['accuracy']:.2%} accuracy · "
              f"{_numeric(recent.get('log_loss'), 5)} log loss · {recent.get('bouts', 'Unknown')} fights."
              if recent.get('accuracy') is not None else '')
    return (f"<div class='sports-record-metrics'>{boxes}</div>"
            f"<div class='sports-record-note'><strong>{_esc(evaluation.get('label') or 'Saved evaluation')}.</strong>"
            "<p>Historical evaluation does not establish live performance. See the saved evaluation notes below.</p>"
            f"<p>{_esc(caveat)}</p></div>")


def _render_forecast_body(snapshot: dict, root: Path) -> bool:
    """Render the release as a distinct research edition, never operating data."""
    if snapshot.get('status') != 'available':
        return False
    report = snapshot['report']
    portraits = _portraits(root)
    history = load_dashboard_history(root / 'assets/data/dashboard-fight-history.json',
                                     report_sha256=snapshot['artifacts']['json']['sha256'],
                                     cutoff_at_utc=report['forecast']['cutoff_at_utc'])
    _match_strip(report, portraits, snapshot['event_status'])
    left, right = st.columns([.32, .68], gap='small')
    by_id = {bout['bout_id']: bout for bout in report['bouts']}
    with right:
        with st.container(key='sports-breakdown'):
            st.markdown("<h2 id='sports-history' class='sports-heading'>Striking history</h2>", unsafe_allow_html=True)
            with st.container(key='sports-breakdown-body'):
                selected = st.selectbox('Choose a matchup', options=list(by_id),
                                        format_func=lambda key: f"{by_id[key]['fighter_a']} vs {by_id[key]['fighter_b']}",
                                        key='sports_matchup', label_visibility='collapsed')
                mode = st.segmented_control('Comparison view', ['Earlier fight history', 'Fighter statistics'],
                                            default='Earlier fight history', key='sports_comparison',
                                            label_visibility='collapsed', width='stretch')
                if mode == 'Fighter statistics':
                    st.markdown(_breakdown(by_id[selected], mode), unsafe_allow_html=True)
                else:
                    _history_chart(history, by_id[selected], portraits)
    position = list(by_id).index(selected)
    context = report['bouts'][(position + 1) % len(report['bouts'])] if len(report['bouts']) > 1 else None
    with left:
        with st.container(key='sports-spotlight'):
            st.markdown("<h2 id='sports-fighters' class='sports-heading'>Fighter spotlight</h2>", unsafe_allow_html=True)
            st.markdown(_spotlight(by_id[selected], portraits, context), unsafe_allow_html=True)
    card, record = st.columns([.43, .57], gap='small')
    with card:
        with st.container(key='sports-card'):
            st.markdown("<div class='sports-section-title'><h2 id='sports-card' class='sports-heading'>Complete card</h2>"
                        f"<span>{len(report['bouts'])} scored · {len(report['unscored_bouts'])} identity holds</span></div>"
                        + _card_table(report, selected), unsafe_allow_html=True)
            if held := report['unscored_bouts']:
                with st.expander(f"{len(held)} matchups held for identity review"):
                    for item in held:
                        st.markdown(f"**{item['fighter_a']} vs {item['fighter_b']}** — {item['reason']}")
    with record:
        with st.container(key='sports-record'):
            st.markdown("<h2 id='sports-record' class='sports-heading'>From the archive</h2>"
                        + _archive_insights(history, [by_id[selected]] + ([context] if context else []), portraits)
                        + _record(report), unsafe_allow_html=True)
            buttons = st.columns(2, gap='small')
            for column, (kind, label, mime) in zip(buttons, (
                ('pdf', 'Download report', 'application/pdf'),
                ('csv', 'Download CSV', 'text/csv'),
            )):
                artifact = snapshot['artifacts'].get(kind)
                if artifact:
                    with column:
                        path = root / artifact['path']
                        st.download_button(label, data=path.read_bytes(), file_name=path.name,
                                           mime=mime, key=f'sports_download_{kind}', width='stretch')
    cutoff = report['forecast']['cutoff_at_utc'].replace('T', ' ').replace('Z', ' UTC')
    st.markdown("<div class='sports-edition-footer'>"
                f"<span>Saved research edition · Cutoff {_esc(cutoff)} · No live odds</span>"
                "<span>Fighter portraits: <a href='https://www.ufc.com/athletes' target='_blank' "
                "rel='noopener noreferrer'>UFC.com</a></span></div>", unsafe_allow_html=True)
    with st.expander('Forecast sources, coverage and limitations'):
        st.caption(report['forecast']['model_label'])
        if note := report.get('evaluation', {}).get('note'):
            st.write(note)
        for note in report.get('source_notes', []):
            st.write(note)
        for note in report.get('limitations', []):
            st.write(note)
        for source in report.get('sources', []):
            st.markdown(f"[{source['label']}]({source['url']})")
    return True


def render_saved_forecast(snapshot: dict, root: Path) -> bool:
    if snapshot.get('status') != 'available':
        return False
    with st.container(key='sports-shell'):
        st.markdown(_rail(root), unsafe_allow_html=True)
        _render_forecast_body(snapshot, root)
    return True
