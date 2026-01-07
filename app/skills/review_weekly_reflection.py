from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.data.repo import get_settings
from app.domain.models import DayLog, DailyPlan, HabitTemplate, PlanItem, Suggestion

logger = logging.getLogger(__name__)
LLM_PROVIDER = "ModelScope"
LLM_BASE_URL = "https://api-inference.modelscope.cn/v1"


@dataclass
class LlmCallError(Exception):
    details: Dict[str, Any]


class WeeklyReflectionInput(BaseModel):
    as_of: date
    window_days: int = 7
    lang: str = "zh"
    existing_id: Optional[int] = None
    trigger: Optional[str] = None
    mode: Optional[str] = None


class WeeklyReflectionOutput(BaseModel):
    opener: str
    highlights: List[str]
    gaps: Dict[str, Any]
    next_steps: List[str]
    metrics: Dict[str, Any]


class ReviewWeeklyReflectionSkill(Skill):
    name = "review.weekly_reflection"
    description = "Generate a weekly reflection card from recent DayLog entries."
    input_schema = WeeklyReflectionInput
    output_schema = WeeklyReflectionOutput

    def run(self, data: WeeklyReflectionInput, context: dict) -> WeeklyReflectionOutput:
        session: Session = context["session"]
        self.log_input = None
        window_days = max(1, data.window_days)
        lang = data.lang if data.lang in {"zh", "en"} else "zh"
        window_start = data.as_of - timedelta(days=window_days - 1)
        window_end = data.as_of

        logs = session.exec(
            select(DayLog).where(DayLog.date >= window_start, DayLog.date <= window_end)
        ).all()
        log_by_date = {log.date: log for log in logs}

        window_dates = [window_start + timedelta(days=offset) for offset in range(window_days)]
        missing_dates = [
            target_date.isoformat()
            for target_date in window_dates
            if not _log_has_content(log_by_date.get(target_date))
        ]
        logged_days = window_days - len(missing_dates)

        plans = session.exec(
            select(DailyPlan).where(
                DailyPlan.date >= window_start, DailyPlan.date <= window_end
            )
        ).all()
        plan_ids = [plan.id for plan in plans]
        items = (
            session.exec(select(PlanItem).where(PlanItem.daily_plan_id.in_(plan_ids))).all()
            if plan_ids
            else []
        )
        completed_items = [item for item in items if item.completed_at]
        daily_plan_rate = len(completed_items) / len(items) if items else 0.0
        weekly_plan_done = len(completed_items)

        habits_context = _build_habits_context(
            session=session,
            items=items,
            window_start=window_start,
            window_end=window_end,
        )
        logs_context = _build_logs_context(window_dates, log_by_date)
        top_topics, topic_scores, tag_counts = _extract_topics(logs)
        topic_labels = _topic_labels(lang)
        top_topic_labels = [_label_topic(topic, topic_labels) for topic in top_topics]
        weekly_context = {
            "lang": lang,
            "window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            "logs": logs_context,
            "habits": habits_context,
            "stats": {
                "logged_days": logged_days,
                "missing_dates": missing_dates,
                "daily_plan_rate": daily_plan_rate,
                "weekly_plan_done": weekly_plan_done,
            },
        }
        self.log_input = _weekly_context_summary(weekly_context, top_topic_labels)
        rule_payload = _build_rules_payload(
            weekly_context=weekly_context,
            top_topics=top_topics,
            top_topic_labels=top_topic_labels,
            topic_scores=topic_scores,
            tag_counts=tag_counts,
            lang=lang,
        )

        generator_mode = "rules"
        output = rule_payload
        force_rules = data.mode == "rules"
        settings = get_settings(session)
        env_key = os.getenv("LIFEOS_LLM_API_KEY", "").strip()
        env_model = os.getenv("LIFEOS_LLM_MODEL", "").strip()
        llm_key = env_key or (settings.llm_api_key if settings else "")
        llm_model = env_model or (settings.llm_model if settings else "")
        notice = ""
        debug_payload: Dict[str, Any] = {}
        if llm_key and not force_rules:
            llm_output, llm_error, debug_payload = _try_llm_generation(
                llm_model, llm_key, weekly_context, lang
            )
            if llm_output:
                output = llm_output
                generator_mode = "llm"
            else:
                generator_mode = "llm_fallback_rules"
                notice = _llm_notice(
                    llm_error, lang, debug_payload.get("llm_error_summary", "")
                )

        manual = data.trigger == "manual_regenerate"
        generator_mode = _decorate_generator_mode(generator_mode, manual)
        used_data = {
            "logs_days": logged_days,
            "habits_count": len(habits_context),
            "topics": top_topic_labels,
            "missing_count": len(missing_dates),
        }
        metrics = {
            "logged_days": logged_days,
            "missing_dates": missing_dates,
            "top_topics": top_topic_labels,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "daily_plan_rate": daily_plan_rate,
            "weekly_plan_done": weekly_plan_done,
            "generator_mode": generator_mode,
            "lang": lang,
            "used_data": used_data,
        }
        if debug_payload:
            metrics.update(debug_payload)
        if manual:
            metrics["regenerated_at"] = datetime.utcnow().isoformat()
        if notice:
            metrics["notice"] = notice
        output["metrics"] = metrics

        metrics_payload = {
            "opener": output["opener"],
            "highlights": output["highlights"],
            "gaps": output["gaps"],
            "next_steps": output["next_steps"],
            **metrics,
        }

        suggestion = None
        if data.existing_id:
            suggestion = session.exec(
                select(Suggestion).where(Suggestion.id == data.existing_id)
            ).first()
        if suggestion:
            suggestion.reason = output["opener"]
            suggestion.metrics_json = metrics_payload
            session.add(suggestion)
            session.commit()
        else:
            suggestion = Suggestion(
                habit_id=None,
                type="weekly_reflection",
                reason=output["opener"],
                metrics_json=metrics_payload,
            )
            session.add(suggestion)
            session.commit()

        return WeeklyReflectionOutput(**output)


