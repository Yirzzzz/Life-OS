from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.skills.review_weekly_reflection import LlmCallError, _call_llm, _clean_llm_text


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
    def run(
        self,
        payload: Dict[str, Any],
        model_key: str,
        api_key: str,
    ) -> Dict[str, Any]:
        lang = payload.get("lang", "zh")
        node_a, node_a_meta = _node_a(payload, model_key, api_key, lang)
        node_b, node_b_meta = _node_b(payload, model_key, api_key, lang)
        progress_signals = _progress_signals(payload, node_a)
        node_c, node_c_meta = _node_c(
            node_a, node_b, progress_signals, model_key, api_key, lang
        )
        _apply_gate(node_c, node_a)
        if node_c.get("replan_needed"):
            node_b, node_b_meta = _node_b_replan(
                payload, node_c, progress_signals, model_key, api_key, lang
            )
        node_d, node_d_meta = _node_d(
            node_a, node_b, node_c, progress_signals, model_key, api_key, lang
        )
        metrics = node_d.get("metrics", {})
        metrics["node_a"] = node_a_meta
        metrics["node_b"] = node_b_meta
        metrics["node_c"] = node_c_meta
        metrics["node_d"] = node_d_meta
        node_d["metrics"] = metrics
        return {
            "node_a": node_a,
            "node_b": node_b,
            "node_c": node_c,
            "output": node_d,
        }


def _llm_json(
    model_key: str, api_key: str, prompt: str, lang: str
) -> tuple[Optional[Dict[str, Any]], str]:
    if not api_key:
        return None, "llm_no_key"
    try:
        content, _, _, _ = _call_llm(model_key, api_key, prompt, lang)
    except LlmCallError:
        return None, "llm_error"
    if not content:
        return None, "llm_empty"
    cleaned = _clean_llm_text(content)
    try:
        return json.loads(cleaned), ""
    except json.JSONDecodeError:
        return None, "llm_invalid_json"


def _build_prompt_node_a(payload: Dict[str, Any], lang: str) -> str:
    schema = (
        '{"related_events":[{"event":str,"date":"YYYY-MM-DD","source_type":"log|plan|objective|milestone",'
        '"source_id":int,"quote":str,"url":str}],"top_topics":[str],'
        '"coverage_days":int,"window":{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","days":int}}'
    )
    goal = payload.get("goal", {})
    window = payload.get("window", {})
    logs = payload.get("recent_logs", [])
    plans = payload.get("recent_plan_items", [])
    objectives = payload.get("short_term_objectives", [])
    milestones = payload.get("milestones", [])
    if lang == "en":
        return (
            "You are a goal event summarizer. Extract real events related to the goal. "
            "Quote exact phrases only, do NOT fabricate. Return JSON only.\n"
            f"Schema: {schema}\n"
            f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
            f"window: {json.dumps(window, ensure_ascii=False)}\n"
            f"logs: {json.dumps(logs, ensure_ascii=False)}\n"
            f"plans: {json.dumps(plans, ensure_ascii=False)}\n"
            f"objectives: {json.dumps(objectives, ensure_ascii=False)}\n"
            f"milestones: {json.dumps(milestones, ensure_ascii=False)}"
        )
    return (
        "你是目标事件总结器。提取与目标相关的真实事件，只引用原文，不要编造。仅输出JSON。\n"
        f"Schema: {schema}\n"
        f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
        f"window: {json.dumps(window, ensure_ascii=False)}\n"
        f"logs: {json.dumps(logs, ensure_ascii=False)}\n"
        f"plans: {json.dumps(plans, ensure_ascii=False)}\n"
        f"objectives: {json.dumps(objectives, ensure_ascii=False)}\n"
        f"milestones: {json.dumps(milestones, ensure_ascii=False)}"
    )


