"""Small owner-prose regression checks, not a language or factual-quality grader."""
import re

ENGLISH_SENTENCE = re.compile(r"(?:[A-Za-z][A-Za-z'’-]*[\s,;:/()-]+){7,}[A-Za-z][A-Za-z'’-]*")
UNTRANSLATED_LABEL = re.compile(r"\b(?:AI_DRAFT|INSUFFICIENT_EVIDENCE|RESEARCH_FIRST)\b")


def language_issues(texts, source_titles=()):
    issues = []
    for index, text in enumerate(texts):
        prose = str(text)
        for title in source_titles:
            if title:
                prose = prose.replace(title, "")
        if ENGLISH_SENTENCE.search(prose):
            issues.append(f"{index}:long_english_sentence")
        if UNTRANSLATED_LABEL.search(prose):
            issues.append(f"{index}:untranslated_state")
    return issues


def decision_prose(result):
    from .tdb_semantic import depth_statements, price_statements
    return [result.thesis_delta.rationale, result.assessment.abstention_reason or "",
            *[r.rationale for r in result.assessment.rationales],
            *([s.text for s in depth_statements(result.depth)] if result.depth else []),
            *(result.depth.missing_context if result.depth else []),
            *([s.text for s in price_statements(result.price_assessment)] if result.price_assessment else [])]
