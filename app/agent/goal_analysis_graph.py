from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _normalize_text(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def _truncate_text(text: str, limit: int = 160) -> str:
    cleaned = _normalize_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[:limit]


def _extract_tokens(text: str) -> List[str]:
    cleaned = _normalize_text(text).lower()
    tokens = re.split(r"[^a-z0-9\u4e00-\u9fff]+", cleaned)
    return [token for token in tokens if len(token) >= 2]


def _has_text(text: str) -> bool:
    return bool(_normalize_text(text))


def _log_has_content(log: Dict[str, Any]) -> bool:
    if not log:
        return False
    if _has_text(log.get("journal_md", "")):
        return True
    for entry in log.get("period_entries", []) or []:
        if _has_text(entry.get("text", "")):
            return True
        if entry.get("tags"):
            return True
    if log.get("tags"):
        return True
    return False


def _evidence_link(source_type: str, target_date: Optional[str]) -> str:
    if source_type == "log" and target_date:
        return f"/logs?target_date={target_date}"
    if source_type == "plan" and target_date:
        return f"/plans?target_date={target_date}"
    return "/goals"


def _pick_current_mode(text: str) -> str:
    text = _normalize_text(text).lower()
    if any(keyword in text for keyword in ("bug", "fix", "修复", "缺陷")):
        return "修bug"
    if any(keyword in text for keyword in ("refactor", "polish", "打磨", "优化")):
        return "打磨"
    if any(keyword in text for keyword in ("deploy", "launch", "release", "上线", "发布")):
        return "上架"
    if any(keyword in text for keyword in ("learn", "study", "course", "学习", "课程")):
        return "学习冲刺"
    if any(keyword in text for keyword in ("paper", "thesis", "论文")):
        return "论文冲刺"
    if any(keyword in text for keyword in ("build", "setup", "搭建", "框架")):
        return "搭建"
    return "推进中"


def _confidence_score(evidence_count: int, source_types: int) -> float:
    if evidence_count <= 0:
        return 0.2
    base = 0.35 + 0.08 * min(evidence_count, 6) + 0.05 * min(source_types, 4)
    return max(0.1, min(base, 1.0))


def _next_steps_minimal(lang: str) -> List[str]:
    if lang == "en":
        return ["Pick one smallest task you can finish today.", "Capture 1 short log entry."]
    return ["选一个今天就能完成的小任务。", "补一条最短的日志或计划记录。"]


def _next_steps_stable(lang: str) -> List[str]:
    if lang == "en":
        return [
            "Turn the top evidence item into a concrete task for the next 48 hours.",
            "Reserve one focused block and log the outcome.",
        ]
    return ["把最关键的证据项转成 48 小时内可完成的任务。", "预留一个专注时间块并记录结果。"]


def _summarize_progress(goal: Dict[str, Any], window_days: int, lang: str) -> str:
    title = goal.get("title", "")
    if lang == "en":
        return f"In the last {window_days} days, your effort on \"{title}\" shows a clear trace."
    return f"最近 {window_days} 天，你在「{title}」上的行动有迹可循。"


def _assumptions(lang: str) -> List[str]:
    if lang == "en":
        return ["Based on recent logs/plans and linked objectives."]
    return ["基于最近日志/计划与目标关联信息推断。"]


def _build_highlights(
    evidence: Sequence[Dict[str, Any]], lang: str, max_items: int = 3
) -> List[Dict[str, Any]]:
    highlights: List[Dict[str, Any]] = []
    for ev in evidence[:max_items]:
        text = ev.get("quote") or ""
        if not text:
            continue
        highlights.append({"text": text, "evidence_ids": [ev.get("id")]})
    if not highlights and lang == "en":
        highlights.append({"text": "No strong evidence highlights yet.", "evidence_ids": []})
    if not highlights and lang != "en":
        highlights.append({"text": "还没有足够的证据亮点。", "evidence_ids": []})
    return highlights


@dataclass
class GoalAnalysisGraph:
    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        lang = payload.get("lang", "zh")
        node_a = _intent_inference(payload, lang)
        node_b = _evidence_collector(payload, node_a, lang)
        node_c = _synthesize(payload, node_a, node_b, lang)
        return {
            "intent": node_a,
            "evidence": node_b,
            "output": node_c,
        }


def _intent_inference(payload: Dict[str, Any], lang: str) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    for log in payload.get("recent_logs", []) or []:
        if not _log_has_content(log):
            continue
        date_value = log.get("date")
        quote = ""
        if _has_text(log.get("journal_md", "")):
            quote = _truncate_text(log.get("journal_md", ""), 160)
        else:
            for entry in log.get("period_entries", []) or []:
                if _has_text(entry.get("text", "")):
                    quote = _truncate_text(entry.get("text", ""), 160)
                    break
        if not quote:
            tags = log.get("tags") or []
            if tags:
                quote = _truncate_text(", ".join(tags), 120)
        if quote:
            evidence.append(
                {
                    "id": f"log-{date_value}",
                    "quote": quote,
                    "source_type": "log",
                    "date": date_value,
                    "link": _evidence_link("log", date_value),
                }
            )

    for item in payload.get("recent_plan_items", []) or []:
        title = _normalize_text(item.get("title", ""))
        if not title:
            continue
        date_value = item.get("date")
        quote = title
        if _has_text(item.get("note", "")):
            quote = _truncate_text(f"{title} - {item.get('note')}", 160)
        evidence.append(
            {
                "id": f"plan-{date_value}-{len(evidence)}",
                "quote": quote,
                "source_type": "plan",
                "date": date_value,
                "link": _evidence_link("plan", date_value),
            }
        )

    for obj in payload.get("short_term_objectives", []) or []:
        title = _normalize_text(obj.get("title", ""))
        if not title:
            continue
        date_value = obj.get("due_date")
        quote = title
        if _has_text(obj.get("note", "")):
            quote = _truncate_text(f"{title} - {obj.get('note')}", 160)
        evidence.append(
            {
                "id": f"objective-{obj.get('id')}",
                "quote": quote,
                "source_type": "objective",
                "date": date_value,
                "link": _evidence_link("objective", date_value),
            }
        )

    for milestone in payload.get("milestones", []) or []:
        title = _normalize_text(milestone.get("title", ""))
        if not title:
            continue
        date_value = milestone.get("due_date")
        evidence.append(
            {
                "id": f"milestone-{milestone.get('id')}",
                "quote": title,
                "source_type": "milestone",
                "date": date_value,
                "link": _evidence_link("milestone", date_value),
            }
        )

    evidence_sorted = sorted(
        evidence,
        key=lambda ev: ev.get("date") or "",
        reverse=True,
    )
    evidence_quotes = evidence_sorted[:6]
    combined_text = " ".join(ev.get("quote", "") for ev in evidence_quotes)
    current_mode = _pick_current_mode(combined_text)
    source_types = len({ev.get("source_type") for ev in evidence_quotes})
    confidence = _confidence_score(len(evidence_quotes), source_types)

    if lang == "en":
        intent_summary = "Focus inferred from recent logs, plans, objectives, and milestones."
    else:
        intent_summary = "基于日志、计划与目标关联信息推断当前推进重点。"

    constraints: List[str] = []
    if not evidence_quotes:
        constraints.append("最近证据较少，难以判断推进强度。")
    if payload.get("goal", {}).get("end_date"):
        constraints.append("受目标截止日期影响需要关注节奏。")

    return {
        "user_intent_summary": intent_summary,
        "current_mode": current_mode,
        "constraints": constraints,
        "evidence_quotes": evidence_quotes,
        "confidence": confidence,
    }


def _evidence_collector(
    payload: Dict[str, Any], intent: Dict[str, Any], lang: str
) -> Dict[str, Any]:
    logs = payload.get("recent_logs", []) or []
    window = payload.get("window", {})
    window_days = int(window.get("days") or 0)
    logged_days = sum(1 for log in logs if _log_has_content(log))
    coverage = (logged_days / window_days) if window_days else 0.0

    goal = payload.get("goal", {})
    goal_tokens = _extract_tokens(f"{goal.get('title', '')} {goal.get('description_md', '')}")
    matched_evidence: List[Dict[str, Any]] = []
    match_hits = 0
    raw_evidence = payload.get("raw_evidence", []) or []
    for ev in raw_evidence:
        text = ev.get("text", "")
        tokens = set(_extract_tokens(text))
        match = bool(goal_tokens and tokens.intersection(goal_tokens))
        score = 1.0 if match else 0.0
        if match:
            match_hits += 1
        matched_evidence.append(
            {
                "id": ev.get("id"),
                "quote": ev.get("text"),
                "link": ev.get("link"),
                "source_type": ev.get("source_type"),
                "date": ev.get("date"),
                "score": score,
            }
        )
    alignment = (match_hits / len(raw_evidence)) if raw_evidence else 0.0
    if not raw_evidence and lang != "en":
        alignment = 0.0

    return {
        "coverage": coverage,
        "alignment": alignment,
        "matched_evidence": matched_evidence[:8],
        "raw_evidence_count": len(raw_evidence),
    }


def _gate_outputs(confidence: float, coverage: float, alignment: float) -> str:
    if confidence < 0.5 or coverage < (2 / 7):
        return "conservative"
    if alignment >= 0.35 and confidence >= 0.5:
        return "stable"
    return "conservative"


def _synthesize(
    payload: Dict[str, Any],
    intent: Dict[str, Any],
    evidence: Dict[str, Any],
    lang: str,
) -> Dict[str, Any]:
    goal = payload.get("goal", {})
    window_days = int(payload.get("window", {}).get("days") or 0)
    confidence = float(intent.get("confidence") or 0.0)
    coverage = float(evidence.get("coverage") or 0.0)
    alignment = float(evidence.get("alignment") or 0.0)
    gate_mode = _gate_outputs(confidence, coverage, alignment)

    progress_summary = _summarize_progress(goal, window_days, lang)
    highlights = _build_highlights(intent.get("evidence_quotes", []), lang)
    risks: List[str] = []
    if gate_mode == "conservative" and lang != "en":
        risks.append("证据不足，建议先补齐记录再扩展计划。")
    if gate_mode == "conservative" and lang == "en":
        risks.append("Evidence is sparse; keep the plan small for now.")

    if gate_mode == "stable":
        next_steps = _next_steps_stable(lang)
    else:
        next_steps = _next_steps_minimal(lang)

    if gate_mode == "stable":
        next_steps = next_steps[:3]
    else:
        next_steps = next_steps[:2]

    return {
        "progress_summary": progress_summary,
        "highlights": highlights,
        "risks": risks[:2],
        "next_steps": next_steps,
        "assumptions": _assumptions(lang),
        "ask_back": "",
        "notice": payload.get("llm_notice", ""),
        "metrics": {
            "coverage": coverage,
            "alignment": alignment,
            "confidence": confidence,
            "window_start": payload.get("window", {}).get("start"),
            "window_end": payload.get("window", {}).get("end"),
            "generator_mode": payload.get("generator_mode", "rules"),
        },
    }
