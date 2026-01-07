from __future__ import annotations

import json
from datetime import date, datetime, timedelta
import os
from typing import Tuple
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.data.repo import get_settings
from app.domain.models import DayLog, Suggestion


class WeeklyReflectionInput(BaseModel):
    as_of: date
    window_days: int = 7
    lang: str = "zh"
    existing_id: Optional[int] = None
    trigger: Optional[str] = None


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
        window_days = max(1, data.window_days)
        lang = data.lang if data.lang in {"zh", "en"} else "zh"
        window_start = data.as_of - timedelta(days=window_days - 1)
        window_end = data.as_of

        logs = session.exec(
            select(DayLog).where(DayLog.date >= window_start, DayLog.date <= window_end)
        ).all()
        log_by_date = {log.date: log for log in logs}

        missing_dates = [
            (window_start + timedelta(days=offset)).isoformat()
            for offset in range(window_days)
            if not _log_has_content(log_by_date.get(window_start + timedelta(days=offset)))
        ]
        logged_days = window_days - len(missing_dates)

        top_topics, topic_scores = _extract_topics(logs)
        topic_labels = _topic_labels(lang)
        top_topic_labels = [topic_labels.get(key, key) for key in top_topics]
        rule_payload = _build_rules_payload(
            logs=logs,
            window_start=window_start,
            window_end=window_end,
            logged_days=logged_days,
            missing_dates=missing_dates,
            top_topics=top_topics,
            top_topic_labels=top_topic_labels,
            topic_scores=topic_scores,
            lang=lang,
        )

        generator_mode = "rules"
        output = rule_payload
        settings = get_settings(session)
        env_key = os.getenv("LIFEOS_LLM_API_KEY", "").strip()
        env_model = os.getenv("LIFEOS_LLM_MODEL", "").strip()
        llm_key = env_key or (settings.llm_api_key if settings else "")
        llm_model = env_model or (settings.llm_model if settings else "")
        notice = ""
        debug_payload: Dict[str, Any] = {}
        if llm_key:
            llm_output, llm_error, debug_payload = _try_llm_generation(
                llm_model, llm_key, rule_payload, lang
            )
            if llm_output:
                output = llm_output
                generator_mode = "llm"
            else:
                generator_mode = "llm_fallback_rules"
                notice = _llm_notice(llm_error, lang)

        manual = data.trigger == "manual_regenerate"
        generator_mode = _decorate_generator_mode(generator_mode, manual)
        metrics = {
            "logged_days": logged_days,
            "missing_dates": missing_dates,
            "top_topics": top_topic_labels,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "generator_mode": generator_mode,
            "lang": lang,
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


def _extract_topics(logs: List[DayLog]) -> Tuple[List[str], Dict[str, int]]:
    topic_keywords = {
        "health": ["运动", "锻炼", "晨跑", "睡眠", "休息", "健康"],
        "product": ["产品", "迭代", "上线", "开发", "接口", "优化", "项目"],
        "learning": ["学习", "阅读", "课程", "写作", "复盘"],
        "relationship": ["家人", "朋友", "陪伴", "沟通", "关系"],
        "emotion": ["情绪", "焦虑", "开心", "压力", "平静", "难过", "放松"],
        "rest": ["休闲", "娱乐", "散步", "音乐", "电影"],
    }
    scores: Dict[str, int] = {key: 0 for key in topic_keywords}
    for log in logs:
        for tag in log.tags or []:
            if tag in scores:
                scores[tag] += 3
        for entry in log.period_entries or []:
            for tag in entry.get("tags", []) or []:
                if tag in scores:
                    scores[tag] += 3
        text = " ".join(
            [entry.get("text", "") for entry in (log.period_entries or [])] + [log.journal_md]
        ).lower()
        for topic, keywords in topic_keywords.items():
            for kw in keywords:
                if kw.lower() in text:
                    scores[topic] += 1

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_topics = [k for k, score in ranked if score > 0][:4]
    if not top_topics:
        top_topics = ["rhythm", "reflection"]
    return top_topics, scores


def _build_rules_payload(
    logs: List[DayLog],
    window_start: date,
    window_end: date,
    logged_days: int,
    missing_dates: List[str],
    top_topics: List[str],
    top_topic_labels: List[str],
    topic_scores: Dict[str, int],
    lang: str,
) -> Dict[str, Any]:
    opener = _opener_for_counts(logged_days, len(missing_dates), lang)
    highlights = _build_highlights(logged_days, top_topics, top_topic_labels, lang)
    gaps = _build_gaps(missing_dates, lang)
    next_steps = _build_next_steps(logged_days, len(missing_dates), lang)

    return {
        "opener": opener,
        "highlights": highlights,
        "gaps": gaps,
        "next_steps": next_steps,
        "metrics": {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
    }


def _opener_for_counts(logged_days: int, missing_count: int, lang: str) -> str:
    if lang == "en":
        if logged_days >= 5:
            return "✨ You kept a steady rhythm this week; your notes show real momentum."
        if logged_days >= 3:
            return "🌤️ Nice consistency this week—let’s keep the pace gentle and steady."
        if logged_days >= 1:
            return "🤍 It’s okay if the week felt messy. Even a few notes matter."
        return "🫶 No entries this week is okay—whenever you’re ready, we can start today."
    if logged_days >= 5:
        return "✨ 这一周你很有节奏感，记录里能感受到你的推进力。"
    if logged_days >= 3:
        return "🌤️ 这周记录还不错，我们一起把节奏稳住。"
    if logged_days >= 1:
        return "🤍 这一周有些忙乱也没关系，愿意记录就已经很棒了。"
    return "🫶 这一周没有记录也没关系，我们随时可以从今天开始。"


def _build_highlights(
    logged_days: int,
    top_topics: List[str],
    top_topic_labels: List[str],
    lang: str,
) -> List[str]:
    highlights: List[str] = []
    if logged_days == 0:
        if lang == "en":
            return ["This week felt like a soft pause", "Giving yourself space is also care"]
        return ["这一周像是一次缓冲的空档", "留给自己一些空间也是一种照顾"]

    for topic in top_topic_labels[:3]:
        if lang == "en":
            highlights.append(f"In your notes, {topic} stood out this week.")
        else:
            highlights.append(f"本周的记录里，{topic}是比较突出的主题。")
    if len(highlights) < 2:
        if lang == "en":
            highlights.append("You left your own rhythm across different moments.")
        else:
            highlights.append("你在不同片段里留下了属于自己的节奏。")

    return highlights[:4]


def _build_gaps(missing_dates: List[str], lang: str) -> Dict[str, Any]:
    if not missing_dates:
        return {
            "missing_dates": [],
            "message": "Nice—your week looks complete."
            if lang == "en"
            else "这周记录很完整，已经很不错了。",
            "links": [],
        }

    links = [
        {"date": missing_date, "url": f"/logs?target_date={missing_date}"}
        for missing_date in missing_dates
    ]
    return {
        "missing_dates": missing_dates,
        "message": "A few days are blank—add a tiny note if you feel like it."
        if lang == "en"
        else "这周还有几天未记录，如果愿意可以补上一点点。",
        "links": links,
    }


def _build_next_steps(logged_days: int, missing_count: int, lang: str) -> List[str]:
    steps: List[str] = []
    if missing_count:
        steps.append(
            "Pick one day and add 2-3 lines—no need to be complete."
            if lang == "en"
            else "挑 1 天补记 2-3 句就好，不用追求完整。"
        )
    if logged_days == 0:
        steps.append(
            'Write one line tonight: "the thing I cared most about today."'
            if lang == "en"
            else "今晚先写一句“今天最在意的事”。"
        )
    else:
        steps.append(
            "Keep the most energizing tiny habit and move gently forward."
            if lang == "en"
            else "保留最有能量的一条小习惯，继续轻轻推进。"
        )
    return steps[:2]


def _try_llm_generation(
    model_key: str, api_key: str, rule_payload: Dict[str, Any], lang: str
) -> tuple[Optional[Dict[str, Any]], str, Dict[str, Any]]:
    base_payload = {
        "opener": rule_payload["opener"],
        "highlights": rule_payload["highlights"],
        "gaps": rule_payload["gaps"],
        "next_steps": rule_payload["next_steps"],
    }
    if lang == "en":
        prompt = (
            "You are a kind weekly reflection assistant. Generate JSON based on the input."
            "Fields: opener(1 sentence with emoji), highlights(2-4),"
            "gaps(missing_dates/message/links), next_steps(1-2)."
            "Tone: gentle, non-judgmental. Output JSON only."
            f"Input reference: {json.dumps(base_payload, ensure_ascii=False)}"
        )
    else:
        prompt = (
            "你是情绪价值回顾助手，请根据输入数据生成 JSON。"
            "输出字段：opener(1句含emoji)、highlights(2-4条)、gaps(包含missing_dates/message/links)、"
            "next_steps(1-2条)。语气温柔不评判。仅输出JSON。"
            f"输入参考：{json.dumps(base_payload, ensure_ascii=False)}"
        )

    try:
        content, finish_reason, used_response_format = _call_llm(
            model_key, api_key, prompt, lang
        )
    except Exception as exc:
        return None, "llm_error", {"llm_error": f"{exc}", "llm_stream": False}
    if not content:
        return None, "llm_empty", {"llm_stream": False}
    debug = {
        "llm_finish_reason": finish_reason,
        "llm_stream": False,
        "llm_response_format": used_response_format,
    }
    if finish_reason == "length":
        debug["llm_raw_text"] = _truncate_text(content)
        return None, "llm_truncated", debug
    raw_text = content
    cleaned = _clean_llm_text(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        debug["llm_raw_text"] = _truncate_text(raw_text)
        debug["llm_parse_error"] = f"{exc}"
        return None, "llm_invalid_json", debug

    opener = (parsed.get("opener") or "").strip()
    highlights = [str(item).strip() for item in parsed.get("highlights", []) if str(item).strip()]
    gaps = parsed.get("gaps", {})
    next_steps = [str(item).strip() for item in parsed.get("next_steps", []) if str(item).strip()]

    if not opener or len(highlights) < 2 or len(next_steps) < 1:
        debug["llm_raw_text"] = _truncate_text(raw_text)
        return None, "llm_missing_fields", debug

    return {
        "opener": opener,
        "highlights": highlights[:4],
        "gaps": _normalize_gaps(gaps),
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


def _llm_notice(error_code: str, lang: str) -> str:
    if not error_code:
        return ""
    if lang == "en":
        if error_code == "llm_invalid_json":
            return "LLM returned non-JSON output; fell back to rules."
        if error_code == "llm_truncated":
            return "LLM response was truncated; fell back to rules."
        return "LLM failed; fell back to rules."
    if error_code == "llm_invalid_json":
        return "LLM 输出非 JSON，已回退规则生成"
    if error_code == "llm_truncated":
        return "LLM 响应被截断，已回退规则生成"
    return "LLM 调用失败，已回退规则生成"


def _topic_labels(lang: str) -> Dict[str, str]:
    if lang == "en":
        return {
            "health": "health & body",
            "product": "projects & progress",
            "learning": "learning & growth",
            "relationship": "relationships",
            "emotion": "emotions",
            "rest": "rest & recovery",
            "rhythm": "daily rhythm",
            "reflection": "reflection",
        }
    return {
        "health": "健康与身体",
        "product": "项目与推进",
        "learning": "学习与成长",
        "relationship": "关系与陪伴",
        "emotion": "情绪与感受",
        "rest": "放松与修复",
        "rhythm": "生活节奏",
        "reflection": "记录感受",
    }


def _normalize_gaps(gaps: Dict[str, Any]) -> Dict[str, Any]:
    missing_dates = gaps.get("missing_dates") or []
    links = gaps.get("links") or []
    return {
        "missing_dates": [str(item) for item in missing_dates],
        "message": str(gaps.get("message") or "").strip(),
        "links": links,
    }


def _call_llm(model_key: str, api_key: str, prompt: str, lang: str) -> Tuple[str, str, bool]:
    if OpenAI is None:
        return "", "", False
    client = OpenAI(api_key=api_key, base_url="https://api-inference.modelscope.cn/v1/")
    model_id = model_key or "Qwen/Qwen2.5-Coder-32B-Instruct"
    messages = [
        {
            "role": "system",
            "content": "You are a gentle weekly reflection assistant."
            if lang == "en"
            else "你是温柔的周度回顾助手。",
        },
        {"role": "user", "content": prompt},
    ]
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=messages,
            stream=False,
            temperature=0,
            response_format={"type": "json_object"},
        )
        used_response_format = True
    except Exception:
        resp = client.chat.completions.create(
            model=model_id,
            messages=messages,
            stream=False,
            temperature=0,
        )
        used_response_format = False
    if not resp.choices:
        return "", "", used_response_format
    message = resp.choices[0].message
    finish_reason = resp.choices[0].finish_reason or ""
    return (message.content or "").strip(), finish_reason, used_response_format


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


def get_skill() -> Skill:
    return ReviewWeeklyReflectionSkill()
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None