def _build_prompt_node_b(payload: Dict[str, Any], lang: str) -> str:
    schema = (
        '{"phase_plan":[{"phase":str,"goals":[str],"deliverables":[str],"milestones":[str]}],'
        '"success_criteria":[str],"assumptions":[str]}'
    )
    goal = payload.get("goal", {})
    window = payload.get("window", {})
    if lang == "en":
        return (
            "You are a phased plan generator. Use only goal text and time horizon. "
            "Keep it actionable and not overloaded. Output JSON only.\n"
            f"Schema: {schema}\n"
            f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
            f"window: {json.dumps(window, ensure_ascii=False)}"
        )
    return (
        "你是阶段计划生成器。仅基于目标文本与时间跨度生成可执行计划，不要过载。仅输出JSON。\n"
        f"Schema: {schema}\n"
        f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
        f"window: {json.dumps(window, ensure_ascii=False)}"
    )


def _build_prompt_node_b_replan(
    payload: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    lang: str,
) -> str:
    schema = (
        '{"phase_plan":[{"phase":str,"goals":[str],"deliverables":[str],"milestones":[str]}],'
        '"success_criteria":[str],"assumptions":[str]}'
    )
    goal = payload.get("goal", {})
    instructions = node_c.get("replan_instructions") or []
    if lang == "en":
        return (
            "You are a replan generator. Follow the hard instructions to reduce scope and fit time. "
            "Output JSON only.\n"
            f"Schema: {schema}\n"
            f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
            f"progress: {json.dumps(progress_signals, ensure_ascii=False)}\n"
            f"instructions: {json.dumps(instructions, ensure_ascii=False)}"
        )
    return (
        "你是重规划生成器。必须遵守硬约束指令并根据剩余时间缩小范围。仅输出JSON。\n"
        f"Schema: {schema}\n"
        f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
        f"progress: {json.dumps(progress_signals, ensure_ascii=False)}\n"
        f"instructions: {json.dumps(instructions, ensure_ascii=False)}"
    )


def _build_prompt_node_c(
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
    progress_signals: Dict[str, Any],
    lang: str,
) -> str:
    schema = (
        '{"inferred_intent":str,"current_mode":"搭建|修bug|打磨|上架|学习冲刺|论文冲刺|其他",'
        '"scope_adjustment":{"verdict":"too_big|aligned|too_small|unclear","reason":str,'
        '"adjusted_plan":[{"phase":str,"goals":[str],"deliverables":[str],"milestones":[str]}]},'
        '"risks":[str],"constraints":[str],"confidence":0.0,"alignment":0.0,'
        '"replan_needed":true,"replan_reason":str,"replan_instructions":[str],'
        '"plan_pace_verdict":"ahead|on_track|behind|unclear","pace_gap":0.0,"max_next_steps":1}'
    )
    if lang == "en":
        return (
            "You are a feasibility adjuster. Compare evidence (node_a) vs plan (node_b). "
            "Cite quotes from node_a in the reason. If evidence is sparse, shrink the plan. "
            "Return JSON only.\n"
            f"Schema: {schema}\n"
            f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
            f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
            f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
        )
    return (
        "你是可行性校准器。对比node_a证据与node_b计划，理由必须引用node_a证据。证据少则收敛计划。仅输出JSON。\n"
        f"Schema: {schema}\n"
        f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
        f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
        f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
    )


