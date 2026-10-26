"""Static contract for the Quiet Intelligence v0.3 Console (no browser, no network).

Browser journeys prove behaviour; these checks keep the safety-relevant wiring from
regressing silently: theme init before first paint under CSP, Today resume as a
read-only GET, POST only behind an explicit confirmed start, and Universe readback.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[1] / 'src/market_agent/static'


def source(name):
    return (STATIC / name).read_text(encoding='utf-8')


def test_theme_initializes_before_styles_without_inline_script():
    html = source('index.html')
    init = html.index('<script src="/static/theme-init.js"></script>')
    assert init < html.index('<link rel="stylesheet"')
    # CSP is script-src 'self': every script must be external and same-origin.
    assert not re.search(r'<script(?![^>]*\bsrc=)[^>]*>', html)
    assert html.index('quiet.css') > html.index('research-desk.css')
    assert re.search(r'<select id="mia-theme" data-theme-control aria-label="外观主题">', html)
    for value in ('auto', 'light', 'dark'):
        assert f'<option value="{value}">' in html


def test_theme_init_persists_preference_and_only_renders_dark_or_light():
    script = source('theme-init.js')
    assert 'mia.theme.v1' in script
    assert 'prefers-color-scheme: dark' in script
    assert 'fetch(' not in script and 'api(' not in script
    assert script.count('try {') >= 1 and 'catch {}' in script  # storage may be unavailable
    css = source('quiet.css')
    assert 'html[data-theme="light"]' in css and 'html[data-theme="dark"]' in css
    assert 'prefers-reduced-motion: reduce' in css


def test_today_resume_is_get_only_and_start_requires_confirmation():
    script = source('attention.js')
    resume = script[script.index('async function observe(id)'):script.index('actions.append(resumeButton')]
    assert '/api/attention/deep/' in resume and 'method:"POST"' not in resume
    assert 'resumeButton.addEventListener("click",()=>{if(deepId && !observing)void observe(deepId);});' in script
    start = script[script.index('deepButton.addEventListener("click"'):script.index('reviewed.addEventListener')]
    assert start.index('await miaConfirmCost(') < start.index('if(!confirmed || observing)return;') < start.index('method:"POST"')
    # A lost observation must never be presented as a failed task.
    assert '任务状态待确认' in script


def test_freshness_labels_and_universe_readback_are_rendered():
    script = source('attention.js')
    for label in ('今日发布', '早前发布 · 待复核', '历史报道 · 待复核', '发布时间未知', '仅来源日期'):
        assert label in script
    assert 'data.historical_items' in script
    assert 'entry.reason' in script and 'entry.owner_note' in script
    assert 'owner_note:$("universe-owner-note").value.trim()' in script


def test_research_view_shows_cutoff_and_dated_price_without_inferring_session():
    script = source('research-desk.js')
    assert '研究截止 ${localZone(task.cutoff)}' in script
    assert '交易日未记录' in script and 'session_date' in script
    assert 'premise.cloneNode(true)' in script
    assert '第 ${task.revision} 轮 · ${local(task.updated_at)}' in script


def test_new_static_assets_are_served_with_csp(tmp_path, monkeypatch):
    monkeypatch.setenv('MIA_DB_PATH', str(tmp_path / 'import-only.db'))
    from market_agent.main import create_app
    client = TestClient(create_app(db_path=tmp_path / 'ui.db', market_fixture='evals/fixtures/market_eod.json'))
    for path in ('/static/theme-init.js', '/static/quiet.css', '/static/cost-dialog.js'):
        response = client.get(path)
        assert response.status_code == 200
        assert "script-src 'self'" in response.headers['content-security-policy']


NATIVE_CONFIRM = re.compile(r'(?<![\w.$])confirm\(')


def test_paid_actions_use_in_app_dialog_and_cancel_is_safe():
    for name in ('attention.js', 'research-desk.js'):
        assert not NATIVE_CONFIRM.search(source(name)), name
    dialog = source('cost-dialog.js')
    # Only an explicit click on the confirm button resolves true.
    assert dialog.count('finish(true)') == 1
    assert """[data-cost="confirm"]').addEventListener("click", () => finish(true))""" in dialog
    for path in ('"cancel", event => { event.preventDefault(); finish(false); }',
                 '"close", () => finish(false)', 'if (!inside) finish(false);',
                 'if (pending) return Promise.resolve(false);'):
        assert path in dialog
    # Cancel holds default focus so Enter alone never confirms a paid action.
    assert """[data-cost="cancel"]').focus();""" in dialog
    html = source('index.html')
    assert html.index('cost-dialog.js') < html.index('attention.js') < html.index('research-desk.js')
    desk = source('research-desk.js')
    # Every research POST sits behind an awaited confirmation.
    assert desk.count('send(') == 5  # definition + four confirmed call sites
    assert 'if (await confirmFollowup("DEEP") && stillShowing(shown)) void send($("desk-followup").value, true, "DEEP");' in desk
    # The related Today item shown in the dialog must still be the one sent.
    assert 'const draft=attentionDraft;if(await confirmNew() && attentionDraft===draft)void send($("desk-question").value,false);' in desk
    # A follow-up/retry confirmation covers only the task revision it showed.
    assert 'const shown=active;if(await confirmFollowup() && stillShowing(shown))void send($("desk-followup").value,true);' in desk
    assert 'const shown=active;if(await confirmRetry() && stillShowing(shown) && active.status==="FAILED")void send(active.question,true);' in desk
    assert 'active.task_id===shown.task_id && active.revision===shown.revision && !historicalView' in desk


def test_home_keeps_short_recent_summary_and_records_is_full_list():
    desk = source('research-desk.js')
    assert 'const HOME_RECENT=3;' in desk
    assert 'const shown=full ? matches : matches.slice(0,HOME_RECENT);' in desk
    assert 'document.body.dataset.consoleView==="history"' in desk
    html = source('index.html')
    assert 'id="desk-recent-search"' in html and 'id="desk-recent-all"' in html


def test_today_long_material_opens_in_drawer_without_hiding_history_or_failures():
    script = source('attention.js')
    assert 'id="attention-detail"' in source('index.html')
    assert 'const earlier=data.earlier_items || data.historical_items || [];' in script
    assert 'earlier.forEach(row=>older.append(compactRow(row)))' in script
    assert 'openDetail(row,r=>[`${r.item.symbol} · 证据与记录`,renderItem(r,"evidence")])' in script
    assert 'openDetail(row,r=>[`${r.item.symbol} · 关注详情`,renderItem(r,"drawer")])' in script
    # A queue refresh redraws an open drawer from the new row; it closes only
    # when that exact item is no longer listed, never showing a stale row.
    assert 'syncDetail([...data.items,...(data.historical_items||[])]);' in script
    assert 'rows.find(r=>r.item.item_id===detailSource.itemId)' in script
    assert 'if(!row){closeDetail(false);return;}' in script
    assert '$("attention-status").classList.toggle("warning",!!incomplete);' in script
    for kept in ('发布 ${publication(e)} · 获取 ${stamp(e.retrieved_at)}', '最近完整成功扫描', 'attention-coverage'):
        assert kept in script
