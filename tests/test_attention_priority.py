"""Counterfactual priority oracle: selection latency, never investment accuracy."""
from datetime import timedelta
import pytest

from market_agent.attention import EventPacket
from market_agent.attention_selection import candidate_signal, select_candidates, selection_projection
from market_agent.research_universe import UniverseEntry
from test_attention_continuation import receipt
from test_convictiondesk_attention import NOW


def packet(symbol, title, age, key, *, held=False, now=NOW):
    entry=UniverseEntry(symbol=symbol,state='HOLDING' if held else 'WATCHING',
        reason='synthetic',created_at=NOW-timedelta(days=30),updated_at=NOW)
    e=receipt(symbol,title,now-age,now,key)
    return EventPacket(event_key=str(key),evidence=[e],universe=entry,cutoff=now)


def test_regulatory_clue_is_offered_in_arrival_refresh_amid_sustained_routine_flow():
    pending=[];critical_key='regulatory'
    for day in range(10):
        now=NOW+timedelta(days=day)
        pending += [packet(s,'Routine operating note',timedelta(minutes=20+k),f'{day}-{s}-{k}',now=now)
                    for s in ('APP','AAPL','MSFT','NVDA') for k in range(5)]
        if day==3:
            pending.append(packet('SMR','Regulator suspends design review',timedelta(hours=1),critical_key,now=now))
        chosen=select_candidates(pending,now,4)
        if day==3:
            assert critical_key in {p.event_key for p in chosen},'Critical clue must not wait behind ordinary volume'
        assert len(chosen)<=4
        pending=[p for p in pending if p.event_key not in {p.event_key for p in chosen}]


def test_fresh_major_and_deadline_old_material_both_get_capacity():
    old=[packet(s,'Routine old report',timedelta(days=6,hours=20),s) for s in ('APP','AAPL','MSFT','NVDA')]
    fresh=packet('SMR','New company disclosure',timedelta(hours=1),'fresh')
    chosen=select_candidates([*old,fresh],NOW,4)
    assert fresh in chosen
    assert sum(p in old for p in chosen)==3
    assert len({p.universe.symbol for p in chosen})==4
    # No oracle labels this generic new report materially important: the reserved
    # fresh lane is needed even when there is no recognized keyword.
    assert candidate_signal(fresh)==0


def test_watch_list_cannot_be_starved_by_more_than_four_daily_holdings():
    held=[packet(s,'Holding routine note',timedelta(minutes=20),s,held=True)
          for s in ('APP','AAPL','MSFT','NVDA','AMZN')]
    watch=packet('SMR','Watch-list disclosure',timedelta(hours=2),'watch')
    chosen=select_candidates([*held,watch],NOW,4)
    assert chosen[0].universe.state=='HOLDING'
    assert watch in chosen


def test_fresh_signal_arrivals_cannot_erase_expiring_old_deadline_lane():
    old=packet('ORCL','Old ordinary disclosure',timedelta(days=6,hours=20),'deadline')
    arrivals=[packet(s,'Guidance withdrawn',timedelta(minutes=n+1),s,held=True)
              for n,s in enumerate(('APP','AAPL','MSFT','NVDA','AMZN'))]
    chosen=select_candidates([*arrivals,old],NOW,4)
    assert old in chosen
    assert any(p in arrivals for p in chosen)


def test_signal_is_only_replayable_priority_and_does_not_mutate_or_drop_receipts():
    rows=[packet('APP','FDA rejects application',timedelta(hours=2),'risk'),
          packet('SMR','Synthetic ordinary disclosure',timedelta(days=2),'ordinary')]
    before=[p.model_dump_json() for p in rows]
    first=select_candidates(rows,NOW,1)
    assert first==select_candidates(list(reversed(rows)),NOW,1)
    projection=selection_projection(rows,first,NOW,1)
    assert projection['materiality_status']=='UNVERIFIED_UNTIL_SEMANTIC_SCREEN'
    assert projection['offered'][0]['signal_hint']==2
    assert projection['current_candidates']==2 and projection['deferred_current']==1
    assert projection['request_limit']==1 and projection['event_limit']==1
    assert [p.model_dump_json() for p in rows]==before
    assert all(p.evidence[0].structured_data is None for p in rows)


def test_expired_and_future_receipts_are_excluded_but_never_deleted():
    rows=[packet('APP','Guidance withdrawn',timedelta(days=8),'expired'),
          packet('SMR','Guidance withdrawn',timedelta(hours=-1),'future')]
    before=[p.model_dump_json() for p in rows]
    assert select_candidates(rows,NOW,4)==[]
    assert [p.model_dump_json() for p in rows]==before


@pytest.mark.parametrize('title',[
    'Company files for Chapter 11', 'Shares halted pending disclosure',
    'Company cuts full-year outlook', 'CEO resigns following operating review',
    'Auditor resigns over internal controls', 'DOJ opens antitrust probe',
    'FDA issues complete response letter', 'Nasdaq deficiency notice received',
    'Major customer terminates supply contract', 'NRC pauses licensing review',
])
def test_major_event_vocabulary_is_an_unverified_clue_not_a_semantic_verdict(title):
    p=packet('APP',title,timedelta(hours=1),title)
    assert candidate_signal(p)==2
    assert selection_projection([p],[p],NOW,4)['materiality_status']=='UNVERIFIED_UNTIL_SEMANTIC_SCREEN'


def test_routine_earnings_date_and_market_wordplay_are_not_escalated():
    for title in ['Company announces earnings date','Stock halts losing streak as market rebounds']:
        assert candidate_signal(packet('APP',title,timedelta(hours=1),title))==0