def _build_prompt_node_d(
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    lang: str,
) -> str:
    schema = (
        '{"progress_summary":str,"highlights":[{"text":str,"evidence":[{"quote":str,"url":str}]}],'
        '"next_steps":[str],"to_improve":[str],"assumptions":[str],"ask_back":str,"notice":str,'
        '"metrics":{"coverage":0.0,"alignment":0.0,"confidence":0.0,"window_start":str,"window_end":str,'
        '"generator_mode":"llm_progress_replan"}}'
    )
    if lang == "en":
        return (
            "You are Node D, the final card synthesizer for a Goal Analysis agent.\n"
            "Your job: produce ONE final card JSON that is useful, grounded, and consistent with Node C decisions.\n"
            "\n"
            "HARD OUTPUT RULES:\n"
            "- Output JSON only. No markdown, no extra text.\n"
            "- Follow the Schema exactly (keys + types). Do not add/remove keys.\n"
            "- Do NOT copy node_a/node_b/node_c verbatim. Synthesize.\n"
            "- Do NOT invent facts/events/metrics/quotes/URLs. If unknown, state uncertainty via assumptions/notice.\n"
            "\n"
            "DECISION INHERITANCE (MUST FOLLOW NODE C):\n"
            "- Treat node_c as the source of truth for whether replanning is needed.\n"
            "- If node_c indicates no replan (or replan_needed=false), do NOT propose a major replan; provide incremental steps only.\n"
            "- If node_c indicates replan_needed=true, align next_steps with the replanned plan (node_b replan output if present).\n"
            "\n"
            "GATING / CONSERVATIVE MODE:\n"
            "- If coverage < 2 OR confidence < 0.5: output only 1–2 minimal, low-risk next_steps. Avoid strong judgments.\n"
            "- Also set notice to explain low evidence coverage. ask_back should request the single most helpful missing info.\n"
            "- If alignment < 0.4 OR verdict == 'too_big': emphasize scope narrowing and smaller milestones.\n"
            "\n"
            "EVIDENCE & HIGHLIGHTS:\n"
            "- highlights must be supported by node_a.related_events.\n"
            "- Each highlight must include evidence list with 1–2 items. Each evidence item contains quote and url.\n"
            "- quote must come from node_a.related_events text/excerpt (short and faithful). url must come from the event if available, else \"\".\n"
            "- If no usable evidence exists, set highlights=[] and rely on notice + ask_back.\n"
            "\n"
            "NEXT_STEPS QUALITY:\n"
            "- next_steps must be concrete actions and prioritized (most important first).\n"
            "- Prefer steps that can be done in 7 days.\n"
            "- Generate at most progress.max_next_steps steps (do NOT generate more then truncate).\n"
            "\n"
            "METRICS:\n"
            "- Fill metrics using node_c if available; keep them consistent with node_c and progress window.\n"
            "- Set metrics.window_start = progress.window_start and metrics.window_end = progress.window_end.\n"
            "- Set metrics.generator_mode = \"llm_progress_replan\".\n"
            "\n"
            f"Schema: {schema}\n"
            f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
            f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
            f"node_c: {json.dumps(node_c, ensure_ascii=False)}\n"
            f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
        )

    return (
        "你是 Node D：Goal Analysis agent 的最终卡片总结器。\n"
        "你的任务：输出一份最终卡片 JSON，要求有用、可执行、并严格继承 Node C 的决策。\n"
        "\n"
        "硬性输出规则：\n"
        
        "- 只输出 JSON，不要 markdown，不要多余文字。\n"
        "- 严格遵循 Schema（字段名+类型），不要增删字段。\n"
        "- 不要原样复读 node_a/node_b/node_c，要做综合归纳。\n"
        "- 不要编造事实/事件/指标/quote/url；不确定就写在 assumptions/notice。\n"
        "\n"
        "决策继承（必须遵守 Node C）：\n"
        "- 将 node_c 视为是否需要 replan 的唯一权威。\n"
        "- 若 node_c 表示不 replan（或 replan_needed=false），不要提出大改计划，只给增量动作。\n"
        "- 若 node_c 表示 replan_needed=true，则 next_steps 必须对齐 replanned 计划（若 node_b 内含 replan 版本则优先使用）。\n"
        "\n"
        "门控 / 保守模式：\n"
        "- 若 coverage < 2 或 confidence < 0.5：next_steps 只给 1–2 条最小、低风险动作；避免强结论。\n"
        "- 同时 notice 说明证据不足；ask_back 提一个最关键的问题以补全信息。\n"
        "- 若 alignment < 0.4 或 verdict == 'too_big'：强调缩小范围、拆小里程碑。\n"
        "\n"
        "证据与 highlights：\n"
        "- highlights 必须由 node_a.related_events 支撑。\n"
        "- 每条 highlight 的 evidence 需 1–2 条，包含 quote 与 url。\n"
        "- quote 必须来自 related_events 的 text/excerpt（短、忠实）；url 若事件未提供则填 \"\"。\n"
        "- 若没有可用证据，则 highlights=[]，并通过 notice + ask_back 处理。\n"
        "\n"
        "next_steps 质量要求：\n"
        "- next_steps 必须是具体可执行动作，并按优先级排序（最重要在前）。\n"
        "- 优先给7天内可完成的步骤。\n"
        "- 最多生成 progress.max_next_steps 条，不要先生成很多再截断。\n"
        "\n"
        "metrics：\n"
        "- metrics 尽量沿用 node_c 的数值，并与 progress 的窗口一致。\n"
        "- metrics.window_start = progress.window_start；metrics.window_end = progress.window_end。\n"
        "- metrics.generator_mode = \"llm_progress_replan\"。\n"
        "\n"
        f"Schema: {schema}\n"
        f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
        f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
        f"node_c: {json.dumps(node_c, ensure_ascii=False)}\n"
        f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
    )