def _log_has_content(log: Optional[DayLog]) -> bool:
    if not log:
        return False
    if (log.journal_md or "").strip():
        return True
    for entry in log.period_entries or []:
        if (entry.get("text", "") or "").strip():
            return True
    return False


def _normalize_text(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def _excerpt_text(text: str, limit: int = 200) -> str:
    cleaned = _normalize_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit]


def _summarize_period_entries(entries: List[Dict[str, Any]]) -> Dict[str, str]:
    buckets = {"morning": [], "afternoon": [], "evening": []}
    for entry in entries or []:
        period = entry.get("period")
        if period not in buckets:
            continue
        text = _normalize_text(entry.get("text", ""))
        if text:
            buckets[period].append(text)
    return {key: _excerpt_text(" / ".join(texts), 200) for key, texts in buckets.items()}


def _collect_tags(log: Optional[DayLog]) -> List[str]:
    if not log:
        return []
    tags: List[str] = []
    tags.extend(log.tags or [])
    for entry in log.period_entries or []:
        tags.extend(entry.get("tags", []) or [])
    cleaned: List[str] = []
    for tag in tags:
        tag_text = str(tag).strip()
        if tag_text and tag_text not in cleaned:
            cleaned.append(tag_text)
    return cleaned


def _build_logs_context(
    window_dates: List[date], log_by_date: Dict[date, DayLog]
) -> List[Dict[str, Any]]:
    logs_context: List[Dict[str, Any]] = []
    for target_date in window_dates:
        log = log_by_date.get(target_date)
        entries = log.period_entries if log else []
        logs_context.append(
            {
                "date": target_date.isoformat(),
                "has_content": _log_has_content(log),
                "tags": _collect_tags(log),
                "period_summaries": _summarize_period_entries(entries),
                "journal_excerpt": _excerpt_text(log.journal_md if log else "", 200),
            }
        )
    return logs_context


def _build_habits_context(
    session: Session, items: List[PlanItem], window_start: date, window_end: date
) -> List[Dict[str, Any]]:
    habit_ids = {item.linked_habit_id for item in items if item.linked_habit_id}
    if not habit_ids:
        return []
    habits = session.exec(select(HabitTemplate).where(HabitTemplate.id.in_(habit_ids))).all()
    done_counts: Dict[int, int] = {}
    for item in items:
        if not item.linked_habit_id or not item.completed_at:
            continue
        done_date = item.completed_at.date()
        if done_date < window_start or done_date > window_end:
            continue
        done_counts[item.linked_habit_id] = done_counts.get(item.linked_habit_id, 0) + 1

    habits_context: List[Dict[str, Any]] = []
    for habit in habits:
        eligible_start = max(window_start, habit.start_date)
        eligible_days = (
            (window_end - eligible_start).days + 1 if eligible_start <= window_end else 0
        )
        target_per_week = max(int(habit.target_per_week or 0), 0)
        if habit.frequency == "weekly":
            expected = min(target_per_week, eligible_days) if target_per_week else 0
            completion_type = "weekly"
        else:
            expected = eligible_days
            completion_type = "daily"
        done_count = done_counts.get(habit.id or 0, 0)
        rate = done_count / expected if expected else 0.0
        habits_context.append(
            {
                "habit_id": habit.id,
                "title": habit.title,
                "frequency": habit.frequency,
                "target_per_week": habit.target_per_week,
                "start_date": habit.start_date.isoformat(),
                "completion": {
                    "type": completion_type,
                    "done_count": done_count,
                    "expected": expected,
                    "rate": min(rate, 1.0),
                },
            }
        )
    return habits_context


