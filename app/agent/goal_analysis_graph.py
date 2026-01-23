from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.skills.review_weekly_reflection import LlmCallError, _call_llm, _clean_llm_text


def _normalize_text(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def _truncate_text(text: str, limit: int = 160) -> str:
    cleaned = _normalize_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[:limit]


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




@dataclass
class GoalAnalysisGraph:
    def run(
        self,
        payload: Dict[str, Any],
        model_key: str,
        api_key: str,
    ) -> Dict[str, Any]:
        lang = payload.get("lang", "zh")
        node_a, node_b, node_a_meta, node_b_meta = _run_parallel_ab(
            payload, model_key, api_key, lang
        )
        node_b_original = node_b
        node_b_replan = None
        progress_signals = _progress_signals(payload, node_a)
        node_c, node_c_meta = _node_c(
            node_a, node_b, progress_signals, model_key, api_key, lang
        )
        _apply_gate(node_c, node_a)
        if node_c.get("replan_needed"):
            node_b, node_b_meta = _node_b_replan(
                payload, node_c, progress_signals, model_key, api_key, lang
            )
            node_b_replan = node_b
        node_d, node_d_meta = _node_d(
            node_a,
            node_b,
            node_b_original,
            node_b_replan,
            node_c,
            progress_signals,
            model_key,
            api_key,
            lang,
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

def _coverage_days(payload: Dict[str, Any]) -> int:
    logs = payload.get("recent_logs", []) or []
    return sum(1 for log in logs if _log_has_content(log))


def _coverage_ratio(payload: Dict[str, Any]) -> float:
    window_days = int((payload.get("window") or {}).get("days") or 0)
    if window_days <= 0:
        return 0.0
    return _coverage_days(payload) / window_days


def _run_parallel_ab(
    payload: Dict[str, Any],
    model_key: str,
    api_key: str,
    lang: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(_node_a, payload, model_key, api_key, lang)
        future_b = executor.submit(_node_b, payload, model_key, api_key, lang)
        node_a, node_a_meta = future_a.result()
        node_b, node_b_meta = future_b.result()
    return node_a, node_b, node_a_meta, node_b_meta
def _build_prompt_node_a(payload: Dict[str, Any], lang: str) -> str:
    schema = (
        '{"evidence":[{"id":int,"date":"YYYY-MM-DD","source_type":"log|plan|objective|milestone|next_step_feedback","source_id":int,'
        '"quote":str,"url":str,"tags":[str]}],'
        '"habit_summary":{"top_patterns":[{"text":str,"evidence_ids":[int]}],'
        '"blockers":[{"text":str,"evidence_ids":[int]}],'
        '"triggers":[{"text":str,"evidence_ids":[int]}],'
        '"momentum":"up|down|flat|unknown"},'
        '"coverage":{"coverage_days":int,"window":{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","days":int}}}'
    )
    goal = payload.get("goal", {})
    window = payload.get("window", {})
    logs = payload.get("recent_logs", [])
    plans = payload.get("recent_plan_items", [])
    objectives = payload.get("short_term_objectives", [])
    milestones = payload.get("milestones", [])
    next_step_feedback = payload.get("recent_next_step_feedback", [])

    if lang == "en":
        return (
            "Role: Node A — Goal-Scoped Evidence Summarizer (STRICTLY goal-relevant).\n"
            "Goal: extract ONLY evidence and habit patterns that are DIRECTLY relevant to the given goal.\n"
            "\n"
            "SCOPE CONSTRAINT (MOST IMPORTANT):\n"
            "- You MUST only use logs/plans/objectives/milestones/next_step_feedback that are clearly related to goal.title/goal.description_md.\n"
            "- Relevance definition: an item is relevant only if it (a) describes work/action/outcome toward the goal, or\n"
            "  (b) describes a blocker/trigger that directly affects progress on the goal.\n"
            "- If you cannot explain the connection to the goal, it is NOT relevant and MUST be excluded.\n"
            "\n"
            "GOAL ANCHORING:\n"
            "- First, derive 5–12 short 'goal anchors' (keywords/phrases) from goal.title and goal.description_md.\n"
            "- Then select evidence ONLY when it matches these anchors lexically or semantically.\n"
            "- Prefer items that mention goal outputs/deliverables/milestones explicitly.\n"
            "\n"
            "EXCLUSION RULES:\n"
            "- Do NOT summarize generic lifestyle habits (sleep/mood/exercise/diet) unless the goal is explicitly about them.\n"
            "- Do NOT include unrelated productivity habits (e.g., journaling) unless explicitly tied to goal progress.\n"
            "- Do NOT 'helpfully' infer relevance; require explicit textual support in the quote.\n"
            "\n"
            "HARD RULES:\n"
            "- Output JSON only. No markdown, no extra text.\n"
            "- Do NOT fabricate. Every conclusion must cite evidence_ids.\n"
            "- evidence[].quote MUST be an exact excerpt from the input (verbatim).\n"
            "- tags are 1–3 short labels such as: goal_progress, goal_action, goal_deliverable, blocker, trigger, plan_step, milestone.\n"
            "\n"
            "WHAT TO EXTRACT:\n"
            "1) evidence: pick up to 10 most informative GOAL-RELEVANT items (prefer logs). Assign incremental id starting from 1.\n"
            "- You can use next_step_feedback as evidence when it directly reflects goal-related actions or decisions.\n"
            "2) habit_summary:\n"
            "   - top_patterns: 1–3 recurring goal-related behavior patterns you can prove from evidence.\n"
            "   - blockers: 1–3 recurring goal-related blockers.\n"
            "   - triggers: 1–3 recurring goal-related triggers.\n"
            "   - momentum: up/down/flat/unknown based ONLY on goal-relevant evidence.\n"
            "\n"
            "CONSERVATIVE MODE:\n"
            "- If you find fewer than 2 goal-relevant evidence items, keep habit_summary lists empty and set momentum=unknown.\n"
            "\n"
            f"Schema: {schema}\n"
            f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
            f"window: {json.dumps(window, ensure_ascii=False)}\n"
            f"logs: {json.dumps(logs, ensure_ascii=False)}\n"
            f"plans: {json.dumps(plans, ensure_ascii=False)}\n"
            f"objectives: {json.dumps(objectives, ensure_ascii=False)}\n"
            f"milestones: {json.dumps(milestones, ensure_ascii=False)}\n"
            f"next_step_feedback: {json.dumps(next_step_feedback, ensure_ascii=False)}"
        )

    return (
        "角色：Node A — 目标范围证据总结器（严格只总结与目标相关的内容）。\n"
        "目标：只提取与 goal.title / goal.description_md 直接相关的证据与习惯模式。\n"
        "\n"
        "范围约束（最重要，必须遵守）：\n"
        "- 你只能使用与目标明确相关的 logs/plans/objectives/milestones/next_step_feedback。\n"
        "- “相关”的定义：一条记录必须 (a) 直接描述推进目标的行动/产出/结果，或 (b) 直接描述影响该目标推进的阻碍/触发因素。\n"
        "- 如果你无法用一句话解释“它如何影响这个目标”，则判定为不相关，必须排除。\n"
        "\n"
        "目标锚点（必须执行）：\n"
        "- 先从 goal.title 与 goal.description_md 中提炼 5–12 个“目标锚点关键词/短语”。\n"
        "- evidence 只能从“与锚点词字面匹配或语义匹配”的记录中选择。\n"
        "- 优先选择明确提到交付物/里程碑/可验收结果的记录。\n"
        "\n"
        "排除规则（强制）：\n"
        "- 不要总结泛化生活习惯（睡眠/情绪/运动/饮食），除非目标本身就是这些主题。\n"
        "- 不要总结与目标无关的通用效率习惯（例如随手记日志），除非原文明确说明它用于推进该目标。\n"
        "- 不要“善意推断”相关性；相关性必须能从 quote 原文中直接看出来。\n"
        "\n"
        "硬性规则（必须遵守）：\n"
        "- 只输出 JSON，不要 markdown，不要多余文字。\n"
        "- 不得编造。任何结论都必须给 evidence_ids。\n"
        "- evidence[].quote 必须是输入中的原文片段（逐字一致）。\n"
        "- tags 控制为 1–3 个短标签：goal_progress/goal_action/goal_deliverable/blocker/trigger/plan_step/milestone 等。\n"
        "\n"
        "你需要输出：\n"
        "1) evidence：最多 10 条“最关键的目标相关证据”（优先 logs），id 从 1 递增。\n"
        "- 可以使用 next_step_feedback 作为证据，但必须直接反映与目标相关的行动或决策。\n"
        "2) habit_summary（也必须是目标相关）：\n"
        "   - top_patterns：1–3 条“反复出现的、与目标推进相关的行为模式”（要能被证据支撑）。\n"
        "   - blockers：1–3 条“反复出现的、影响目标推进的阻碍/卡点”。\n"
        "   - triggers：1–3 条“反复出现的、能促进目标推进的触发器/助推因素”。\n"
        "   - momentum：up/down/flat/unknown（只能基于目标相关证据做保守判断）。\n"
        "\n"
        "保守模式：\n"
        "- 若找到的目标相关证据少于 2 条：top_patterns/blockers/triggers 置空数组，并设 momentum=unknown。\n"
        "\n"
        f"Schema: {schema}\n"
        f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
        f"window: {json.dumps(window, ensure_ascii=False)}\n"
        f"logs: {json.dumps(logs, ensure_ascii=False)}\n"
        f"plans: {json.dumps(plans, ensure_ascii=False)}\n"
        f"objectives: {json.dumps(objectives, ensure_ascii=False)}\n"
        f"milestones: {json.dumps(milestones, ensure_ascii=False)}\n"
        f"next_step_feedback: {json.dumps(next_step_feedback, ensure_ascii=False)}"
    )


def _build_prompt_node_b(payload: Dict[str, Any], lang: str) -> str:
    schema = (
        '{"plan_outline":[{"phase":str,"deliverables":[str],"milestones":[str]}],'
        '"success_criteria":[str],"assumptions":[str]}'
    )
    goal = payload.get("goal", {})
    window = payload.get("window", {})

    if lang == "en":
        return (
            "Role: Node B — Goal Planner (coarse plan).\n"
            "Input constraint: use ONLY goal.title, goal.description_md, and window.\n"
            "\n"
            "HARD RULES:\n"
            "- Output JSON only.\n"
            "- Keep it coarse, avoid overload.\n"
            "- plan_outline: 2–4 phases max.\n"
            "- Each phase: 2–4 deliverables, 1–3 milestones.\n"
            "- Deliverables should be concrete outcomes (nouns), not vague verbs.\n"
            "\n"
            f"Schema: {schema}\n"
            f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
            f"window: {json.dumps(window, ensure_ascii=False)}"
        )
    return (
        "角色：Node B — 目标粗规划器。\n"
        "输入约束：只能使用 goal.title / goal.description_md / window。\n"
        "\n"
        "硬性规则：\n"
        "- 只输出 JSON。\n"
        "- 粗规划即可，不要过载。\n"
        "- plan_outline 最多 2–4 个阶段。\n"
        "- 每阶段 2–4 个 deliverables，1–3 个 milestones。\n"
        "- deliverables 写“可交付成果”（名词化），不要空泛动词。\n"
        "\n"
        f"Schema: {schema}\n"
        f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
        f"window: {json.dumps(window, ensure_ascii=False)}"
    )


def _prompt_addon_node_b(payload: Dict[str, Any], lang: str) -> str:
    if lang == "en":
        return (
            "ADDON (DO NOT CHANGE ANY EXISTING FIELDS):\n"
            "- Append two new fields ONLY: plan_detail_text_v1 and plan_summary_text_v1.\n"
            "- plan_detail_text_v1 must be the full planning content in plain text.\n"
            "- plan_summary_text_v1 must be 1–2 natural language sentences (no JSON/arrays/code).\n"
            "- Do NOT modify existing keys or their content.\n"
        )
    return (
        "ADDON（不得修改任何已有字段）：\n"
        "- 只追加 plan_detail_text_v1 与 plan_summary_text_v1。\n"
        "- plan_detail_text_v1 为完整规划内容（纯文本）。\n"
        "- plan_summary_text_v1 为 1–2 句自然语言摘要（不要 JSON/数组/代码块）。\n"
        "- 不得修改已有字段或其内容。\n"
    )
def _build_prompt_node_b_replan(payload, node_c, progress_signals, lang) -> str:
    schema = (
        '{"plan_outline":[{"phase":str,"deliverables":[str],"milestones":[str]}],'
        '"success_criteria":[str],"assumptions":[str]}'
    )
    goal = payload.get("goal", {})
    instructions = node_c.get("replan_instructions") or []
    reason = node_c.get("replan_reason") or ""

    if lang == "en":
        return (
            "Role: Node B_replan — Goal Planner (replan with hard constraints).\n"
            "You MUST follow the arbiter's instructions to prevent repeating the same planning mistakes.\n"
            "\n"
            "HARD RULES:\n"
            "- Output JSON only.\n"
            "- Follow instructions as HARD constraints.\n"
            "- Reduce scope; prefer fewer deliverables with higher certainty.\n"
            "- plan_outline: 1–3 phases max.\n"
            "\n"
            f"Schema: {schema}\n"
            f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
            f"progress: {json.dumps(progress_signals, ensure_ascii=False)}\n"
            f"replan_reason: {json.dumps(reason, ensure_ascii=False)}\n"
            f"hard_instructions: {json.dumps(instructions, ensure_ascii=False)}"
        )
    return (
        "角色：Node B_replan — 重规划器（带硬约束）。\n"
        "你必须遵守仲裁器的硬约束，避免重复之前不切实际的问题。\n"
        "\n"
        "硬性规则：\n"
        "- 只输出 JSON。\n"
        "- instructions 是硬约束，必须逐条落实。\n"
        "- 缩小范围：宁可少交付物，但更确定。\n"
        "- plan_outline 最多 1–3 个阶段。\n"
        "\n"
        f"Schema: {schema}\n"
        f"goal: {json.dumps(goal, ensure_ascii=False)}\n"
        f"progress: {json.dumps(progress_signals, ensure_ascii=False)}\n"
        f"replan_reason: {json.dumps(reason, ensure_ascii=False)}\n"
        f"硬约束 instructions: {json.dumps(instructions, ensure_ascii=False)}"
    )


def _prompt_addon_node_b_replan(payload: Dict[str, Any], lang: str) -> str:
    if lang == "en":
        return (
            "ADDON (DO NOT CHANGE ANY EXISTING FIELDS):\n"
            "- Append replan_detail_text_v1 and replan_summary_text_v1.\n"
            "- replan_detail_text_v1 must be the full replanned content in plain text.\n"
            "- replan_summary_text_v1 must be 1–2 natural language sentences (no JSON/arrays/code).\n"
            "- Do NOT modify existing keys or their content.\n"
        )
    return (
        "ADDON（不得修改任何已有字段）：\n"
        "- 追加 replan_detail_text_v1 与 replan_summary_text_v1。\n"
        "- replan_detail_text_v1 为完整重规划内容（纯文本）。\n"
        "- replan_summary_text_v1 为 1–2 句自然语言摘要（不要 JSON/数组/代码块）。\n"
        "- 不得修改已有字段或其内容。\n"
    )

def _build_prompt_node_c(node_a, node_b, progress_signals, lang) -> str:
    schema = (
        '{"verdict":"too_big|aligned|too_small|unclear","reason":str,"evidence_ids":[int],'
        '"replan_needed":bool,"replan_reason":str,"replan_instructions":[str],'
        '"replan_rationale_summary":str,"replan_evidence_ids":[int],'
        '"max_next_steps":int,"confidence":0.0,"alignment":0.0}'
    )
    if lang == "en":
        return (
            "Role: Node C — Arbiter (feasibility + anti-repeat replanning).\n"
            "Decide whether Node B plan is realistic given Node A evidence and progress signals.\n"
            "\n"
            "HARD RULES:\n"
            "- Output JSON only.\n"
            "- reason MUST reference evidence_ids from node_a.evidence.\n"
            "- If replan_needed=true, replan_instructions MUST be non-empty (2–4 items) and actionable.\n"
            "- If replan_needed=true, replan_rationale_summary MUST be a concise, user-facing why summary.\n"
            "- replan_instructions MUST explicitly prevent the same failure (e.g., reduce phases, limit deliverables per phase, focus on one core deliverable).\n"
            "\n"
            "HOW TO JUDGE:\n"
            "- If A shows low coverage or weak momentum OR progress shows low coverage_ratio: be conservative.\n"
            "- If B has too many deliverables/milestones for the window: verdict=too_big.\n"
            "- If B is vague/unmeasurable: treat as unclear.\n"
            "\n"
            "OUTPUT CALIBRATION:\n"
            "- confidence: 0.3–0.6 when evidence sparse; >0.6 only with strong evidence.\n"
            "- max_next_steps: 1 when conservative, else 2–3.\n"
            "- alignment: estimate whether A evidence matches B deliverables (0–1, rough).\n"
            "\n"
            f"Schema: {schema}\n"
            f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
            f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
            f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
        )
    return (
        "角色：Node C — 仲裁器（可行性判断 + 防复发重规划）。\n"
        "任务：结合 A 的证据总结与 progress_signals，判断 B 的计划是否不切实际；若需要重规划，必须给出可执行的“防复发”指令。\n"
        "\n"
        "硬性规则：\n"
        "- 只输出 JSON。\n"
        "- reason 必须引用 node_a.evidence 的 evidence_ids（不能空口判断）。\n"
        "- 若 replan_needed=true：replan_instructions 必须非空（2–4条），且必须具体可执行。\n"
        "- 若 replan_needed=true：replan_rationale_summary 必须给出一句面向用户的“为什么要重计划”的摘要。\n"
        "- replan_instructions 必须能避免同类问题再次出现（例如：减少阶段数、限制每阶段交付物数量、优先一个核心交付物、明确验收标准）。\n"
        "\n"
        "判断口径：\n"
        "- A 显示覆盖低 / momentum 弱 或 progress.coverage_ratio 低：要保守。\n"
        "- B 在窗口内交付物/里程碑明显过多：verdict=too_big。\n"
        "- B 描述空泛、不可验收：verdict=unclear。\n"
        "\n"
        "输出校准：\n"
        "- confidence：证据少时 0.3–0.6；只有证据强时才 >0.6。\n"
        "- max_next_steps：保守模式=1，否则 2–3。\n"
        "- alignment：估计 A 证据与 B 交付物匹配程度（0–1，粗略即可）。\n"
        "\n"
        f"Schema: {schema}\n"
        f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
        f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
        f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
    )


def _prompt_addon_node_c(lang: str) -> str:
    if lang == "en":
        return (
            "ADDON (DO NOT CHANGE ANY EXISTING FIELDS):\n"
            "- Always output replan_rationale_summary and replan_evidence_ids.\n"
            "- If replan_needed=false, replan_rationale_summary should briefly say why replan is not needed and replan_evidence_ids=[].\n"
            "- Do NOT modify existing keys or their content."
        )
    return (
        "ADDON（不得修改任何已有字段）：\n"
        "- 必须输出 replan_rationale_summary 与 replan_evidence_ids。\n"
        "- 若 replan_needed=false：replan_rationale_summary 用一句话说明无需重计划，replan_evidence_ids=[]。\n"
        "- 不得修改已有字段或其内容。"
    )


def _prompt_addon_node_d(lang: str) -> str:
    if lang == "en":
        return (
            "ADDON (DO NOT CHANGE ANY EXISTING FIELDS):\n"
            "- For next_steps items, include a short reason field (non-empty).\n"
            "- Reasons should reference replan summary or highlights when possible.\n"
            "- Do NOT modify existing keys or their content."
        )
    return (
        "ADDON（不得修改任何已有字段）：\n"
        "- next_steps 每条追加 reason（不为空）。\n"
        "- reason 尽量引用重规划摘要或 highlights。\n"
        "- 不得修改已有字段或其内容。"
    )

def _build_prompt_node_d(node_a, node_b, node_c, progress_signals, lang) -> str:
    schema = (
        '{"progress_summary":str,"highlights":[{"text":str,"evidence_ids":[int],"confidence":"High|Med|Low","needs_confirmation":bool}],'
        '"progress_gap":{"text":str,"evidence_ids":[int],"confidence":"High|Med|Low","needs_confirmation":bool},'
        '"remaining_work":[{"text":str,"priority":int,"evidence_ids":[int],"confidence":"High|Med|Low","needs_confirmation":bool}],'
        '"next_steps":[{"text":str,"reason":str,"evidence_ids":[int],"confidence":"High|Med|Low","needs_confirmation":bool}],'
        '"replan":{"needed":bool,"summary":str,"why":str,"confidence":"High|Med|Low","evidence_ids":[int]},'
        '"to_improve":[str],"assumptions":[str],"ask_back":str,"notice":str,'
        '"trust_summary":{"coverage_rate":0.0,"overall_confidence":"High|Med|Low","low_confidence_claims_count":int,'
        '"evidence_pool_size":int,"conflicts_detected":int,"broken_evidence_id_count":int},'
        '"metrics":{"coverage":0.0,"alignment":0.0,"confidence":0.0,'
        '"progress_pct":0.0,'
        '"window_start":str,"window_end":str,"generator_mode":"llm_progress_replan"}}'
    )

    if lang == "en":
        return (
            "Role: Node D — Final Card Synthesizer.\n"
            "Produce ONE final JSON card that is useful, actionable, and consistent with Node C decisions.\n"
            "\n"
            "HARD OUTPUT RULES:\n"
            "- JSON only. No markdown, no extra text.\n"
            "- Follow schema exactly. Do not add/remove keys.\n"
            "- Do NOT fabricate facts. If uncertain, use assumptions/notice.\n"
            "- Emojis are allowed in progress_summary/highlights/next_steps.\n"
            "\n"
            "STYLE (IMPORTANT):\n"
            "- Write progress_summary in a warm, encouraging, Xiaohongshu-like tone (uplifting, supportive, confidence-building).\n"
            "- When mentioning product/feature/function nouns, wrap them with corner quotes like 『...』.\n"
            "- Use emojis naturally (but avoid spam).\n"
            "\n"
            "GOAL EXPECTATION (MUST, SCHEMA UNCHANGED):\n"
            "- You MUST include a 'Goal Expectation' mini-section INSIDE progress_summary (do NOT add new JSON keys).\n"
            "- This mini-section must be 3–4 sentences, forward-looking, and grounded in current progress.\n"
            "- It MUST include an estimated progress percentage toward the goal (e.g., 'Estimated progress: 35%').\n"
            "- Estimate should be based on A evidence coverage + B milestones/deliverables + C feasibility verdict.\n"
            "\n"
            "DECISION INHERITANCE:\n"
            "- If node_c.replan_needed=true, treat node_b as replanned version (already re-run) and align outputs to it.\n"
            "- If node_c.replan_needed=false, do not propose major replans.\n"
            "\n"
            "PROGRESS_GAP (MOST IMPORTANT):\n"
            "- NOT time progress. Describe gap between: (A evidence of what is done) vs (B deliverables/milestones).\n"
            "- Mention what seems already done (grounded by A evidence) and what remains.\n"
            "\n"
            "REMAINING_WORK (MOST IMPORTANT):\n"
            "- Output 4-5 items, ordered by priority (1 = highest).\n"
            "- Items should be actionable and align with B deliverables/milestones.\n"
            "- If supported by evidence, include evidence_ids; else evidence_ids=[].\n"
            "\n"
            "HIGHLIGHTS:\n"
            "- 1–3 items; each must cite evidence_ids from A.\n"
            "\n"
            "NEXT_STEPS:\n"
            "- Concrete actions for the next 7 days.\n"
            "- At most node_c.max_next_steps items.\n"
            "- Each item MUST include a short reason (non-empty).\n"
            "- Reason must reference either replan rationale/constraints or highlights; avoid empty filler.\n"
            "\n"
            "REPLAN:\n"
            "- If node_c.replan_needed=true, output replan with summary/why/confidence/evidence_ids.\n"
            "- replan.summary should include intent + timebox + deliverables.\n"
            "- replan.why should be a one-line reason grounded in node_c.\n"
            "- Wrap feature/function nouns with 『』 when relevant.\n"
            "\n"
            "METRICS:\n"
            "- metrics.coverage/alignment/confidence should follow node_c.\n"
            "- window_start/end from progress window.\n"
            "\n"
            f"Schema: {schema}\n"
            f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
            f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
            f"node_c: {json.dumps(node_c, ensure_ascii=False)}\n"
            f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
        )

    return (
        "角色：Node D — 最终卡片总结器。\n"
        "输出一份最终 JSON 卡片：有用、可执行，并严格继承 Node C 的结论。\n"
        "\n"
        "硬性输出规则：\n"
        "- 只输出 JSON，不要 markdown，不要多余文字。\n"
        "- 严格遵循 schema，不增删字段。\n"
        "- 不得编造事实；不确定写 assumptions/notice。\n"
        "- 允许在 progress_summary/highlights/next_steps 中使用 emoji。\n"
        "\n"
        "文风与情绪价值（重要）：\n"
        "- progress_summary 必须写成“小红书风格”的鼓励型总结：温暖、有获得感、让人愿意继续做下去。\n"
        "- 提到功能/产品名词/模块名词时，用『』包起来（例如：『Weekly Reflection』、『Goal Analysis』等）。\n"
        "- emoji 可以用，但要克制、自然，不要刷屏。\n"
        "\n"
        "目标预期（必须，严格按照 schema）：\n"
        "- 你必须在 progress_summary 内部插入一个「🎯 目标预期」小段（不要新增 JSON 字段）。\n"
        "- 该小段必须 3–4 句话：描述最终可验收的状态 + 近期可达成的方向 + 风险/前置条件（可选）。\n"
        "- 你必须填写 metrics.progress_pct，并且结合当前进度，取值 0–100 的数字，表示“距离目标的进度估计百分比””\n"
        "- 百分比要基于：A 的证据覆盖/完成信号 + B 的交付/里程碑 + C 的可行性判断 来估算。\n"
        "\n"
        "决策继承：\n"
        "- 若 node_c.replan_needed=true：将 node_b 视为已重规划版本，对齐输出。\n"
        "- 若 node_c.replan_needed=false：不要提出大改计划。\n"
        "\n"
        "progress_gap（最重要）：\n"
        "- 不是时间进度。\n"
        "- 描述：A 的证据显示“已做了什么” vs B 的交付/里程碑“还缺什么”。\n"
        "- 点名最大的缺口（例如：缺少可验收交付物/缺少连续推进/缺少关键里程碑）。\n"
        "\n"
        "remaining_work（最重要）：\n"
        "- 只输出 4-5 条，按 priority 排序（1最高）。\n"
        "- 尽量具体可执行，并尽量对齐 B 的 deliverables/milestones。\n"
        "- 若有证据支撑可填 evidence_ids，否则 evidence_ids=[]。\n"
        "- 功能/模块名词尽量用『』包起来。\n"
        "\n"
        "highlights：\n"
        "- 1–3 条，必须引用 A 的 evidence_ids。\n"
        "\n"
        "next_steps：\n"
        "- 面向未来 7 天的具体动作。\n"
        "- 最多 node_c.max_next_steps 条。\n"
        "- 每条必须包含 reason（不为空）。\n"
        "- reason 必须引用 replan 的依赖/约束/缺口，或引用 highlights（至少一种）。\n"
        "\n"
        "REPLAN：\n"
        "- 若 node_c.replan_needed=true，必须输出 replan（summary/why/confidence/evidence_ids）。\n"
        "- replan.summary 需包含意图 + 时间盒 + 交付物。\n"
        "- replan.why 为一句话，需与 node_c 决策一致。\n"
        "- 功能/模块名词尽量用『』包起来。\n"
        "\n"
        "metrics：\n"
        "- coverage/alignment/confidence 尽量沿用 node_c。\n"
        "- window_start/end 使用 progress 窗口。\n"
        "\n"
        f"Schema: {schema}\n"
        f"node_a: {json.dumps(node_a, ensure_ascii=False)}\n"
        f"node_b: {json.dumps(node_b, ensure_ascii=False)}\n"
        f"node_c: {json.dumps(node_c, ensure_ascii=False)}\n"
        f"progress: {json.dumps(progress_signals, ensure_ascii=False)}"
    )


def _validate_node_a(data: Dict[str, Any]) -> bool:
    return bool(
        isinstance(data.get("evidence"), list)
        and isinstance(data.get("habit_summary"), dict)
        and isinstance(data.get("coverage"), dict)
    )


def _validate_node_b(data: Dict[str, Any]) -> bool:
    return bool(
        isinstance(data.get("plan_outline"), list)
        and isinstance(data.get("success_criteria"), list)
        and isinstance(data.get("assumptions"), list)
    )


def _validate_node_c(data: Dict[str, Any]) -> bool:
    return bool(
        isinstance(data.get("verdict"), str)
        and isinstance(data.get("reason"), str)
        and isinstance(data.get("evidence_ids"), list)
        and isinstance(data.get("confidence"), (int, float))
        and isinstance(data.get("alignment"), (int, float))
        and isinstance(data.get("replan_needed"), bool)
        and isinstance(data.get("replan_reason"), str)
        and isinstance(data.get("replan_instructions"), list)
        and isinstance(data.get("replan_rationale_summary"), str)
        and isinstance(data.get("replan_evidence_ids"), list)
        and isinstance(data.get("max_next_steps"), int)
    )


def _validate_node_d(data: Dict[str, Any]) -> bool:
    progress_gap = data.get("progress_gap")
    progress_gap_ok = isinstance(progress_gap, str) or (
        isinstance(progress_gap, dict) and isinstance(progress_gap.get("text"), str)
    )
    trust_summary = data.get("trust_summary")
    trust_summary_ok = trust_summary is None or isinstance(trust_summary, dict)
    replan = data.get("replan")
    replan_ok = replan is None or isinstance(replan, dict)
    return bool(
        isinstance(data.get("progress_summary"), str)
        and isinstance(data.get("highlights"), list)
        and progress_gap_ok
        and isinstance(data.get("remaining_work"), list)
        and isinstance(data.get("next_steps"), list)
        and isinstance(data.get("to_improve"), list)
        and isinstance(data.get("assumptions"), list)
        and isinstance(data.get("metrics"), dict)
        and trust_summary_ok
        and replan_ok
    )


def _progress_signals(payload: Dict[str, Any], node_a: Dict[str, Any]) -> Dict[str, Any]:
    goal = payload.get("goal", {})
    window = payload.get("window", {})
    start_raw = goal.get("start_date") or window.get("start")
    end_raw = goal.get("end_date")
    window_end_raw = window.get("end")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today()
    window_end_date = date.fromisoformat(window_end_raw) if window_end_raw else date.today()
    elapsed_days = max((window_end_date - start_date).days, 0)
    window_days = int(window.get("days") or max(elapsed_days, 1))
    total_days = (
        max((date.fromisoformat(end_raw) - start_date).days, 0) if end_raw else max(elapsed_days, window_days)
    )
    total_days = max(total_days, 1)
    time_progress = min(max(elapsed_days / total_days, 0.0), 1.0)
    coverage_days = int(((node_a.get("coverage") or {}).get("coverage_days")) or 0)
    coverage_ratio = coverage_days / max(elapsed_days, 1)
    remaining_days = max(total_days - elapsed_days, 0)
    return {
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "time_progress": time_progress,
        "coverage_ratio": coverage_ratio,
        "remaining_days": remaining_days,
        "window_days": window_days,
    }


def _fallback_node_a(payload: Dict[str, Any]) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
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
        evidence.append(
            {
                "id": len(evidence) + 1,
                "date": log.get("date"),
                "source_type": "log",
                "source_id": 0,
                "quote": quote,
                "url": _evidence_link("log", log.get("date")),
                "tags": log.get("tags") or [],
            }
        )
    window = payload.get("window", {})
    return {
        "evidence": evidence[:10],
        "habit_summary": {
            "top_patterns": [],
            "blockers": [],
            "triggers": [],
            "momentum": "unknown",
        },
        "coverage": {
            "coverage_days": _coverage_days(payload),
            "window": {
                "start": window.get("start"),
                "end": window.get("end"),
                "days": int(window.get("days") or 0),
            },
        },
    }


def _fallback_node_b(payload: Dict[str, Any], lang: str) -> Dict[str, Any]:
    if lang == "en":
        return {
            "plan_outline": [
                {
                    "phase": "Phase 1",
                    "deliverables": ["A small, shippable outcome"],
                    "milestones": ["First usable checkpoint"],
                }
            ],
            "success_criteria": ["One concrete deliverable completed"],
            "assumptions": ["Timeline is flexible"],
        }
    return {
        "plan_outline": [
            {
                "phase": "Phase 1",
                "deliverables": ["一个可交付的小成果"],
                "milestones": ["首个可用里程碑"],
            }
        ],
        "success_criteria": ["完成一个具体交付物"],
        "assumptions": ["时间线可调整"],
    }


def _fallback_node_c(node_a: Dict[str, Any], node_b: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "verdict": "unclear",
        "reason": "Evidence is limited; keep scope small.",
        "evidence_ids": [],
        "replan_needed": False,
        "replan_reason": "Evidence is limited; keep scope small.",
        "replan_instructions": [],
        "replan_rationale_summary": "",
        "replan_evidence_ids": [],
        "max_next_steps": 1,
        "confidence": 0.4,
        "alignment": 0.3,
    }


def _fallback_node_d(
    payload: Dict[str, Any],
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
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
        next_steps = [
            {
                "text": "Pick one smallest task you can finish today.",
                "reason": "Keep the scope tight and create a quick win.",
                "evidence_ids": [],
            }
        ]
    else:
        summary = "近期行动显示已有小幅推进。"
        next_steps = [
            {
                "text": "选一个今天就能完成的小任务。",
                "reason": "先做最小可完成任务，建立推进节奏。",
                "evidence_ids": [],
            }
        ]
    max_steps = int(node_c.get("max_next_steps") or 1)
    next_steps = next_steps[:max_steps]
    highlights = []
    evidence = node_a.get("evidence") or []
    for ev in evidence[:2]:
        highlights.append({"text": ev.get("quote", ""), "evidence_ids": [ev.get("id")]})
    remaining_work = []
    plan_outline = node_b.get("plan_outline") or []
    priority = 1
    for phase in plan_outline:
        for deliverable in phase.get("deliverables", []):
            remaining_work.append(
                {"text": deliverable, "priority": priority, "evidence_ids": []}
            )
            priority += 1
    while len(remaining_work) < 5:
        remaining_work.append(
            {
                "text": f"Clarify deliverable #{len(remaining_work)+1}",
                "priority": len(remaining_work) + 1,
                "evidence_ids": [],
            }
        )
    return {
        "progress_summary": summary,
        "highlights": highlights,
        "progress_gap": {
            "text": "Evidence shows partial progress; most deliverables remain.",
            "evidence_ids": [],
            "confidence": "Low",
            "needs_confirmation": True,
        },
        "remaining_work": remaining_work[:5],
        "next_steps": next_steps,
        "to_improve": [],
        "risks": [],
        "assumptions": [],
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


def _normalize_confidence(value: Any, has_evidence: bool) -> str:
    if isinstance(value, (int, float)):
        score = float(value)
        if score >= 0.66:
            return "High"
        if score >= 0.33:
            return "Med"
        return "Low"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"high", "h"}:
            return "High"
        if lowered in {"med", "medium", "m"}:
            return "Med"
        if lowered in {"low", "l"}:
            return "Low"
    return "Med" if has_evidence else "Low"


def _normalize_claim_item(
    item: Any, evidence_pool_ids: set[int]
) -> tuple[Dict[str, Any], int, bool]:
    if isinstance(item, dict):
        text = str(item.get("text") or "")
        reason = str(item.get("reason") or "")
        evidence_ids_raw = item.get("evidence_ids")
        if isinstance(evidence_ids_raw, list):
            evidence_ids = []
            for val in evidence_ids_raw:
                if isinstance(val, int):
                    evidence_ids.append(val)
                elif isinstance(val, str) and val.isdigit():
                    evidence_ids.append(int(val))
        else:
            evidence_ids = []
        valid_ids = [eid for eid in evidence_ids if eid in evidence_pool_ids]
        broken_count = len(evidence_ids) - len(valid_ids)
        has_evidence = bool(valid_ids)
        confidence = _normalize_confidence(item.get("confidence"), has_evidence)
        needs_confirmation = bool(item.get("needs_confirmation")) or not has_evidence
        if broken_count > 0:
            confidence = "Low"
            needs_confirmation = True
        normalized = dict(item)
        normalized["text"] = text
        if "reason" in item:
            normalized["reason"] = reason
        normalized["evidence_ids"] = valid_ids
        normalized["confidence"] = confidence
        normalized["needs_confirmation"] = needs_confirmation
        return normalized, broken_count, has_evidence
    text = str(item or "")
    normalized = {
        "text": text,
        "evidence_ids": [],
        "confidence": "Low",
        "needs_confirmation": True,
    }
    return normalized, 0, False


def _normalize_claim_list(
    items: Iterable[Any], evidence_pool_ids: set[int]
) -> tuple[List[Dict[str, Any]], int, int, int, int]:
    normalized: List[Dict[str, Any]] = []
    broken_total = 0
    total_claims = 0
    claims_with_evidence = 0
    claims_needing_confirmation = 0
    for item in items:
        normalized_item, broken_count, has_evidence = _normalize_claim_item(
            item, evidence_pool_ids
        )
        normalized.append(normalized_item)
        broken_total += broken_count
        total_claims += 1
        if has_evidence:
            claims_with_evidence += 1
        if normalized_item.get("needs_confirmation"):
            claims_needing_confirmation += 1
    return (
        normalized,
        broken_total,
        total_claims,
        claims_with_evidence,
        claims_needing_confirmation,
    )


def _is_json_like(text: str) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] in {"{", "["}:
        return True
    try:
        json.loads(stripped)
    except Exception:  # noqa: BLE001
        return False
    return True


def _first_sentences(text: str, max_sentences: int = 2) -> str:
    cleaned = _normalize_text(text)
    if not cleaned:
        return ""
    parts = re.split(r"([。！？.!?])", cleaned)
    sentences: List[str] = []
    current = ""
    for part in parts:
        if part in {"。", "！", "？", ".", "!", "?"}:
            current += part
            sentences.append(current.strip())
            current = ""
        else:
            current += part
        if len(sentences) >= max_sentences:
            break
    if not sentences and current.strip():
        sentences.append(current.strip())
    return " ".join(sentences[:max_sentences])


def _summary_from_outline(outline: Any, lang: str) -> str:
    if not isinstance(outline, list) or not outline:
        return ""
    deliverables: List[str] = []
    for phase in outline:
        if isinstance(phase, dict):
            for item in phase.get("deliverables", []) or []:
                if item:
                    deliverables.append(str(item))
    deliverables = deliverables[:2]
    phase_count = len(outline)
    if lang == "en":
        if deliverables:
            return (
                f"Plan has {phase_count} phases and focuses on {', '.join(deliverables)}."
            )
        return f"Plan has {phase_count} phases with concrete deliverables."
    if deliverables:
        return f"计划包含 {phase_count} 个阶段，聚焦交付：{', '.join(deliverables)}。"
    return f"计划包含 {phase_count} 个阶段，包含可交付成果。"


def _coerce_summary_text(
    summary_text: str, detail_text: str, outline: Any, lang: str
) -> str:
    if summary_text and not _is_json_like(summary_text):
        return summary_text.strip()
    if detail_text and not _is_json_like(detail_text):
        return _first_sentences(detail_text, 2)
    summary = _summary_from_outline(outline, lang)
    if summary:
        return summary
    if detail_text:
        return _first_sentences(detail_text, 2)
    return ""


def _coerce_detail_text(detail_text: str, outline: Any) -> str:
    if detail_text and not _is_json_like(detail_text):
        return detail_text.strip()
    if isinstance(outline, list) and outline:
        return json.dumps(outline, ensure_ascii=False, indent=2)
    return ""


def _build_plan_view_v1(
    node_b_original: Dict[str, Any],
    node_b_replan: Optional[Dict[str, Any]],
    node_c: Dict[str, Any],
    lang: str,
) -> Dict[str, Any]:
    origin_outline = node_b_original.get("plan_outline") if isinstance(node_b_original, dict) else []
    origin_detail = (
        node_b_original.get("plan_detail_text_v1") if isinstance(node_b_original, dict) else ""
    )
    origin_summary = (
        node_b_original.get("plan_summary_text_v1") if isinstance(node_b_original, dict) else ""
    )
    origin_detail = _coerce_detail_text(str(origin_detail or ""), origin_outline)
    origin_summary = _coerce_summary_text(str(origin_summary or ""), origin_detail, origin_outline, lang)

    replan_outline = node_b_replan.get("plan_outline") if isinstance(node_b_replan, dict) else []
    replan_detail = (
        node_b_replan.get("replan_detail_text_v1") if isinstance(node_b_replan, dict) else ""
    )
    replan_summary = (
        node_b_replan.get("replan_summary_text_v1") if isinstance(node_b_replan, dict) else ""
    )
    replan_detail = _coerce_detail_text(str(replan_detail or ""), replan_outline)
    replan_summary = _coerce_summary_text(str(replan_summary or ""), replan_detail, replan_outline, lang)

    replan_available = bool(node_c.get("replan_needed") and (replan_detail or replan_summary or replan_outline))
    origin_available = bool(origin_detail or origin_summary or origin_outline)
    current_mode = "replan" if replan_available else "origin"

    return {
        "current_mode": current_mode,
        "origin": {
            "available": origin_available,
            "summary": origin_summary,
            "detail": origin_detail,
        },
        "replan": {
            "available": replan_available,
            "summary": replan_summary,
            "detail": replan_detail,
        },
    }


def _normalize_timebox_v1(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if re.match(r"^\d+(\.\d+)?[hd]$", value):
        return value
    return ""


def _normalize_plan_item_v1(
    item: Any, evidence_pool_ids: set[int]
) -> tuple[Dict[str, Any], int]:
    if not isinstance(item, dict):
        return {
            "id": 0,
            "title": str(item or ""),
            "deliverable": "",
            "timebox": "",
            "priority": "P1",
            "depends_on": [],
            "evidence_ids": [],
            "confidence": "Low",
            "needs_confirmation": True,
        }, 0
    title = str(item.get("title") or "").strip()
    deliverable = str(item.get("deliverable") or "").strip()
    timebox = _normalize_timebox_v1(item.get("timebox"))
    priority = str(item.get("priority") or "P1").strip()
    depends_on = item.get("depends_on")
    if not isinstance(depends_on, list):
        depends_on = []
    evidence_ids_raw = item.get("evidence_ids")
    evidence_ids = []
    if isinstance(evidence_ids_raw, list):
        for val in evidence_ids_raw:
            if isinstance(val, int):
                evidence_ids.append(val)
            elif isinstance(val, str) and val.isdigit():
                evidence_ids.append(int(val))
    valid_ids = [eid for eid in evidence_ids if eid in evidence_pool_ids]
    broken_count = len(evidence_ids) - len(valid_ids)
    has_evidence = bool(valid_ids)
    confidence = _normalize_confidence(item.get("confidence"), has_evidence)
    needs_confirmation = bool(item.get("needs_confirmation")) or not has_evidence
    if not title or not deliverable or not timebox:
        confidence = "Low"
        needs_confirmation = True
    if broken_count > 0:
        confidence = "Low"
        needs_confirmation = True
    return (
        {
            "id": item.get("id") or 0,
            "title": title,
            "deliverable": deliverable,
            "timebox": timebox,
            "priority": "P0" if priority == "P0" else "P1",
            "depends_on": depends_on,
            "evidence_ids": valid_ids,
            "confidence": confidence,
            "needs_confirmation": needs_confirmation,
        },
        broken_count,
    )


def _normalize_plan_items_v1(
    items: Any, evidence_pool_ids: set[int]
) -> tuple[List[Dict[str, Any]], int]:
    if not isinstance(items, list):
        return [], 0
    normalized: List[Dict[str, Any]] = []
    broken_total = 0
    for item in items:
        normalized_item, broken = _normalize_plan_item_v1(item, evidence_pool_ids)
        normalized.append(normalized_item)
        broken_total += broken
    return normalized, broken_total


def _build_analysis_plan_v1(
    node_a: Dict[str, Any],
    node_b_original: Dict[str, Any],
    node_b_replan: Optional[Dict[str, Any]],
    node_c: Dict[str, Any],
    lang: str,
) -> tuple[Dict[str, Any], int]:
    evidence_pool = node_a.get("evidence") or []
    evidence_pool_ids: set[int] = set()
    for item in evidence_pool:
        raw_id = item.get("id")
        if isinstance(raw_id, int):
            evidence_pool_ids.add(raw_id)
        elif isinstance(raw_id, str) and raw_id.isdigit():
            evidence_pool_ids.add(int(raw_id))

    original_items_raw = (
        node_b_original.get("plan_items_v1") if isinstance(node_b_original, dict) else []
    )
    original_items, broken_original = _normalize_plan_items_v1(
        original_items_raw, evidence_pool_ids
    )
    original_summary = (
        str(node_b_original.get("plan_summary_v1") or "").strip()
        if isinstance(node_b_original, dict)
        else ""
    )
    original_available = bool(original_items)

    replan_items_raw = (
        node_b_replan.get("replan_plan_items_v1")
        if isinstance(node_b_replan, dict)
        else []
    )
    replan_items, broken_replan = _normalize_plan_items_v1(
        replan_items_raw, evidence_pool_ids
    )
    replan_summary = (
        str(node_b_replan.get("replan_plan_summary_v1") or "").strip()
        if isinstance(node_b_replan, dict)
        else ""
    )
    replan_available = bool(node_c.get("replan_needed") and replan_items)

    diff_summary = (
        node_b_replan.get("diff_summary_v1") if isinstance(node_b_replan, dict) else None
    )
    if not isinstance(diff_summary, dict):
        original_titles = {item.get("title") for item in original_items if item.get("title")}
        replan_titles = {item.get("title") for item in replan_items if item.get("title")}
        diff_summary = {
            "added_count": max(len(replan_titles - original_titles), 0),
            "removed_count": max(len(original_titles - replan_titles), 0),
            "changed_count": 0,
        }

    plan = {
        "replan": {
            "available": replan_available,
            "items": replan_items,
            "summary": replan_summary,
        },
        "original": {
            "available": original_available,
            "items": original_items,
            "summary": original_summary,
        },
        "diff_summary": diff_summary,
    }
    broken_total = broken_original + broken_replan
    return plan, broken_total


def _derive_next_steps_v1(
    plan_items: List[Dict[str, Any]],
    highlights: List[Dict[str, Any]],
    max_steps: int,
    plan_summary: str,
    lang: str,
) -> List[Dict[str, Any]]:
    highlight_texts = [
        str(item.get("text") or "").lower() for item in highlights if isinstance(item, dict)
    ]
    def _already_done(item: Dict[str, Any]) -> bool:
        title = (item.get("title") or "").lower()
        deliverable = (item.get("deliverable") or "").lower()
        for text in highlight_texts:
            if title and title in text:
                return True
            if deliverable and deliverable in text:
                return True
        return False

    candidates = [item for item in plan_items if not item.get("depends_on")]
    if not candidates:
        candidates = plan_items[:]
    ordered = sorted(
        candidates,
        key=lambda item: (0 if item.get("priority") == "P0" else 1),
    )
    steps: List[Dict[str, Any]] = []
    for item in ordered:
        if _already_done(item):
            continue
        reason_base = plan_summary or (
            "Based on replanned deliverables." if lang == "en" else "基于重规划交付物。"
        )
        reason = (
            f"{reason_base} Focus on {item.get('deliverable')}."
            if lang == "en"
            else f"{reason_base} 优先交付「{item.get('deliverable')}」。"
        )
        steps.append(
            {
                "text": item.get("title") or "",
                "reason": reason,
                "evidence_ids": item.get("evidence_ids") or [],
                "confidence": item.get("confidence") or "Low",
                "needs_confirmation": item.get("needs_confirmation", True),
            }
        )
        if len(steps) >= max_steps:
            break
    return steps[:max_steps]


def _build_replan_summary(
    node_b: Dict[str, Any], progress_signals: Dict[str, Any], lang: str
) -> str:
    plan_outline = node_b.get("plan_outline") or []
    deliverables: List[str] = []
    for phase in plan_outline:
        for deliverable in phase.get("deliverables", []) or []:
            if deliverable:
                deliverables.append(str(deliverable))
    deliverables = deliverables[:3]
    window_days = int(progress_signals.get("window_days") or 7)
    if lang == "en":
        if deliverables:
            return (
                f"Focus the next {window_days} days on {len(deliverables)} deliverables: "
                + ", ".join(deliverables)
                + "."
            )
        return f"Focus the next {window_days} days on one core, shippable outcome."
    if deliverables:
        return f"未来 {window_days} 天聚焦 {len(deliverables)} 个交付物：{', '.join(deliverables)}。"
    return f"未来 {window_days} 天聚焦一个核心可交付成果。"


def _normalize_replan(
    node_d: Dict[str, Any],
    node_b: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    evidence_pool_ids: set[int],
    lang: str,
) -> tuple[Dict[str, Any], int]:
    replan = node_d.get("replan")
    if not isinstance(replan, dict):
        replan = {}
    needed = bool(replan.get("needed") or node_c.get("replan_needed"))
    summary = str(replan.get("summary") or "").strip()
    why = str(replan.get("why") or "").strip()
    confidence = _normalize_confidence(replan.get("confidence"), False)
    evidence_ids_raw = replan.get("evidence_ids")
    if isinstance(evidence_ids_raw, list):
        evidence_ids = []
        for val in evidence_ids_raw:
            if isinstance(val, int):
                evidence_ids.append(val)
            elif isinstance(val, str) and val.isdigit():
                evidence_ids.append(int(val))
    else:
        evidence_ids = []
    if not evidence_ids and isinstance(node_c.get("replan_evidence_ids"), list):
        for val in node_c.get("replan_evidence_ids"):
            if isinstance(val, int):
                evidence_ids.append(val)
            elif isinstance(val, str) and val.isdigit():
                evidence_ids.append(int(val))
    valid_ids = [eid for eid in evidence_ids if eid in evidence_pool_ids]
    broken_count = len(evidence_ids) - len(valid_ids)
    if broken_count > 0:
        confidence = "Low"
    if needed and not summary:
        summary = _build_replan_summary(node_b, progress_signals, lang)
    if needed and not why:
        why = (
            str(node_c.get("replan_rationale_summary") or "").strip()
            or str(node_c.get("replan_reason") or "").strip()
            or str(node_c.get("reason") or "").strip()
        )
    if not confidence:
        confidence = "Med" if valid_ids else "Low"
    node_d["replan"] = {
        "needed": needed,
        "summary": summary,
        "why": why,
        "confidence": confidence,
        "evidence_ids": valid_ids,
    }
    return node_d["replan"], broken_count


def _ensure_next_step_reasons(
    node_d: Dict[str, Any], node_c: Dict[str, Any], lang: str
) -> None:
    reason = (
        str(node_c.get("replan_rationale_summary") or "").strip()
        or str(node_c.get("replan_reason") or "").strip()
        or str(node_c.get("reason") or "").strip()
    )
    if not reason:
        reason = (
            "Based on recent highlights, take the next concrete step."
            if lang == "en"
            else "基于近期亮点，推进下一步具体动作。"
        )
    next_steps = node_d.get("next_steps") or []
    for item in next_steps:
        if isinstance(item, dict):
            item_reason = str(item.get("reason") or "").strip()
            if not item_reason:
                item["reason"] = reason


def _apply_trust_summary(
    node_d: Dict[str, Any],
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    lang: str,
) -> Dict[str, Any]:
    evidence_pool = node_a.get("evidence") or []
    evidence_pool_ids: set[int] = set()
    for item in evidence_pool:
        raw_id = item.get("id")
        if isinstance(raw_id, int):
            evidence_pool_ids.add(raw_id)
        elif isinstance(raw_id, str) and raw_id.isdigit():
            evidence_pool_ids.add(int(raw_id))
    highlights, broken_h, total_h, covered_h, needs_h = _normalize_claim_list(
        node_d.get("highlights") or [], evidence_pool_ids
    )
    next_steps, broken_n, total_n, covered_n, needs_n = _normalize_claim_list(
        node_d.get("next_steps") or [], evidence_pool_ids
    )
    remaining_work, broken_r, total_r, covered_r, needs_r = _normalize_claim_list(
        node_d.get("remaining_work") or [], evidence_pool_ids
    )
    progress_gap_raw = node_d.get("progress_gap")
    progress_gap_claim = None
    broken_g = 0
    total_g = 0
    covered_g = 0
    needs_g = 0
    if progress_gap_raw is not None:
        if isinstance(progress_gap_raw, dict) or isinstance(progress_gap_raw, str):
            progress_gap_claim, broken_g, total_g, covered_g, needs_g = _normalize_claim_list(
                [progress_gap_raw], evidence_pool_ids
            )
            progress_gap_claim = progress_gap_claim[0]
    node_d["highlights"] = highlights
    node_d["next_steps"] = next_steps
    node_d["remaining_work"] = remaining_work
    if progress_gap_claim:
        node_d["progress_gap"] = progress_gap_claim

    total_claims = total_h + total_n + total_r + total_g
    covered_claims = covered_h + covered_n + covered_r + covered_g
    coverage_rate = covered_claims / total_claims if total_claims else 0.0
    low_confidence_claims_count = needs_h + needs_n + needs_r
    if progress_gap_claim:
        if progress_gap_claim.get("needs_confirmation"):
            low_confidence_claims_count += 1

    replan, broken_replan = _normalize_replan(
        node_d, node_b, node_c, progress_signals, evidence_pool_ids, lang
    )
    broken_evidence_id_count = broken_h + broken_n + broken_r + broken_g + broken_replan
    if coverage_rate >= 0.7 and low_confidence_claims_count == 0:
        overall_confidence = "High"
    elif coverage_rate >= 0.4 and low_confidence_claims_count <= max(1, total_claims // 3):
        overall_confidence = "Med"
    else:
        overall_confidence = "Low"

    node_d["trust_summary"] = {
        "coverage_rate": coverage_rate,
        "overall_confidence": overall_confidence,
        "low_confidence_claims_count": low_confidence_claims_count,
        "evidence_pool_size": len(evidence_pool),
        "conflicts_detected": 0,
        "broken_evidence_id_count": broken_evidence_id_count,
    }
    return node_d


def _apply_gate(node_c: Dict[str, Any], node_a: Dict[str, Any]) -> None:
    coverage_days = int(((node_a.get("coverage") or {}).get("coverage_days")) or 0)
    confidence = float(node_c.get("confidence") or 0.0)
    alignment = float(node_c.get("alignment") or 0.0)
    verdict = (node_c.get("verdict") or "").strip()
    if coverage_days < 2 or confidence < 0.5:
        node_c["replan_needed"] = False
        node_c["max_next_steps"] = 1
        node_c["replan_instructions"] = []
        return
    if alignment < 0.4 or verdict == "too_big":
        node_c["replan_needed"] = True
        node_c["max_next_steps"] = max(1, min(int(node_c.get("max_next_steps") or 2), 2))
        instructions = node_c.get("replan_instructions") or []
        if not instructions:
            instructions = ["缩小范围，优先保留一个核心交付物。"]
        node_c["replan_instructions"] = instructions
    if node_c.get("replan_needed") and not (node_c.get("replan_reason") or "").strip():
        node_c["replan_reason"] = node_c.get("reason") or "Evidence suggests scope mismatch."


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
    prompt = f"{prompt}\n\n{_prompt_addon_node_b(payload, lang)}"
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_b(parsed):
        return parsed, {"mode": "llm"}
    return _fallback_node_b(payload, lang), {"mode": "fallback", "error": error}


def _node_b_replan(
    payload: Dict[str, Any],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    model_key: str,
    api_key: str,
    lang: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_b_replan(payload, node_c, progress_signals, lang)
    prompt = f"{prompt}\n\n{_prompt_addon_node_b_replan(payload, lang)}"
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_b(parsed):
        return parsed, {"mode": "llm_replan"}
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
    prompt = f"{prompt}\n\n{_prompt_addon_node_c(lang)}"
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_c(parsed):
        return parsed, {"mode": "llm"}
    return _fallback_node_c(node_a, node_b), {"mode": "fallback", "error": error}


def _node_d(
    node_a: Dict[str, Any],
    node_b: Dict[str, Any],
    node_b_original: Dict[str, Any],
    node_b_replan: Optional[Dict[str, Any]],
    node_c: Dict[str, Any],
    progress_signals: Dict[str, Any],
    model_key: str,
    api_key: str,
    lang: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = _build_prompt_node_d(node_a, node_b, node_c, progress_signals, lang)
    prompt = f"{prompt}\n\n{_prompt_addon_node_d(lang)}"
    parsed, error = _llm_json(model_key, api_key, prompt, lang)
    if parsed and _validate_node_d(parsed):
        greeting = "Good morning, " if lang == "en" else "\u65e9\u4e0a\u597d\uff0c"
        summary = parsed.get("progress_summary") or ""
        if not summary.startswith(greeting):
            parsed["progress_summary"] = f"{greeting}{summary}".strip()
        max_steps = min(5, max(3, int(node_c.get("max_next_steps") or 3)))
        raw_steps = parsed.get("next_steps") or []
        normalized_steps = []
        for item in raw_steps:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    evidence_ids = item.get("evidence_ids")
                    if not isinstance(evidence_ids, list):
                        evidence_ids = []
                    reason = item.get("reason") or ""
                    normalized_steps.append(
                        {
                            "text": str(text),
                            "reason": str(reason),
                            "evidence_ids": evidence_ids,
                            "confidence": item.get("confidence"),
                            "needs_confirmation": item.get("needs_confirmation"),
                        }
                    )
            elif item is not None:
                normalized_steps.append(
                    {
                        "text": str(item),
                        "reason": "",
                        "evidence_ids": [],
                        "confidence": "Low",
                        "needs_confirmation": True,
                    }
                )
        parsed["next_steps"] = normalized_steps[:max_steps]
        _ensure_next_step_reasons(parsed, node_c, lang)
        remaining_work = parsed.get("remaining_work") or []
        while len(remaining_work) < 5:
            remaining_work.append(
                {
                    "text": f"Clarify deliverable #{len(remaining_work)+1}",
                    "priority": len(remaining_work) + 1,
                    "evidence_ids": [],
                }
            )
        parsed["remaining_work"] = remaining_work[:5]
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
        parsed = _apply_trust_summary(parsed, node_a, node_b, node_c, progress_signals, lang)
        plan_v1, broken_plan = _build_analysis_plan_v1(
            node_a, node_b_original, node_b_replan, node_c, lang
        )
        parsed["plan"] = plan_v1
        max_steps = min(5, max(3, int(node_c.get("max_next_steps") or 3)))
        plan_items = (
            plan_v1.get("replan", {}).get("items")
            if plan_v1.get("replan", {}).get("available")
            else plan_v1.get("original", {}).get("items")
        )
        plan_summary = (
            plan_v1.get("replan", {}).get("summary")
            if plan_v1.get("replan", {}).get("available")
            else plan_v1.get("original", {}).get("summary")
        )
        if plan_items:
            parsed["next_steps"] = _derive_next_steps_v1(
                plan_items, parsed.get("highlights") or [], max_steps, plan_summary, lang
            )
            _ensure_next_step_reasons(parsed, node_c, lang)
        parsed["plan_view"] = _build_plan_view_v1(
            node_b_original, node_b_replan, node_c, lang
        )
        trust = parsed.get("trust_summary") or {}
        if isinstance(trust, dict):
            trust["broken_evidence_id_count"] = int(
                trust.get("broken_evidence_id_count") or 0
            ) + broken_plan
            parsed["trust_summary"] = trust
        return parsed, {"mode": "llm"}
    fallback = _fallback_node_d(
        {"window": (node_a.get("coverage") or {}).get("window", {})},
        node_a,
        node_b,
        node_c,
        progress_signals,
        lang,
    )
    fallback = _apply_trust_summary(fallback, node_a, node_b, node_c, progress_signals, lang)
    plan_v1, broken_plan = _build_analysis_plan_v1(
        node_a, node_b_original, node_b_replan, node_c, lang
    )
    fallback["plan"] = plan_v1
    max_steps = min(5, max(3, int(node_c.get("max_next_steps") or 3)))
    plan_items = (
        plan_v1.get("replan", {}).get("items")
        if plan_v1.get("replan", {}).get("available")
        else plan_v1.get("original", {}).get("items")
    )
    plan_summary = (
        plan_v1.get("replan", {}).get("summary")
        if plan_v1.get("replan", {}).get("available")
        else plan_v1.get("original", {}).get("summary")
    )
    if plan_items:
        fallback["next_steps"] = _derive_next_steps_v1(
            plan_items, fallback.get("highlights") or [], max_steps, plan_summary, lang
        )
        _ensure_next_step_reasons(fallback, node_c, lang)
    fallback["plan_view"] = _build_plan_view_v1(
        node_b_original, node_b_replan, node_c, lang
    )
    trust = fallback.get("trust_summary") or {}
    if isinstance(trust, dict):
        trust["broken_evidence_id_count"] = int(
            trust.get("broken_evidence_id_count") or 0
        ) + broken_plan
        fallback["trust_summary"] = trust
    return fallback, {"mode": "fallback", "error": error}