def _validate_node_a(data: Dict[str, Any]) -> bool:
    return bool(
        isinstance(data.get("related_events"), list)
        and isinstance(data.get("top_topics"), list)
        and isinstance(data.get("coverage_days"), int)
        and isinstance(data.get("window"), dict)
    )


def _validate_node_b(data: Dict[str, Any]) -> bool:
    return bool(
        isinstance(data.get("phase_plan"), list)
        and isinstance(data.get("success_criteria"), list)
        and isinstance(data.get("assumptions"), list)
    )


def _validate_node_c(data: Dict[str, Any]) -> bool:
    scope = data.get("scope_adjustment") or {}
    return bool(
        isinstance(data.get("inferred_intent"), str)
        and isinstance(data.get("current_mode"), str)
        and isinstance(scope.get("verdict"), str)
        and isinstance(scope.get("reason"), str)
        and isinstance(scope.get("adjusted_plan"), list)
        and isinstance(data.get("confidence"), (int, float))
        and isinstance(data.get("alignment"), (int, float))
        and isinstance(data.get("replan_needed"), bool)
        and isinstance(data.get("replan_reason"), str)
        and isinstance(data.get("replan_instructions"), list)
        and isinstance(data.get("plan_pace_verdict"), str)
        and isinstance(data.get("pace_gap"), (int, float))
        and isinstance(data.get("max_next_steps"), int)
    )


def _validate_node_d(data: Dict[str, Any]) -> bool:
    return bool(
        isinstance(data.get("progress_summary"), str)
        and isinstance(data.get("highlights"), list)
        and isinstance(data.get("next_steps"), list)
        and isinstance(data.get("to_improve"), list)
        and isinstance(data.get("assumptions"), list)
        and isinstance(data.get("metrics"), dict)
    )


def _coverage_days(payload: Dict[str, Any]) -> int:
    logs = payload.get("recent_logs", []) or []
    return sum(1 for log in logs if _log_has_content(log))


def _coverage_ratio(payload: Dict[str, Any]) -> float:
    window_days = int((payload.get("window") or {}).get("days") or 0)
    if window_days <= 0:
        return 0.0
    return _coverage_days(payload) / window_days