def _weekly_context_summary(weekly_context: Dict[str, Any], topics: List[str]) -> Dict[str, Any]:
    stats = weekly_context.get("stats", {})
    return {
        "window": weekly_context.get("window", {}),
        "logged_days": stats.get("logged_days", 0),
        "missing_dates": stats.get("missing_dates", []),
        "habits_count": len(weekly_context.get("habits", [])),
        "topics": topics,
    }


def _label_topic(topic: str, topic_labels: Dict[str, str]) -> str:
    return topic_labels.get(topic, topic)


def _pick_low_habit(
    habits: List[Dict[str, Any]], threshold: float = 0.6
) -> Optional[Dict[str, Any]]:
    candidates = [
        habit
        for habit in habits
        if habit.get("completion", {}).get("expected", 0) > 0
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda habit: habit.get("completion", {}).get("rate", 0.0)
    )
    if candidates[0].get("completion", {}).get("rate", 0.0) >= threshold:
        return None
    return candidates[0]


def _extract_topics(logs: List[DayLog]) -> Tuple[List[str], Dict[str, int], Dict[str, int]]:
    topic_keywords = {
        "health": ["健康", "身体", "运动", "睡眠", "饮食", "锻炼"],
        "product": ["项目", "进展", "开发", "迭代", "版本", "需求", "修复"],
        "learning": ["学习", "阅读", "课程", "笔记", "练习"],
        "relationship": ["关系", "朋友", "家人", "沟通", "陪伴"],
        "emotion": ["情绪", "心情", "焦虑", "开心", "难过", "压力", "放松"],
        "rest": ["休息", "恢复", "放空", "休假", "冥想"],
    }
    scores: Dict[str, int] = {key: 0 for key in topic_keywords}
    tag_counts: Dict[str, int] = {}
    period_counts: Dict[str, int] = {"morning": 0, "afternoon": 0, "evening": 0}
    text_chunks: List[str] = []
    for log in logs:
        for tag in _collect_tags(log):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        for entry in log.period_entries or []:
            text = entry.get("text", "")
            if text:
                text_chunks.append(text)
            period = entry.get("period")
            if period in period_counts and _normalize_text(text):
                period_counts[period] += 1
        if log.journal_md:
            text_chunks.append(log.journal_md)
    text = _normalize_text(" ".join(text_chunks)).lower()
    for topic, keywords in topic_keywords.items():
        for kw in keywords:
            if kw.lower() in text:
                scores[topic] += 1

    topics: List[str] = []
    if tag_counts:
        ranked_tags = sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)
        topics.extend([tag for tag, _ in ranked_tags])

    if len(topics) < 2:
        ranked_topics = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        for key, score in ranked_topics:
            if score > 0 and key not in topics:
                topics.append(key)
            if len(topics) >= 4:
                break

    if len(topics) < 2:
        ranked_periods = sorted(period_counts.items(), key=lambda item: item[1], reverse=True)
        for period, count in ranked_periods:
            if count > 0 and period not in topics:
                topics.append(period)
            if len(topics) >= 2:
                break

    if not topics:
        topics = ["rhythm", "reflection"]

    return topics[:4], scores, tag_counts



def _build_rules_payload(
    weekly_context: Dict[str, Any],
    top_topics: List[str],
    top_topic_labels: List[str],
    topic_scores: Dict[str, int],
    tag_counts: Dict[str, int],
    lang: str,
) -> Dict[str, Any]:
    stats = weekly_context.get("stats", {})
    missing_dates = stats.get("missing_dates", [])
    opener = _opener_for_counts(stats.get("logged_days", 0), len(missing_dates), lang)
    highlights = _build_highlights(weekly_context, top_topic_labels, lang)
    gaps = _build_gaps(missing_dates, lang)
    next_steps = _build_next_steps(weekly_context, lang)

    window = weekly_context.get("window", {})
    return {
        "opener": opener,
        "highlights": highlights,
        "gaps": gaps,
        "next_steps": next_steps,
        "metrics": {
            "window_start": window.get("start"),
            "window_end": window.get("end"),
        },
    }



