"""Read-only research brief from persisted, validated evidence. No model calls."""

from decimal import Decimal

from .console import _percent, run_metrics
from .domain import RunRecord


IMPACT = {
    "strengthens": "近期事件支持现有投资逻辑",
    "weakens": "近期事件削弱现有投资逻辑",
    "mixed": "近期事件带来相互交织的影响",
    "no_material_change": "尚未看到投资逻辑的实质变化",
    "insufficient": "现有证据不足以判断逻辑是否改变",
}
LEVEL = {"none": "无", "low": "低", "medium": "中", "high": "高", "insufficient": "证据不足"}
COMPARE_FIELDS = {"event_materiality": "事件重要性", "direction": "影响方向",
                  "persistence": "持续性", "thesis_impact": "投资逻辑影响",
                  "execution_risk": "执行风险"}


def _comparison(record: RunRecord, judgments: list) -> dict | None:
    primary = judgments[0].structured_data["assessment"]
    if len(judgments) > 1:
        secondary = judgments[1].structured_data["assessment"]
        differences = [
            {"dimension": title, "primary": primary[key], "comparison": secondary[key]}
            for key, title in COMPARE_FIELDS.items() if primary[key] != secondary[key]
        ]
        insufficient = any(secondary[key] == "insufficient" for key in COMPARE_FIELDS)
        return {"status": "insufficient" if insufficient else "different" if differences else "aligned",
                "primary_model": primary["model"], "comparison_model": secondary["model"],
                "comparison_judge": secondary["judge"],
                "differences": differences,
                "aligned_dimensions": [title for key, title in COMPARE_FIELDS.items() if primary[key] == secondary[key]],
                "latency_ms": next((event.latency_ms for event in record.trace.model_events
                                    if event.purpose.endswith("comparison") and event.status == "success"), None) if record.trace else None,
                "primary_evidence_id": judgments[0].evidence_id,
                "comparison_evidence_id": judgments[1].evidence_id}
    if record.trace and any(event.purpose.endswith("comparison") and event.status == "error"
                            for event in record.trace.model_events):
        return {"status": "unavailable", "primary_model": primary["model"],
                "comparison_model": None, "comparison_judge": None, "differences": [],
                "primary_evidence_id": judgments[0].evidence_id,
                "comparison_evidence_id": None}
    return {"status": "not_configured", "primary_model": primary["model"],
            "comparison_model": None, "comparison_judge": None,
            "differences": [], "primary_evidence_id": judgments[0].evidence_id,
            "comparison_evidence_id": None}


def research_presentation(record: RunRecord) -> dict:
    """Display precision changes here; authoritative claims/evidence remain untouched."""
    answer = record.answer
    if answer is None:
        return {"kind": "unavailable", "headline": "这次研究未完成", "facts": [], "evidence_ids": []}
    if not answer.claims and not answer.evidence:
        return {"kind": "unsupported", "headline": "目前还不能分析这个问题",
                "deck": answer.answer, "facts": [], "evidence_ids": [], "caution": "不会在缺少已验证工具时生成投资判断。"}
    metrics = run_metrics(record)
    if metrics["cards"]:
        item = next(item for item in answer.evidence if item.evidence_id == metrics["evidence_id"])
        result = item.structured_data["result"]
        sectors = result["sector_weights"]
        top = result["top_holding"]
        scope = "已定价部分" if metrics["partial"] else "当前组合"
        classified_sectors = {name: weight for name, weight in sectors.items() if name != "待分类"}
        unknown_weight = Decimal(sectors.get("待分类", "0"))
        if classified_sectors and max(Decimal(value) for value in classified_sectors.values()) > unknown_weight:
            sector, weight = max(classified_sectors.items(), key=lambda row: Decimal(row[1]))
            headline = f"{scope}已分类行业中，{sector} 的敞口最大"
            deck = f"该行业占已定价股票市值的 {_percent(Decimal(weight))}。"
        elif top:
            headline = f"{scope}的最大账户持仓是 {top['symbol']}"
            deck = "行业分类尚不完整，可先从单一持仓与账户分布查看集中情况。"
        else:
            headline = "暂时无法判断组合集中风险"
            deck = "当前缺少有效股价；持仓数量与成本仍然保留。"
        facts = [] if not top else [
            {"label": "最大账户持仓", "value": _percent(Decimal(top["nav_weight"])),
             "note": f"{top['symbol']} · 占净值"},
            {"label": "前三项账户持仓", "value": _percent(Decimal(result["top_three_nav_weight"])),
             "note": "占净值 · 按账户持仓计算"},
            {"label": "集中度 HHI", "value": f"{Decimal(result['hhi']):,.2f}",
             "note": "按已定价权益计算 · 越高越集中"},
        ]
        return {"kind": "daily", "headline": headline, "deck": deck, "facts": facts,
                "evidence_ids": [item.evidence_id], "partial": metrics["partial"],
                "caution": "部分持仓缺价；以下比例仅基于已定价部分，不能代表完整组合。"
                if metrics["partial"] else "集中度描述敞口分布，不等于超出你的风险承受范围。"}
    judgments = [item for item in answer.evidence if item.evidence_type == "semantic_judgment"
                 and (item.structured_data or {}).get("assessment")]
    if judgments:
        primary = judgments[0]
        assessment = primary.structured_data["assessment"]
        has_draft = any(item.evidence_type == "thesis" and
                        (item.structured_data or {}).get("status") == "AI_DRAFT"
                        for item in answer.evidence)
        return {
            "kind": "event", "headline": IMPACT.get(assessment["thesis_impact"], "现有证据不足"),
            "deck": "基于本次新闻与公司披露，评估事件对投资逻辑的影响。事件研究不使用判断价格，也不提供增持或减持建议；需要价格相关的行动比较，请使用持仓决策简报。",
            "facts": [
                {"label": "事件重要性", "value": LEVEL[assessment["event_materiality"]], "note": "对现有投资逻辑"},
                {"label": "判断把握", "value": LEVEL[assessment["confidence"]], "note": "针对证据，不是收益概率"},
                {"label": "执行风险", "value": LEVEL[assessment["execution_risk"]], "note": "本次材料反映的风险"},
            ],
            "evidence_ids": [primary.evidence_id],
            "caution": "投资逻辑由 AI 起草，尚未经你确认。" if has_draft else "判断仅覆盖本次检索到的材料。",
            "primary_model": assessment["model"],
            "comparison": _comparison(record, judgments),
        }
    return {"kind": "unavailable", "headline": "暂未形成可用的研究结论",
            "deck": "请查看本次数据限制，或选择已支持的研究主题。",
            "facts": [], "evidence_ids": [], "caution": "证据不足时不作推断。"}