def _progress_signals(payload: Dict[str, Any], node_a: Dict[str, Any]) -> Dict[str, Any]:
    window = payload.get("window", {})
    goal = payload.get("goal", {})
    start_raw = goal.get("start_date") or window.get("start")
    end_raw = goal.get("end_date")
    window_end_raw = window.get("end")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today()
    window_end_date = date.fromisoformat(window_end_raw) if window_end_raw else date.today()
    elapsed_days = max((window_end_date - start_date).days, 0)
    total_days = max((date.fromisoformat(end_raw) - start_date).days, 0) if end_raw else elapsed_days
    if total_days <= 0:
        total_days = max(elapsed_days, 1)
    time_progress = min(max(elapsed_days / total_days, 0.0), 1.0)
    coverage_days = int(node_a.get("coverage_days") or 0)
    coverage_ratio = coverage_days / max(elapsed_days, 1)
    remaining_days = max(total_days - elapsed_days, 0)
    return {
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "time_progress": time_progress,
        "coverage_ratio": coverage_ratio,
        "remaining_days": remaining_days,
    }


def _fallback_node_a(payload: Dict[str, Any]) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    for log in payload.get("recent_logs", []) or []:
        if not _log_has_content(log):
            continue
        quote = ""
        if _has_text(log.get("journal_md", "")):
            quote = _truncate_text(log.get("journal_md", ""), 160)
        else:
            for entry in log.get("period_entries", []) or []:
                if _has_text(entry.get("text", "")):
                    quote = _truncate_text(entry.get("text", ""), 160)
                    break
        if not quote:
            continue
        events.append(
            {
                "event": quote,
                "date": log.get("date"),
                "source_type": "log",
                "source_id": 0,
                "quote": quote,
                "url": _evidence_link("log", log.get("date")),
            }
        )
    window = payload.get("window", {})
    return {
        "related_events": events[:6],
        "top_topics": [],
        "coverage_days": _coverage_days(payload),
        "window": {
            "start": window.get("start"),
            "end": window.get("end"),
            "days": int(window.get("days") or 0),
        },
    }


def _fallback_node_b(payload: Dict[str, Any], lang: str) -> Dict[str, Any]:
    if lang == "en":
        return {
            "phase_plan": [
                {
                    "phase": "Phase 1",
                    "goals": ["Clarify the next deliverable"],
                    "deliverables": ["A small, shippable outcome"],
                    "milestones": ["First usable checkpoint"],
                }
            ],
            "success_criteria": ["One concrete deliverable completed"],
            "assumptions": ["Timeline is flexible"],
        }
    return {
        "phase_plan": [
            {
                "phase": "Phase 1",
                "goals": ["明确下一步可交付成果"],
                "deliverables": ["一个可交付的小成果"],
                "milestones": ["首个可用里程碑"],
            }
        ],
        "success_criteria": ["完成一个具体交付物"],
        "assumptions": ["时间线可调整"],
    }


def _fallback_node_c(node_a: Dict[str, Any], node_b: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "inferred_intent": "Based on recent evidence, focus on immediate progress.",
        "current_mode": "其他",
        "scope_adjustment": {
            "verdict": "unclear",
            "reason": "Evidence is limited; keep scope small.",
            "adjusted_plan": node_b.get("phase_plan") or [],
        },
        "risks": [],
        "constraints": [],
        "confidence": 0.4,
        "alignment": 0.3,
        "replan_needed": False,
        "replan_reason": "Evidence is limited; keep scope small.",
        "replan_instructions": [],
        "plan_pace_verdict": "unclear",
        "pace_gap": 0.0,
        "max_next_steps": 1,
    }


def _fallback_node_d(
    payload: Dict[str, Any],
    node_a: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    lang: str,
) -> Dict[str, Any]:
    window = payload.get("window", {})
    coverage = float(progress_signals.get("coverage_ratio") or 0.0)
    confidence = float(node_c.get("confidence") or 0.0)
    alignment = float(node_c.get("alignment") or 0.0)
    if lang == "en":
        summary = "Recent activity suggests small but real progress."
        next_steps = ["Pick one smallest task you can finish today."]
    else:
        summary = "近期行动显示已有小幅推进。"
        next_steps = ["选一个今天就能完成的小任务。"]
    max_steps = int(node_c.get("max_next_steps") or 1)
    next_steps = next_steps[:max_steps]
    highlights = []
    for ev in (node_a.get("related_events") or [])[:2]:
        highlights.append({"text": ev.get("event", ""), "evidence": [{"quote": ev.get("quote", ""), "url": ev.get("url", "")}]})
    return {
        "progress_summary": summary,
        "highlights": highlights,
        "next_steps": next_steps,
        "to_improve": [],
        "risks": [],
        "assumptions": node_c.get("constraints") or [],
        "ask_back": "",
        "notice": "",
        "metrics": {
            "coverage": coverage,
            "alignment": alignment,
            "confidence": confidence,
            "window_start": window.get("start"),
            "window_end": window.get("end"),
            "generator_mode": "llm_progress_replan",
        },
    }