def _opener_for_counts(logged_days: int, missing_count: int, lang: str) -> str:
    if lang == "en":
        if logged_days >= 5:
            return "🌿 You kept a steady rhythm this week; your notes show real presence."
        if logged_days >= 3:
            return "✨ Nice consistency this week; let's keep it gentle and steady."
        if logged_days >= 1:
            return "🌤 Even a few notes matter. Thanks for showing up for yourself."
        return "🌙 No entries this week is okay; whenever you're ready, we can start today."
    if logged_days >= 5:
        return "🌿 本周节奏很稳，笔记里有真实的在场感。"
    if logged_days >= 3:
        return "✨ 这周挺稳定的，保持轻柔的节奏就很好。"
    if logged_days >= 1:
        return "🌤 有几条记录也很珍贵，谢谢你陪自己。"
    return "🌙 这一周没有记录也没关系，准备好我们再从今天开始。"


def _build_highlights(
    weekly_context: Dict[str, Any],
    top_topic_labels: List[str],
    lang: str,
) -> List[str]:
    stats = weekly_context.get("stats", {})
    logged_days = stats.get("logged_days", 0)
    highlights: List[str] = []
    if logged_days == 0:
        if lang == "en":
            return ["This week felt like a soft pause", "Giving yourself space is also care"]
        return ["这周像是一个轻柔的暂停", "给自己留白也是一种照顾"]

    for topic in top_topic_labels[:3]:
        if lang == "en":
            highlights.append(f"In your notes, {topic} stood out this week.")
        else:
            highlights.append(f"这周的记录里，{topic}很突出。")
    if len(highlights) < 2:
        if lang == "en":
            highlights.append(f"You left notes on {logged_days} day(s), which is real evidence of care.")
        else:
            highlights.append(f"你记录了 {logged_days} 天，这是实打实的投入。")
    if len(highlights) < 2:
        if lang == "en":
            highlights.append("Your week carries its own rhythm across small moments.")
        else:
            highlights.append("你这一周在小事里也保持了节奏。")

    return highlights[:4]


def _build_gaps(missing_dates: List[str], lang: str) -> Dict[str, Any]:
    if not missing_dates:
        return {
            "missing_dates": [],
            "message": "Nice; your week looks complete."
            if lang == "en"
            else "很棒，这周的记录很完整。",
            "links": [],
        }

    links = [
        {"date": missing_date, "url": f"/logs?target_date={missing_date}"}
        for missing_date in missing_dates
    ]
    return {
        "missing_dates": missing_dates,
        "message": "A few days are blank; add a tiny note if you feel like it."
        if lang == "en"
        else "有几天还空着，想的话补一两句也行。",
        "links": links,
    }


def _build_next_steps(weekly_context: Dict[str, Any], lang: str) -> List[str]:
    stats = weekly_context.get("stats", {})
    missing_dates = stats.get("missing_dates", [])
    logged_days = stats.get("logged_days", 0)
    daily_plan_rate = stats.get("daily_plan_rate", 0.0)
    habits = weekly_context.get("habits", [])

    steps: List[str] = []
    if missing_dates:
        steps.append(
            "Pick one missing day and add 2-3 lines - no need to be complete."
            if lang == "en"
            else "挑一天空白日补 2-3 句就好，不必完整。"
        )

    low_habit = _pick_low_habit(habits)
    if low_habit:
        completion = low_habit.get("completion", {})
        done = completion.get("done_count", 0)
        expected = completion.get("expected", 0)
        title = low_habit.get("title", "")
        if lang == "en":
            steps.append(
                f"Try a lighter version of {title} this week ({done}/{expected})."
            )
        else:
            steps.append(f"这周给 {title} 设个更轻的版本（{done}/{expected}）。")

    if not steps:
        if logged_days == 0:
            steps.append(
                "Write one gentle line tonight: 'the thing I cared most about today.'"
                if lang == "en"
                else "今晚写一句温柔的话：'今天我最在意的一件事。'"
            )
        elif daily_plan_rate and daily_plan_rate < 0.6:
            steps.append(
                "Pick one easy plan item each day to keep the chain warm."
                if lang == "en"
                else "每天挑一个简单计划项，先把链条热起来。"
            )
        else:
            steps.append(
                f"Keep the rhythm you already built across {logged_days} day(s)."
                if lang == "en"
                else f"把你已经建立的节奏延续下去（{logged_days} 天）。"
            )

    return steps[:2]