def _apply_gate(node_c: Dict[str, Any], node_a: Dict[str, Any]) -> None:
    coverage_days = int(node_a.get("coverage_days") or 0)
    confidence = float(node_c.get("confidence") or 0.0)
    alignment = float(node_c.get("alignment") or 0.0)
    verdict = ((node_c.get("scope_adjustment") or {}).get("verdict") or "").strip()
    if coverage_days < 2 or confidence < 0.5:
        node_c["replan_needed"] = False
        node_c["max_next_steps"] = 1
        node_c["plan_pace_verdict"] = "unclear"
        node_c["replan_instructions"] = []
        return
    if alignment < 0.4 or verdict == "too_big":
        node_c["replan_needed"] = True
        node_c["max_next_steps"] = max(1, min(int(node_c.get("max_next_steps") or 2), 2))
        instructions = node_c.get("replan_instructions") or []
        if not instructions:
            instructions = ["缩小范围，优先保留一个核心交付物。"]
        node_c["replan_instructions"] = instructions


def _node_a(
    payload: Dict[str, Any], model_key: str, api_key: str, lang: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_a(payload, lang)
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_a(parsed):
        return parsed, {"mode": "llm"}
    return _fallback_node_a(payload), {"mode": "fallback", "error": error}


def _node_b(
    payload: Dict[str, Any], model_key: str, api_key: str, lang: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_b(payload, lang)
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_b(parsed):
        return parsed, {"mode": "llm"}
    return _fallback_node_b(payload, lang), {"mode": "fallback", "error": error}


def _node_c(
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
    progress_signals: Dict[str, Any],
    model_key: str,
    api_key: str,
    lang: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_c(node_a, node_b, progress_signals, lang)
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_c(parsed):
        return parsed, {"mode": "llm"}
    return _fallback_node_c(node_a, node_b), {"mode": "fallback", "error": error}


def _node_b_replan(
    payload: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    model_key: str,
    api_key: str,
    lang: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_b_replan(payload, node_c, progress_signals, lang)
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_b(parsed):
        return parsed, {"mode": "llm_replan"}
    return _fallback_node_b(payload, lang), {"mode": "fallback", "error": error}


def _node_d(
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    model_key: str,
    api_key: str,
    lang: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_d(node_a, node_b, node_c, progress_signals, lang)
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_d(parsed):
        max_steps = int(node_c.get("max_next_steps") or 3)
        parsed["next_steps"] = (parsed.get("next_steps") or [])[:max_steps]
        metrics = parsed.get("metrics") or {}
        metrics["generator_mode"] = "llm_progress_replan"
        parsed["metrics"] = metrics
        if "risks" not in parsed:
            parsed["risks"] = parsed.get("to_improve") or []
        if node_c.get("replan_needed"):
            assumptions = parsed.get("assumptions") or []
            if not assumptions:
                assumptions = [node_c.get("replan_reason") or "Triggered re-plan based on evidence."]
            parsed["assumptions"] = assumptions
        return parsed, {"mode": "llm"}
    return _fallback_node_d(
        {"window": node_a.get("window", {})},
        node_a,
        node_c,
        progress_signals,
        lang,
    ), {"mode": "fallback", "error": error}


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