def _try_llm_generation(
    model_key: str, api_key: str, weekly_context: Dict[str, Any], lang: str
) -> tuple[Optional[Dict[str, Any]], str, Dict[str, Any]]:
    context_missing_dates = weekly_context.get("stats", {}).get("missing_dates", [])
    if lang == "en":
        prompt = (
            "You are a kind weekly reflection assistant.\n"
            "weekly_context is the ONLY source of truth. Do not invent anything.\n"
            "Output JSON ONLY (no markdown, no extra text). Must be valid for json.loads.\n"
            "JSON fields must be exactly: opener (1 sentence with emoji), "
            "highlights (array of strings), "
            "gaps ({missing_dates:[str], message:str, links:[{date:str,url:str}]}), "
            "next_steps (array of strings), metrics (object).\n"
            "\n"
            "Coverage requirements (must satisfy all):\n"
            "A) Highlights must cover BOTH logs and habits/plans:\n"
            "   - Provide 4 highlights.\n"
            "   - At least 2 highlights summarize LOGS using tags/topics/keywords that appear in weekly_context.\n"
            "   - At least 1 highlight summarizes HABITS (completion, streaks, or consistency) from weekly_context.\n"
            "   - At least 1 highlight summarizes PLANS/OBJECTIVES (progress or completion) from weekly_context.\n"
            "B) Gaps:\n"
            "   - gaps.missing_dates must list missing log dates from weekly_context (if none, empty array).\n"
            "   - gaps.message must mention BOTH (i) log coverage issues and (ii) habit/plan friction if evidenced.\n"
            "   - gaps.links should include only links present in weekly_context; otherwise [].\n"
            "C) Next steps:\n"
            "   - Provide 2 next_steps.\n"
            "   - One next_step must address missing_dates (specific action).\n"
            "   - One next_step must be tied to a real pattern in habits or plan completion (specific action).\n"
            "D) Metrics must include these keys (use numbers/strings as appropriate):\n"
            "   log_days_recorded, log_days_missing, top_tags, "
            "   habit_done, habit_total, habit_completion_rate, "
            "   plan_done, plan_total, plan_completion_rate.\n"
            "\n"
            "Style:\n"
            "- Encouraging, low-pressure, non-judgmental.\n"
            "- Avoid diagnoses.\n"
            "- Each output string should include at least one emoji.\n"
            f"weekly_context: {json.dumps(weekly_context, ensure_ascii=False)}"
        )
    else:
        prompt = (
            "你是一位温柔、真诚的周度复盘助手。\n"
            "你必须严格遵守：\n"
            "1) weekly_context 是唯一事实来源，不得编造、不得脑补。\n"
            "2) 输出必须是【纯 JSON】且可被 json.loads 解析。禁止 Markdown、禁止代码块、禁止额外解释文字。\n"
            "3) 输出字段严格为：\n"
            '   {"opener": str, "highlights": [str], '
            '    "gaps": {"missing_dates":[str], "message": str, "links":[{"date": str, "url": str}]}, '
            '    "next_steps": [str], "metrics": object}\n'
            "   注意：键名必须保持英文，不允许翻译成中文。\n"
            "\n"
            "覆盖要求（必须全部满足）：\n"
            "A) highlights 必须同时覆盖【日志 logs】与【习惯 habits / 计划 plans】：\n"
            "   - 固定输出 4 条 highlights。\n"
            "   - 至少 2 条必须总结日志（logs），且必须引用 weekly_context 中真实出现过的主题/标签/关键词（tags/topics/关键词统计）。\n"
            "   - 至少 1 条必须总结习惯（habits）的完成情况/连续性/波动（必须有 weekly_context 证据）。\n"
            "   - 至少 1 条必须总结计划或目标（plans/objectives）的完成/推进情况（必须有 weekly_context 证据）。\n"
            "B) gaps：\n"
            "   - gaps.missing_dates 仅从 weekly_context 读取漏记日期；没有则 []。\n"
            "   - gaps.message 必须同时提到：①日志覆盖问题（例如漏记/分布不均）②习惯或计划的阻力点（如果 weekly_context 中有完成率或未完成证据）。\n"
            "   - gaps.links 只能使用 weekly_context 中已有的链接；没有则 []。\n"
            "C) next_steps：\n"
            "   - 固定输出 2 条 next_steps。\n"
            "   - 至少 1 条必须直接针对 missing_dates（给出具体可执行动作）。\n"
            "   - 另 1 条必须绑定 weekly_context 的真实模式（习惯完成率/计划完成情况/明显波动之一），给出具体可执行动作。\n"
            "D) metrics 必须包含以下键（值可为数字/字符串/数组，但要可 JSON 化）：\n"
            "   log_days_recorded, log_days_missing, top_tags, "
            "   habit_done, habit_total, habit_completion_rate, "
            "   plan_done, plan_total, plan_completion_rate。\n"
            "\n"
            "语气与格式：\n"
            "- 鼓励、低压、不评判；不要指责；不要做心理诊断。\n"
            "- opener 必须 1 句话并带 emoji。\n"
            "- highlights/next_steps 的每一条字符串都至少包含 1 个 emoji。\n"
            f"weekly_context: {json.dumps(weekly_context, ensure_ascii=False)}"
        )

    try:
        content, finish_reason, used_response_format, response_meta = _call_llm(
            model_key, api_key, prompt, lang
        )
    except LlmCallError as exc:
        debug = _build_llm_error_payload(exc.details)
        _log_llm_failure(debug)
        return None, "llm_error", debug
    if not content:
        debug = _build_llm_error_payload(_make_empty_response_details(model_key))
        _log_llm_failure(debug)
        return None, "llm_empty", debug
    debug = {
        "llm_finish_reason": finish_reason,
        "llm_stream": False,
        "llm_response_format": used_response_format,
    }
    if response_meta:
        debug.update(response_meta)
    if finish_reason == "length":
        debug["llm_raw_text"] = _truncate_text(content)
        debug.update(
            _build_llm_error_payload(
                _make_output_error_details(
                    model_key, "TruncatedResponse", "finish_reason=length"
                )
            )
        )
        return None, "llm_truncated", debug
    raw_text = content
    cleaned = _clean_llm_text(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        debug["llm_raw_text"] = _truncate_text(raw_text)
        debug["llm_parse_error"] = f"{exc}"
        repair_result, repair_debug = _repair_llm_json(
            model_key, api_key, weekly_context, lang
        )
        debug.update(repair_debug)
        if repair_result is None:
            debug.update(
                _build_llm_error_payload(
                    _make_parse_error_details(model_key, raw_text, exc)
                )
            )
            _log_llm_failure(debug)
            return None, "llm_invalid_json", debug
        parsed = repair_result

    opener = (parsed.get("opener") or "").strip()
    highlights = [str(item).strip() for item in parsed.get("highlights", []) if str(item).strip()]
    gaps = parsed.get("gaps", {})
    next_steps = [str(item).strip() for item in parsed.get("next_steps", []) if str(item).strip()]

    if not opener or len(highlights) < 2 or len(next_steps) < 1:
        debug["llm_raw_text"] = _truncate_text(raw_text)
        repair_result, repair_debug = _repair_llm_json(
            model_key, api_key, weekly_context, lang
        )
        debug.update(repair_debug)
        if repair_result is not None:
            opener = (repair_result.get("opener") or "").strip()
            highlights = [
                str(item).strip()
                for item in repair_result.get("highlights", [])
                if str(item).strip()
            ]
            gaps = repair_result.get("gaps", {})
            next_steps = [
                str(item).strip()
                for item in repair_result.get("next_steps", [])
                if str(item).strip()
            ]
        if not opener or len(highlights) < 2 or len(next_steps) < 1:
            debug.update(
                _build_llm_error_payload(
                    _make_output_error_details(
                        model_key, "MissingFields", "missing required fields"
                    )
                )
            )
            return None, "llm_missing_fields", debug

    normalized_gaps = _normalize_gaps(gaps)
    context_gaps = _build_gaps(context_missing_dates, lang)
    normalized_gaps["missing_dates"] = context_gaps.get("missing_dates", [])
    normalized_gaps["links"] = context_gaps.get("links", [])
    if not normalized_gaps.get("message"):
        normalized_gaps["message"] = context_gaps.get("message", "")

    return {
        "opener": opener,
        "highlights": highlights[:4],
        "gaps": normalized_gaps,
        "next_steps": next_steps[:2],
        "metrics": {},
    }, "", debug

def _decorate_generator_mode(generator_mode: str, manual: bool) -> str:
    if not manual:
        return generator_mode
    if generator_mode == "llm":
        return "llm_manual_regenerate"
    if generator_mode == "llm_fallback_rules":
        return "llm_failed_fallback_rules_manual_regenerate"
    return "rules_manual_regenerate"


def _topic_labels(lang: str) -> Dict[str, str]:
    if lang == "en":
        return {
            "health": "health & body",
            "product": "projects & progress",
            "learning": "learning & growth",
            "relationship": "relationships",
            "emotion": "emotions",
            "rest": "rest & recovery",
            "morning": "morning notes",
            "afternoon": "afternoon notes",
            "evening": "evening notes",
            "rhythm": "daily rhythm",
            "reflection": "reflection",
        }
    return {
        "health": "健康与身体",
        "product": "项目与进展",
        "learning": "学习与成长",
        "relationship": "关系",
        "emotion": "情绪",
        "rest": "休息与恢复",
        "morning": "晨间记录",
        "afternoon": "午间记录",
        "evening": "晚间记录",
        "rhythm": "每日节奏",
        "reflection": "复盘",
    }


def _normalize_gaps(gaps: Dict[str, Any]) -> Dict[str, Any]:
    missing_dates = gaps.get("missing_dates") or []
    links = gaps.get("links") or []
    return {
        "missing_dates": [str(item) for item in missing_dates],
        "message": str(gaps.get("message") or "").strip(),
        "links": links,
    }


def _clean_llm_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    block = _extract_json_block(cleaned)
    return block or cleaned


def _extract_json_block(text: str) -> Optional[str]:
    start_idx = None
    stack: List[str] = []
    in_string = False
    escape = False
    for idx, ch in enumerate(text):
        if start_idx is None:
            if ch in "{[":
                start_idx = idx
                stack.append(ch)
            continue
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in "{[":
            stack.append(ch)
            continue
        if ch in "}]":
            if stack:
                stack.pop()
            if start_idx is not None and not stack:
                return text[start_idx : idx + 1]
    return None


def _truncate_text(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_llm_notice(base: str, summary: str, lang: str) -> str:
    suffix = ""
    if summary:
        suffix = f" ({summary})" if lang == "en" else f"（{summary}）"
    if lang == "en":
        return f"{base}{suffix}; fell back to rules."
    return f"{base}{suffix}，已回退规则生成。"

def _llm_notice(error_code: str, lang: str, error_summary: str = "") -> str:
    if not error_code:
        return ""
    summary = (error_summary or "").strip()
    if lang == "en":
        if error_code == "llm_invalid_json":
            return _format_llm_notice("LLM returned non-JSON output", summary, lang)
        if error_code == "llm_truncated":
            return _format_llm_notice("LLM response was truncated", summary, lang)
        return _format_llm_notice("LLM failed", summary, lang)
    if error_code == "llm_invalid_json":
        return _format_llm_notice("LLM 输出非 JSON", summary, lang)
    if error_code == "llm_truncated":
        return _format_llm_notice("LLM 响应被截断", summary, lang)
    return _format_llm_notice("LLM 调用失败", summary, lang)


def _build_llm_error_payload(details: Dict[str, Any]) -> Dict[str, Any]:
    summary = _summarize_llm_error(details)
    payload = {**details}
    if summary:
        payload["llm_error_summary"] = summary
    if "llm_stream" not in payload:
        payload["llm_stream"] = False
    return payload


def _summarize_llm_error(details: Dict[str, Any]) -> str:
    status = details.get("http_status")
    error_type = details.get("error_type") or "LLMError"
    message = details.get("error_message") or ""
    message = _truncate_text(message.replace("\n", " ").strip(), 200)
    if status:
        return f"{status} {message}".strip()
    if message:
        return f"{error_type}: {message}".strip()
    return error_type


def _log_llm_failure(details: Dict[str, Any]) -> None:
    try:
        logger.error("LLM call failed: %s", json.dumps(details, ensure_ascii=True))
    except Exception:  # noqa: BLE001
        logger.error("LLM call failed (unserializable details)")


def _extract_response_meta(resp: Any, model_id: str) -> Dict[str, Any]:
    request_id = ""
    for attr in ("request_id", "_request_id"):
        value = getattr(resp, attr, None)
        if value:
            request_id = str(value)
            break
    return {
        "provider": LLM_PROVIDER,
        "model_id": model_id,
        "base_url": LLM_BASE_URL,
        "request_id": request_id,
    }


def _coerce_response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=True)
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _make_llm_error_details(
    model_id: str, exc: Exception, stream: bool, enable_thinking: bool
) -> Dict[str, Any]:
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    response = getattr(exc, "response", None)
    response_text = ""
    if response is not None:
        status = status or getattr(response, "status_code", None)
        headers = getattr(response, "headers", None) or {}
        request_id = request_id or headers.get("x-request-id") or headers.get("X-Request-Id")
        response_text = _coerce_response_text(
            getattr(response, "text", None) or getattr(response, "content", None)
        )
    body = getattr(exc, "body", None)
    if body and not response_text:
        response_text = _coerce_response_text(body)
    error_message = str(exc)
    if response_text:
        error_message = f"{error_message} | response: {_truncate_text(response_text, 800)}"
    return {
        "provider": LLM_PROVIDER,
        "model_id": model_id,
        "base_url": LLM_BASE_URL,
        "http_status": status,
        "error_type": type(exc).__name__,
        "error_message": _truncate_text(error_message, 800),
        "request_id": str(request_id or ""),
        "llm_stream": stream,
        "llm_enable_thinking": enable_thinking,
    }


def _make_parse_error_details(
    model_id: str, raw_text: str, exc: json.JSONDecodeError
) -> Dict[str, Any]:
    model_id = model_id or "Qwen/Qwen2.5-Coder-32B-Instruct"
    return {
        "provider": LLM_PROVIDER,
        "model_id": model_id,
        "base_url": LLM_BASE_URL,
        "http_status": None,
        "error_type": type(exc).__name__,
        "error_message": _truncate_text(f"{exc} | response: {raw_text}", 800),
        "request_id": "",
    }


def _make_empty_response_details(model_id: str) -> Dict[str, Any]:
    model_id = model_id or "Qwen/Qwen2.5-Coder-32B-Instruct"
    return {
        "provider": LLM_PROVIDER,
        "model_id": model_id,
        "base_url": LLM_BASE_URL,
        "http_status": None,
        "error_type": "EmptyResponse",
        "error_message": "Empty response content.",
        "request_id": "",
    }


def _make_output_error_details(
    model_id: str, error_type: str, message: str
) -> Dict[str, Any]:
    model_id = model_id or "Qwen/Qwen2.5-Coder-32B-Instruct"
    return {
        "provider": LLM_PROVIDER,
        "model_id": model_id,
        "base_url": LLM_BASE_URL,
        "http_status": None,
        "error_type": error_type,
        "error_message": _truncate_text(message, 800),
        "request_id": "",
    }


def _call_llm(
    model_key: str, api_key: str, prompt: str, lang: str, stream: bool = False
) -> Tuple[str, str, bool, Dict[str, Any]]:
    if OpenAI is None:
        return "", "", False, {}
    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)
    model_id = model_key or "Qwen/Qwen2.5-Coder-32B-Instruct"
    messages = [
        {
            "role": "system",
            "content": "You are a gentle weekly reflection assistant."
            if lang == "en"
            else "你是一位温柔的周度复盘助手。",
        },
        {"role": "user", "content": prompt},
    ]
    extra_body = {"enable_thinking": True}
    if not stream:
        extra_body["enable_thinking"] = False
    enable_thinking = bool(extra_body.get("enable_thinking", False))

    request_kwargs = {
        "model": model_id,
        "messages": messages,
        "stream": stream,
        "temperature": 0,
    }
    if extra_body:
        request_kwargs["extra_body"] = extra_body
    try:
        resp = client.chat.completions.create(
            **request_kwargs,
            response_format={"type": "json_object"},
        )
        used_response_format = True
    except Exception as exc:
        try:
            resp = client.chat.completions.create(**request_kwargs)
            used_response_format = False
        except Exception as exc_fallback:
            raise LlmCallError(
                _make_llm_error_details(model_id, exc_fallback, stream, enable_thinking)
            ) from exc_fallback
    if not resp.choices:
        return "", "", used_response_format, {}
    message = resp.choices[0].message
    finish_reason = resp.choices[0].finish_reason or ""
    reasoning_content = getattr(message, "reasoning_content", "") or ""
    response_meta = _extract_response_meta(resp, model_id)
    response_meta["llm_stream"] = stream
    response_meta["llm_enable_thinking"] = enable_thinking
    if reasoning_content:
        response_meta["llm_reasoning_content_present"] = True
    return (message.content or "").strip(), finish_reason, used_response_format, response_meta


def _build_repair_prompt(weekly_context: Dict[str, Any], lang: str) -> str:
    schema = (
        '{"opener": str, "highlights": [str], '
        '"gaps": {"missing_dates":[str], "message": str, "links":[{"date": str, "url": str}]}, '
        '"next_steps": [str], "metrics": object}'
    )
    if lang == "en":
        return (
            "Return ONLY valid JSON for the following schema. "
            "No markdown, no explanations. "
            f"Schema: {schema}. "
            f"weekly_context: {json.dumps(weekly_context, ensure_ascii=False)}"
        )
    return (
        "请只返回符合 schema 的纯 JSON。禁止 Markdown、禁止解释文字。"
        "键名必须保持英文，不允许翻译成中文。"
        f"Schema: {schema}. "
        f"weekly_context: {json.dumps(weekly_context, ensure_ascii=False)}"
    )

def _repair_llm_json(
    model_key: str, api_key: str, weekly_context: Dict[str, Any], lang: str
) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    prompt = _build_repair_prompt(weekly_context, lang)
    debug: Dict[str, Any] = {"llm_repair_attempted": True}
    try:
        content, finish_reason, used_response_format, response_meta = _call_llm(
            model_key, api_key, prompt, lang
        )
    except LlmCallError as exc:
        debug["llm_repair_failed"] = True
        debug.update(_build_llm_error_payload(exc.details))
        _log_llm_failure(debug)
        return None, debug
    debug.update(
        {
            "llm_repair_finish_reason": finish_reason,
            "llm_repair_response_format": used_response_format,
        }
    )
    if response_meta:
        debug.update(response_meta)
    cleaned = _clean_llm_text(content or "")
    try:
        return json.loads(cleaned), debug
    except json.JSONDecodeError as exc:
        debug["llm_repair_parse_error"] = f"{exc}"
        debug["llm_repair_raw_text"] = _truncate_text(content or "")
        return None, debug


def get_skill() -> Skill:
    return ReviewWeeklyReflectionSkill()
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None
