"""TikHub 抖音数据适配器，只实现家庭教育选题所需的最小读链路。"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from qijia_video.errors import ProviderUnavailable
from qijia_video.topic_ports import (
    TikHubCallRecorder,
    TopicCollectionFailed,
    TopicResearchCollection,
)
from qijia_video.topic_contracts import (
    TikHubCallRecord,
    TopicEvidence,
    TopicEvidenceTier,
    TopicEvidenceType,
    TopicLowFollowerDiagnostics,
    TopicMetrics,
    TopicSignalType,
)


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
PLANNED_MAX_TIKHUB_CALLS = 13
MAX_TIKHUB_REQUEST_BUDGET = 100
BILLBOARD_PAGE_SIZE = 20
BATCH_DETAIL_LIMIT = 50

# TikHub 没有公开“低粉爆款”的内部阈值。下面的齐家阈值只用于给已经
# 进入平台榜单的视频增加“指标已复核”标签，不再作为样本入池的一票否决项。
LOW_FOLLOWER_MAX_FOLLOWERS = 50_000
LOW_FOLLOWER_MIN_PLAYS = 500_000
LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO = 20.0
LOW_FOLLOWER_MIN_LIKE_RATE = 0.05
BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE = 0.008
FRESHEST_BREAKOUT_MAX_AGE_HOURS = 24
FRESH_PRIORITY_MAX_AGE_HOURS = 72
RECENT_PRIORITY_MAX_AGE_HOURS = 168
EMERGING_LOW_FOLLOWER_MAX_FOLLOWERS = 100_000
EMERGING_LOW_FOLLOWER_MIN_PLAYS_FRESH = 100_000
EMERGING_LOW_FOLLOWER_MIN_PLAYS = 200_000
EMERGING_LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO = 10.0
EMERGING_LOW_FOLLOWER_MIN_LIKE_RATE = 0.03
EMERGING_LOW_FOLLOWER_MIN_DEEP_ENGAGEMENT_RATE = 0.003
HIGH_HEAT_MIN_PLAYS = 1_000_000
HIGH_HEAT_MIN_LIKE_RATE = 0.08
MIN_USABLE_VIDEO_COUNT = 8
MIN_LOW_FOLLOWER_VIDEO_COUNT = 5
DEFAULT_FAMILY_EDUCATION_QUERIES = (
    "家庭教育",
    "亲子沟通",
    "孩子情绪",
    "学习习惯",
    "青春期教育",
    "父母成长",
)

_VALID_ENDPOINTS = {
    "/api/v1/douyin/billboard/fetch_hot_total_low_fan_list",
    "/api/v1/douyin/billboard/fetch_hot_total_high_like_list",
    "/api/v1/douyin/web/fetch_multi_video",
}

_VIDEO_ID_KEYS = (
    "aweme_id",
    "awemeId",
    "itemId",
    "item_id",
    "itemIdStr",
    "item_id_str",
    "videoId",
    "video_id",
)
_VIDEO_TITLE_KEYS = (
    "desc",
    "itemTitle",
    "item_title",
    "caption",
    "title",
    "itemName",
    "item_name",
    "itemDesc",
    "item_desc",
    "itemDescription",
    "item_description",
    "videoTitle",
    "video_title",
    "contentTitle",
    "content_title",
)
_DIRECT_FAMILY_TERMS = (
    "家庭教育",
    "亲子",
    "父母",
    "家长",
    "孩子",
    "青春期",
    "教养",
)
_SUPPORTING_FAMILY_TERMS = (
    "儿童",
    "育儿",
    "沟通",
    "情绪",
    "学习",
    "作业",
    "习惯",
    "规则",
    "边界",
    "手机",
    "沉迷",
    "叛逆",
    "自驱力",
    "专注力",
    "陪伴",
)
_EXCLUDED_MATERNAL_TERMS = (
    "孕期",
    "备孕",
    "产后",
    "奶粉",
    "辅食",
    "纸尿裤",
    "婴儿车",
    "母乳",
    "待产",
    "好物",
    "穿搭",
)


ProgressReporter = Callable[[dict], None]


def evidence_quality_policy() -> dict[str, dict[str, float | int | bool]]:
    """返回给前端展示的同一份入池与排序口径，避免文案漂移。"""

    return {
        "low_follower_breakout": {
            "max_followers": LOW_FOLLOWER_MAX_FOLLOWERS,
            "min_plays": LOW_FOLLOWER_MIN_PLAYS,
            "min_play_follower_ratio": LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO,
            "min_like_rate": LOW_FOLLOWER_MIN_LIKE_RATE,
            "min_deep_engagement_rate": BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE,
            "hard_gate": False,
        },
        "emerging_low_follower_breakout": {
            "max_followers": EMERGING_LOW_FOLLOWER_MAX_FOLLOWERS,
            "min_plays_fresh": EMERGING_LOW_FOLLOWER_MIN_PLAYS_FRESH,
            "min_plays": EMERGING_LOW_FOLLOWER_MIN_PLAYS,
            "min_play_follower_ratio": (
                EMERGING_LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO
            ),
            "min_like_rate": EMERGING_LOW_FOLLOWER_MIN_LIKE_RATE,
            "min_deep_engagement_rate": (
                EMERGING_LOW_FOLLOWER_MIN_DEEP_ENGAGEMENT_RATE
            ),
            "freshest_age_hours": FRESHEST_BREAKOUT_MAX_AGE_HOURS,
            "hard_gate": False,
        },
        "high_heat_breakout": {
            "min_plays": HIGH_HEAT_MIN_PLAYS,
            "min_like_rate": HIGH_HEAT_MIN_LIKE_RATE,
            "min_deep_engagement_rate": BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE,
            "hard_gate": False,
        },
        "ranking": {
            "fresh_priority_hours": FRESH_PRIORITY_MAX_AGE_HOURS,
            "recent_priority_hours": RECENT_PRIORITY_MAX_AGE_HOURS,
            "missing_metrics_rejected": False,
        },
        "research_gate": {
            "min_usable_videos": MIN_USABLE_VIDEO_COUNT,
            "min_low_follower_videos": MIN_LOW_FOLLOWER_VIDEO_COUNT,
            "batch_detail_limit": BATCH_DETAIL_LIMIT,
        },
    }


class TikHubRequestSession:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        request_budget: int,
        client: httpx.AsyncClient,
        on_calls: TikHubCallRecorder | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.request_budget = max(1, int(request_budget))
        self.client = client
        self.on_calls = on_calls
        self.calls: list[TikHubCallRecord] = []
        self._reserved_calls = 0

    def _reserve(self) -> None:
        if self._reserved_calls >= self.request_budget:
            raise ProviderUnavailable(
                f"TikHub 请求预算已用完（上限 {self.request_budget} 次），研究已停止"
            )
        self._reserved_calls += 1

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        request_label: str = "",
    ) -> Any:
        if endpoint not in _VALID_ENDPOINTS:
            raise ValueError("不允许调用未声明的 TikHub 端点")
        self._reserve()
        started = time.perf_counter()
        body: dict[str, Any] = {}
        response_code: int | None = None
        request_id = ""
        cache_message = ""
        data_shape = ""
        request_succeeded = False
        try:
            response = await self.client.request(
                method,
                self.base_url + endpoint,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            try:
                decoded = response.json()
            except ValueError as exc:
                raise ProviderUnavailable("TikHub 返回了无法读取的响应") from exc
            if not isinstance(decoded, dict):
                raise ProviderUnavailable("TikHub 响应不是预期的 JSON 对象")
            body = decoded
            data_shape = _data_shape(body.get("data"))
            raw_code = body.get("code")
            try:
                response_code = int(raw_code) if raw_code is not None else None
            except (TypeError, ValueError):
                response_code = None
            request_id = str(body.get("request_id") or "")[:200]
            cache_message = str(
                body.get("cache_message_zh") or body.get("cache_message") or ""
            )[:300]
            if response.status_code >= 400 or response_code not in (0, 200):
                message = str(
                    body.get("message_zh")
                    or body.get("message")
                    or response.reason_phrase
                    or "未知错误"
                )
                raise ProviderUnavailable(
                    f"TikHub 请求失败（HTTP {response.status_code}）：{message[:400]}"
                    + (f"；request_id={request_id}" if request_id else "")
                )
            request_succeeded = True
            return body.get("data")
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise ProviderUnavailable("TikHub 网络请求失败") from exc
        finally:
            self.calls.append(TikHubCallRecord(
                endpoint=endpoint,
                request_label=str(request_label or "")[:200],
                request_id=request_id,
                response_code=response_code,
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
                cache_message=cache_message,
                data_shape=data_shape,
                succeeded=request_succeeded,
            ))
            if self.on_calls:
                try:
                    await self.on_calls(list(self.calls))
                except TopicCollectionFailed:
                    raise
                except Exception as exc:
                    raise TopicCollectionFailed(
                        "TikHub 调用已经发生，但成本账本无法持久化；研究已停止",
                        self.calls,
                    ) from exc


def _report(
    progress: ProgressReporter | None,
    message: str,
    stage: str,
    percent: int,
) -> None:
    if progress:
        progress({
            "message": message,
            "stage": stage,
            "percent": max(0, min(100, int(percent))),
        })


def _clean_term(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" #\t\r\n")
    return text[:80]


def _family_relevant(value: str) -> bool:
    text = _clean_term(value)
    if not text or any(term in text for term in _EXCLUDED_MATERNAL_TERMS):
        return False
    if any(term in text for term in _DIRECT_FAMILY_TERMS):
        return True
    return sum(term in text for term in _SUPPORTING_FAMILY_TERMS) >= 2


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _direct_value(node: Any, aliases: tuple[str, ...]) -> Any:
    if not isinstance(node, dict):
        return None
    for alias in aliases:
        if alias in node and node[alias] not in (None, ""):
            return node[alias]
    normalized_aliases = {_normalized_key(alias) for alias in aliases}
    for key, value in node.items():
        if _normalized_key(key) in normalized_aliases and value not in (None, ""):
            return value
    return None


def _deep_value(node: Any, aliases: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        direct = _direct_value(node, aliases)
        if direct not in (None, ""):
            return direct
        for child in node.values():
            if isinstance(child, (dict, list)):
                value = _deep_value(child, aliases)
                if value not in (None, ""):
                    return value
    elif isinstance(node, list):
        for child in node:
            value = _deep_value(child, aliases)
            if value not in (None, ""):
                return value
    return None


def _video_nodes(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _direct_value(node, _VIDEO_ID_KEYS) not in (None, ""):
                found.append(node)
                return
            for key, child in node.items():
                if (
                    isinstance(child, dict)
                    and re.fullmatch(r"\d{5,32}", str(key))
                    and _direct_value(child, _VIDEO_ID_KEYS) in (None, "")
                ):
                    walk({"aweme_id": str(key), **child})
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _payload_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, dict):
        return not value or all(
            _payload_is_empty(child) for child in value.values()
        )
    if isinstance(value, list):
        return not value or all(_payload_is_empty(child) for child in value)
    return False


def _data_shape(value: Any, *, depth: int = 0) -> str:
    """只记录字段结构，不保存 TikHub 返回的业务数据。"""

    if isinstance(value, dict):
        keys = sorted(str(key)[:40] for key in value)[:12]
        current = "object{" + ",".join(keys) + "}"
        if depth >= 2:
            return current
        nested = next(
            (
                child
                for child in value.values()
                if isinstance(child, (dict, list)) and child
            ),
            None,
        )
        return (
            f"{current}>{_data_shape(nested, depth=depth + 1)}"
            if nested is not None
            else current
        )[:300]
    if isinstance(value, list):
        current = f"array[{len(value)}]"
        if depth >= 2 or not value:
            return current
        nested = next(
            (child for child in value if isinstance(child, (dict, list))),
            value[0],
        )
        return f"{current}>{_data_shape(nested, depth=depth + 1)}"[:300]
    if value is None:
        return "null"
    return type(value).__name__[:300]


def _number(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, round(value))
    text = str(value).strip().replace(",", "")
    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    elif text.endswith("亿"):
        multiplier, text = 100_000_000, text[:-1]
    elif text.lower().endswith("w"):
        multiplier, text = 10_000, text[:-1]
    elif text.lower().endswith("k"):
        multiplier, text = 1_000, text[:-1]
    try:
        return max(0, round(float(text) * multiplier))
    except ValueError:
        return 0


def _ratio(numerator: int, denominator: int) -> float | None:
    if numerator < 0 or denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _published_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw_text = str(value).strip()
    if re.fullmatch(r"20\d{6}", raw_text):
        try:
            return datetime.strptime(raw_text, "%Y%m%d").replace(
                tzinfo=BEIJING_TZ
            )
        except ValueError:
            return None
    if isinstance(value, (int, float)) or raw_text.isdigit():
        raw = int(value)
        if raw > 10_000_000_000:
            raw //= 1000
        try:
            return datetime.fromtimestamp(raw, timezone.utc).astimezone(
                BEIJING_TZ
            )
        except (OverflowError, OSError, ValueError):
            return None
    text = raw_text
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def _publication_metrics(
    value: Any,
    *,
    play_count: int,
    as_of: datetime | None = None,
) -> tuple[str, float | None, int | None]:
    published = _published_datetime(value)
    if published is None:
        return "", None, None
    collected = as_of or datetime.now(BEIJING_TZ)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=BEIJING_TZ)
    else:
        collected = collected.astimezone(BEIJING_TZ)
    age_hours = (collected - published).total_seconds() / 3600
    if age_hours < 0:
        return "", None, None
    age_hours = round(max(0.0, age_hours), 2)
    effective_days = max(1.0, age_hours / 24)
    average_daily_plays = round(play_count / effective_days)
    return (
        published.isoformat(timespec="seconds"),
        age_hours,
        max(0, average_daily_plays),
    )


def _duration_seconds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration >= 1000:
        duration /= 1000
    return round(max(0.0, duration), 2)


def _evidence_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}|{identity}".encode("utf-8")).hexdigest()
    return f"ev_{digest[:12]}"


def _emerging_low_follower_min_plays(age_hours: float | None) -> int:
    return (
        EMERGING_LOW_FOLLOWER_MIN_PLAYS_FRESH
        if age_hours is not None
        and age_hours <= FRESHEST_BREAKOUT_MAX_AGE_HOURS
        else EMERGING_LOW_FOLLOWER_MIN_PLAYS
    )


def _increment_diagnostic(
    diagnostics: TopicLowFollowerDiagnostics | None,
    field: str,
) -> None:
    if diagnostics is not None:
        setattr(diagnostics, field, int(getattr(diagnostics, field)) + 1)


def _low_follower_diagnostic_summary(
    diagnostics: TopicLowFollowerDiagnostics,
) -> str:
    other_missing_identity = max(
        0,
        diagnostics.rejected_missing_identity_count
        - diagnostics.rejected_missing_video_id_count
        - diagnostics.rejected_missing_title_count,
    )
    labels = (
        ("空结果查询", diagnostics.empty_query_count),
        ("响应结构未识别", diagnostics.unrecognized_query_count),
        ("作品 ID 缺失", diagnostics.rejected_missing_video_id_count),
        ("标题缺失", diagnostics.rejected_missing_title_count),
        ("其他身份字段缺失", other_missing_identity),
        ("作品 ID 异常", diagnostics.rejected_invalid_video_id_count),
        ("偏离家庭教育", diagnostics.rejected_off_topic_count),
        ("历史记录：发布时间异常", diagnostics.rejected_invalid_publish_time_count),
        ("历史记录：超过 72 小时", diagnostics.rejected_too_old_count),
        ("历史记录：粉丝数缺失", diagnostics.rejected_missing_followers_count),
        ("历史记录：粉丝超过 10 万", diagnostics.rejected_follower_ceiling_count),
        ("历史记录：播放不足", diagnostics.rejected_insufficient_plays_count),
        ("历史记录：播粉比不足", diagnostics.rejected_play_follower_ratio_count),
        ("历史记录：赞播比不足", diagnostics.rejected_like_rate_count),
        ("历史记录：深度互动不足", diagnostics.rejected_deep_engagement_rate_count),
    )
    hard_rejections = "、".join(
        f"{label} {count}"
        for label, count in labels
        if count > 0
    ) or "无"
    return (
        f"低粉榜样本：检查 {diagnostics.received_count} 个，"
        f"唯一可用 {diagnostics.unique_qualified_count} 条"
        f"（指标强复核 {diagnostics.strong_qualified_count}、"
        f"指标潜力复核 {diagnostics.emerging_qualified_count}、"
        f"平台榜单待补 {diagnostics.billboard_only_count}、"
        f"批量详情补齐 {diagnostics.detail_enriched_count}、"
        f"跨检索重复 {diagnostics.duplicate_qualified_count}）；"
        f"排序观察：发布时间未补 {diagnostics.missing_publish_time_count}、"
        f"发布超过 72 小时 {diagnostics.older_than_72h_count}、"
        f"粉丝数未补 {diagnostics.missing_follower_metrics_count}；"
        f"硬淘汰：{hard_rejections}"
    )


def _qualified_video_tier(
    metrics: TopicMetrics,
    signal_type: TopicSignalType,
) -> tuple[TopicEvidenceTier, list[str]]:
    """榜单决定是否入池；公开指标只决定证据标签与排序。"""

    deep_rate = metrics.deep_engagement_rate or 0.0
    like_rate = metrics.like_rate or 0.0
    play_follower_ratio = metrics.play_follower_ratio or 0.0
    age_hours = metrics.published_age_hours
    if (
        signal_type == TopicSignalType.LOW_FOLLOWER_VIDEO
        and 0 < metrics.follower_count <= LOW_FOLLOWER_MAX_FOLLOWERS
        and metrics.play_count >= LOW_FOLLOWER_MIN_PLAYS
        and play_follower_ratio >= LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO
        and like_rate >= LOW_FOLLOWER_MIN_LIKE_RATE
        and deep_rate >= BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE
    ):
        return TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT, [
            "TikHub 低粉爆款榜",
            "齐家可见指标强复核（不是入池门槛）",
            f"作者粉丝不超过 {LOW_FOLLOWER_MAX_FOLLOWERS}",
            f"播放不少于 {LOW_FOLLOWER_MIN_PLAYS}",
            f"播粉比不低于 {LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO:g}",
            f"赞播比不低于 {LOW_FOLLOWER_MIN_LIKE_RATE:.0%}",
            f"深度互动率不低于 {BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE:.1%}",
            f"采集时日均播放约 {metrics.average_daily_plays or 0}",
        ]
    if (
        signal_type == TopicSignalType.LOW_FOLLOWER_VIDEO
        and 0 < metrics.follower_count <= EMERGING_LOW_FOLLOWER_MAX_FOLLOWERS
        and metrics.play_count >= _emerging_low_follower_min_plays(age_hours)
        and (
            play_follower_ratio
            >= EMERGING_LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO
        )
        and like_rate >= EMERGING_LOW_FOLLOWER_MIN_LIKE_RATE
        and deep_rate >= EMERGING_LOW_FOLLOWER_MIN_DEEP_ENGAGEMENT_RATE
    ):
        min_plays = _emerging_low_follower_min_plays(age_hours)
        return TopicEvidenceTier.EMERGING_LOW_FOLLOWER_BREAKOUT, [
            "TikHub 低粉爆款榜",
            "齐家可见指标潜力复核（不是入池门槛）",
            f"作者粉丝不超过 {EMERGING_LOW_FOLLOWER_MAX_FOLLOWERS}",
            f"当前时效档播放不少于 {min_plays}",
            f"播粉比不低于 {EMERGING_LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO:g}",
            f"赞播比不低于 {EMERGING_LOW_FOLLOWER_MIN_LIKE_RATE:.0%}",
            "深度互动率不低于 "
            f"{EMERGING_LOW_FOLLOWER_MIN_DEEP_ENGAGEMENT_RATE:.1%}",
        ]
    if (
        signal_type == TopicSignalType.HIGH_LIKE_VIDEO
        and metrics.play_count >= HIGH_HEAT_MIN_PLAYS
        and like_rate >= HIGH_HEAT_MIN_LIKE_RATE
        and deep_rate >= BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE
    ):
        return TopicEvidenceTier.HIGH_HEAT_BREAKOUT, [
            "TikHub 高点赞率榜",
            "齐家可见指标高热复核（不是入池门槛）",
            f"播放不少于 {HIGH_HEAT_MIN_PLAYS}",
            f"赞播比不低于 {HIGH_HEAT_MIN_LIKE_RATE:.0%}",
            f"深度互动率不低于 {BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE:.1%}",
            f"采集时日均播放约 {metrics.average_daily_plays or 0}",
        ]
    if signal_type == TopicSignalType.LOW_FOLLOWER_VIDEO:
        reasons = [
            "TikHub 低粉爆款榜",
            "平台榜单入池；粉丝、播放与互动只用于排序",
        ]
        if metrics.follower_count <= 0:
            reasons.append("作者粉丝指标待批量详情补齐")
        if age_hours is None:
            reasons.append("发布时间待补齐，不作淘汰")
        elif age_hours <= FRESH_PRIORITY_MAX_AGE_HOURS:
            reasons.append("发布 72 小时内，获得新近优先")
        elif age_hours <= RECENT_PRIORITY_MAX_AGE_HOURS:
            reasons.append("发布 7 天内，按近期样本排序")
        else:
            reasons.append("当前榜单中的较早作品，按回潮样本排序")
        return TopicEvidenceTier.LOW_FOLLOWER_BILLBOARD, reasons
    reasons = [
        "TikHub 高点赞率榜",
        "平台榜单入池；播放与互动只用于排序",
    ]
    if age_hours is None:
        reasons.append("发布时间待补齐，不作淘汰")
    elif age_hours <= FRESH_PRIORITY_MAX_AGE_HOURS:
        reasons.append("发布 72 小时内，获得新近优先")
    elif age_hours <= RECENT_PRIORITY_MAX_AGE_HOURS:
        reasons.append("发布 7 天内，按近期样本排序")
    else:
        reasons.append("当前榜单中的较早作品，按回潮样本排序")
    return TopicEvidenceTier.HIGH_LIKE_BILLBOARD, reasons


def _video_evidence(
    node: dict[str, Any],
    *,
    query: str,
    signal_type: TopicSignalType,
    label: str,
    rank: int,
    as_of: datetime | None = None,
    low_follower_diagnostics: TopicLowFollowerDiagnostics | None = None,
    metrics_enriched: bool = False,
) -> TopicEvidence | None:
    video_id = str(_deep_value(node, _VIDEO_ID_KEYS) or "").strip()[:64]
    title = _clean_term(_deep_value(node, _VIDEO_TITLE_KEYS))
    if not video_id:
        _increment_diagnostic(
            low_follower_diagnostics, "rejected_missing_identity_count"
        )
        _increment_diagnostic(
            low_follower_diagnostics, "rejected_missing_video_id_count"
        )
        return None
    if not title:
        _increment_diagnostic(
            low_follower_diagnostics, "rejected_missing_identity_count"
        )
        _increment_diagnostic(
            low_follower_diagnostics, "rejected_missing_title_count"
        )
        return None
    if not _family_relevant(f"{query} {title}"):
        _increment_diagnostic(
            low_follower_diagnostics, "rejected_off_topic_count"
        )
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", video_id):
        _increment_diagnostic(
            low_follower_diagnostics, "rejected_invalid_video_id_count"
        )
        return None
    play_count = _number(
        _deep_value(
            node,
            (
                "play_count",
                "playCount",
                "itemPlayCnt",
                "play_cnt",
                "itemPlayCount",
                "videoPlayCount",
            ),
        )
    )
    like_count = _number(
        _deep_value(
            node,
            (
                "digg_count",
                "diggCount",
                "like_count",
                "like_cnt",
                "likeCount",
                "itemLikeCnt",
                "itemLikeCount",
            ),
        )
    )
    comment_count = _number(
        _deep_value(
            node,
            (
                "comment_count",
                "comment_cnt",
                "commentCount",
                "itemCommentCnt",
                "itemCommentCount",
            ),
        )
    )
    share_count = _number(
        _deep_value(
            node,
            (
                "share_count",
                "share_cnt",
                "shareCount",
                "itemShareCnt",
                "itemShareCount",
            ),
        )
    )
    collect_count = _number(
        _deep_value(
            node,
            (
                "collect_count",
                "collect_cnt",
                "favorite_count",
                "favorite_cnt",
                "collectCount",
                "itemCollectCnt",
                "itemCollectCount",
                "favoriteCount",
                "favouriteCount",
            ),
        )
    )
    follower_count = _number(
        _deep_value(
            node,
            (
                "follower_count",
                "fans_count",
                "authorFollowerCnt",
                "authorFansCnt",
                "author_follower_count",
                "itemAuthorFollowerCnt",
                "itemAuthorFollowerCount",
                "authorFansCount",
                "author_fans",
                "authorFans",
                "fansCount",
            ),
        )
    )
    published_at, published_age_hours, average_daily_plays = _publication_metrics(
        _deep_value(
            node,
            (
                "create_time",
                "createTime",
                "publish_time",
                "publishTime",
                "itemCreateTime",
                "itemPublishTime",
                "itemPublishDate",
                "publishDate",
                "publish_date",
                "releaseTime",
            ),
        ),
        play_count=play_count,
        as_of=as_of,
    )
    # 不信任上游返回的任意外链，统一构造可复核的抖音作品页。
    video_url = f"https://www.douyin.com/video/{video_id}"
    metrics = TopicMetrics(
        play_count=play_count,
        like_count=like_count,
        comment_count=comment_count,
        share_count=share_count,
        collect_count=collect_count,
        follower_count=follower_count,
        like_rate=_ratio(like_count, play_count),
        comment_rate=_ratio(comment_count, play_count),
        share_rate=_ratio(share_count, play_count),
        collect_rate=_ratio(collect_count, play_count),
        deep_engagement_rate=_ratio(
            comment_count + share_count + collect_count,
            play_count,
        ),
        play_follower_ratio=_ratio(play_count, follower_count),
        published_age_hours=published_age_hours,
        average_daily_plays=average_daily_plays,
    )
    qualification = _qualified_video_tier(metrics, signal_type)
    quality_tier, qualification_reasons = qualification
    return TopicEvidence(
        id=_evidence_id("video", video_id),
        evidence_type=TopicEvidenceType.VIDEO,
        signal_types=[signal_type],
        queries=[query],
        title=title,
        platform_labels=[label],
        quality_tier=quality_tier,
        qualification_reasons=qualification_reasons,
        source_rank=rank,
        video_id=video_id,
        video_url=video_url,
        author_name=str(
            _deep_value(
                node,
                (
                    "nickname",
                    "nickName",
                    "authorNickName",
                    "author_name",
                    "authorName",
                ),
            )
            or ""
        )[:200],
        published_at=published_at,
        duration_seconds=_duration_seconds(
            _deep_value(
                node,
                (
                    "duration",
                    "itemDuration",
                    "durationSeconds",
                    "duration_seconds",
                    "videoDuration",
                ),
            )
        ),
        metrics=metrics,
        metrics_enriched=metrics_enriched,
    )


def _merge_detail_snapshot(
    current: TopicEvidence,
    detail: TopicEvidence,
) -> TopicEvidence:
    """把批量详情的非零累计指标合入榜单快照，再重新计算比率。"""

    merged = current.model_copy(deep=True)
    current_metrics = current.metrics or TopicMetrics()
    detail_metrics = detail.metrics or TopicMetrics()
    play_count = max(current_metrics.play_count, detail_metrics.play_count)
    like_count = max(current_metrics.like_count, detail_metrics.like_count)
    comment_count = max(
        current_metrics.comment_count, detail_metrics.comment_count
    )
    share_count = max(current_metrics.share_count, detail_metrics.share_count)
    collect_count = max(
        current_metrics.collect_count, detail_metrics.collect_count
    )
    follower_count = (
        detail_metrics.follower_count or current_metrics.follower_count
    )
    age_hours = (
        detail_metrics.published_age_hours
        if detail_metrics.published_age_hours is not None
        else current_metrics.published_age_hours
    )
    average_daily_plays = (
        round(play_count / max(1.0, age_hours / 24))
        if age_hours is not None
        else None
    )
    merged.metrics = TopicMetrics(
        play_count=play_count,
        like_count=like_count,
        comment_count=comment_count,
        share_count=share_count,
        collect_count=collect_count,
        follower_count=follower_count,
        like_rate=_ratio(like_count, play_count),
        comment_rate=_ratio(comment_count, play_count),
        share_rate=_ratio(share_count, play_count),
        collect_rate=_ratio(collect_count, play_count),
        deep_engagement_rate=_ratio(
            comment_count + share_count + collect_count,
            play_count,
        ),
        play_follower_ratio=_ratio(play_count, follower_count),
        published_age_hours=age_hours,
        average_daily_plays=average_daily_plays,
    )
    merged.metrics_enriched = True
    if detail.published_at:
        merged.published_at = detail.published_at
    if detail.author_name:
        merged.author_name = detail.author_name
    if detail.duration_seconds is not None:
        merged.duration_seconds = detail.duration_seconds
    if len(detail.title) > len(merged.title):
        merged.title = detail.title
    primary_signal = (
        TopicSignalType.LOW_FOLLOWER_VIDEO
        if TopicSignalType.LOW_FOLLOWER_VIDEO in merged.signal_types
        else TopicSignalType.HIGH_LIKE_VIDEO
    )
    merged.quality_tier, merged.qualification_reasons = _qualified_video_tier(
        merged.metrics,
        primary_signal,
    )
    return merged


def _enrich_video_evidence(
    items: list[TopicEvidence],
    data: Any,
    *,
    as_of: datetime,
) -> tuple[list[TopicEvidence], int]:
    """用一次批量详情调用回填榜单视频；结构缺失时保留原榜单样本。"""

    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node in _video_nodes(data):
        video_id = str(_deep_value(node, _VIDEO_ID_KEYS) or "").strip()[:64]
        if video_id and video_id not in nodes_by_id:
            nodes_by_id[video_id] = node
    enriched: list[TopicEvidence] = []
    enriched_count = 0
    for item in items:
        node = nodes_by_id.get(item.video_id)
        if node is None:
            enriched.append(item)
            continue
        primary_signal = (
            TopicSignalType.LOW_FOLLOWER_VIDEO
            if TopicSignalType.LOW_FOLLOWER_VIDEO in item.signal_types
            else TopicSignalType.HIGH_LIKE_VIDEO
        )
        detail_node = node
        if not _clean_term(_deep_value(node, _VIDEO_TITLE_KEYS)):
            detail_node = {**node, "desc": item.title}
        detail = _video_evidence(
            detail_node,
            query=item.queries[0] if item.queries else "家庭教育",
            signal_type=primary_signal,
            label=item.platform_labels[0] if item.platform_labels else "TikHub 榜单",
            rank=item.source_rank,
            as_of=as_of,
            metrics_enriched=True,
        )
        if detail is None:
            enriched.append(item)
            continue
        enriched.append(_merge_detail_snapshot(item, detail))
        enriched_count += 1
    return enriched, enriched_count


def _merge_evidence(items: list[TopicEvidence]) -> list[TopicEvidence]:
    tier_priority = {
        TopicEvidenceTier.UNASSESSED: 0,
        TopicEvidenceTier.TREND_SIGNAL: 1,
        TopicEvidenceTier.HIGH_LIKE_BILLBOARD: 2,
        TopicEvidenceTier.HIGH_HEAT_BREAKOUT: 3,
        TopicEvidenceTier.LOW_FOLLOWER_BILLBOARD: 4,
        TopicEvidenceTier.EMERGING_LOW_FOLLOWER_BREAKOUT: 5,
        TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT: 6,
    }
    merged: dict[str, TopicEvidence] = {}
    for item in items:
        current = merged.get(item.id)
        if current is None:
            merged[item.id] = item.model_copy(deep=True)
            continue
        current.signal_types = list(dict.fromkeys([
            *current.signal_types,
            *item.signal_types,
        ]))
        current.queries = list(dict.fromkeys([*current.queries, *item.queries]))[:8]
        current.platform_labels = list(dict.fromkeys([
            *current.platform_labels,
            *item.platform_labels,
        ]))[:8]
        current.qualification_reasons = list(dict.fromkeys([
            *current.qualification_reasons,
            *item.qualification_reasons,
        ]))[:8]
        if tier_priority[item.quality_tier] > tier_priority[current.quality_tier]:
            current.quality_tier = item.quality_tier
        positive_ranks = [value for value in (current.source_rank, item.source_rank) if value]
        current.source_rank = min(positive_ranks) if positive_ranks else 0
        if (
            item.metrics
            and (
                not current.metrics
                or item.metrics.play_count > current.metrics.play_count
            )
        ):
            current.metrics = item.metrics
        if len(item.title) > len(current.title):
            current.title = item.title
        current.metrics_enriched = current.metrics_enriched or item.metrics_enriched
    return list(merged.values())


def _video_evidence_list(
    data: Any,
    *,
    query: str,
    signal_type: TopicSignalType,
    label: str,
    limit: int = 3,
    as_of: datetime | None = None,
    low_follower_diagnostics: TopicLowFollowerDiagnostics | None = None,
) -> list[TopicEvidence]:
    evidence: list[TopicEvidence] = []
    nodes = _video_nodes(data)
    if signal_type == TopicSignalType.LOW_FOLLOWER_VIDEO and not nodes:
        _increment_diagnostic(
            low_follower_diagnostics,
            "empty_or_unrecognized_query_count",
        )
        _increment_diagnostic(
            low_follower_diagnostics,
            (
                "empty_query_count"
                if _payload_is_empty(data)
                else "unrecognized_query_count"
            ),
        )
    for rank, node in enumerate(nodes, start=1):
        if signal_type == TopicSignalType.LOW_FOLLOWER_VIDEO:
            _increment_diagnostic(
                low_follower_diagnostics, "received_count"
            )
        item = _video_evidence(
            node,
            query=query,
            signal_type=signal_type,
            label=label,
            rank=rank,
            as_of=as_of,
            low_follower_diagnostics=low_follower_diagnostics,
        )
        if item:
            evidence.append(item)
        if len(evidence) >= limit:
            break
    return evidence


class TikHubDouyinResearchProvider:
    name = "tikhub-douyin"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        request_budget: int = PLANNED_MAX_TIKHUB_CALLS,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 45.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://api.tikhub.dev").strip().rstrip("/")
        self.request_budget = max(
            1, min(MAX_TIKHUB_REQUEST_BUDGET, int(request_budget))
        )
        self.transport = transport
        self.timeout_seconds = max(10.0, float(timeout_seconds))

    @property
    def configured(self) -> bool:
        return not self.configuration_errors

    @property
    def configuration_errors(self) -> list[str]:
        parsed = urlparse(self.base_url)
        errors: list[str] = []
        if not self.api_key:
            errors.append("TIKHUB_API_KEY")
        if not (
            parsed.scheme == "https"
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        ):
            errors.append("TIKHUB_BASE_URL（必须是无凭据、无查询参数的 HTTPS 地址）")
        if self.request_budget < PLANNED_MAX_TIKHUB_CALLS:
            errors.append(
                f"QIJIA_TOPIC_TIKHUB_REQUEST_BUDGET（至少 {PLANNED_MAX_TIKHUB_CALLS}）"
            )
        return errors

    async def collect_family_education(
        self,
        progress: ProgressReporter | None = None,
        on_calls: TikHubCallRecorder | None = None,
    ) -> TopicResearchCollection:
        if not self.configured:
            raise ProviderUnavailable(
                "抖音选题研究未配置：" + "、".join(self.configuration_errors)
            )
        warnings: list[str] = []
        evidence: list[TopicEvidence] = []
        low_follower_diagnostics = TopicLowFollowerDiagnostics()
        timeout = httpx.Timeout(self.timeout_seconds, connect=15.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            session = TikHubRequestSession(
                api_key=self.api_key,
                base_url=self.base_url,
                request_budget=self.request_budget,
                client=client,
                on_calls=on_calls,
            )
            collected_at = datetime.now(BEIJING_TZ)
            valid_through = collected_at.date().isoformat()

            async def optional_call(label: str, coroutine):
                try:
                    return await coroutine
                except TopicCollectionFailed:
                    raise
                except ProviderUnavailable as exc:
                    warnings.append(f"{label}：{exc}")
                    return None

            _report(
                progress,
                "读取六类家庭教育低粉爆款榜样本…",
                "topic_samples",
                18,
            )
            sample_semaphore = asyncio.Semaphore(4)

            async def labelled_videos(
                query: str,
                *,
                endpoint: str,
                date_window: int,
                signal_type: TopicSignalType,
                platform_label: str,
            ) -> list[TopicEvidence]:
                async with sample_semaphore:
                    data = await optional_call(
                        f"“{query}”{platform_label}视频不可用",
                        session.request(
                            "POST",
                            endpoint,
                            json_body={
                                "page": 1,
                                "page_size": BILLBOARD_PAGE_SIZE,
                                "date_window": date_window,
                                "keyword": query,
                                # 受控家庭教育关键词比母婴大类更精确；空标签避免
                                # 把母婴商品内容误当成家庭教育证据。
                                "tags": [],
                            },
                            request_label=f"{query} · {platform_label}",
                        ),
                    )
                    response_as_of = datetime.now(BEIJING_TZ)
                try:
                    return _video_evidence_list(
                        data,
                        query=query,
                        signal_type=signal_type,
                        label=platform_label,
                        limit=4,
                        as_of=response_as_of,
                        low_follower_diagnostics=(
                            low_follower_diagnostics
                            if signal_type
                            == TopicSignalType.LOW_FOLLOWER_VIDEO
                            else None
                        ),
                    )
                except Exception as exc:
                    warnings.append(
                        f"“{query}”{platform_label}结构无法识别：{type(exc).__name__}"
                    )
                    return []

            low_follower_groups = await asyncio.gather(*(
                labelled_videos(
                    query,
                    endpoint=(
                        "/api/v1/douyin/billboard/"
                        "fetch_hot_total_low_fan_list"
                    ),
                    date_window=72,
                    signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
                    platform_label="TikHub 72 小时窗口低粉爆款榜",
                )
                for query in DEFAULT_FAMILY_EDUCATION_QUERIES
            ))
            raw_low_follower_evidence = [
                item
                for group in low_follower_groups
                for item in group
            ]
            evidence.extend(raw_low_follower_evidence)
            low_follower_ids = {item.id for item in raw_low_follower_evidence}
            low_follower_diagnostics.unique_qualified_count = len(
                low_follower_ids
            )
            low_follower_diagnostics.billboard_only_count = len(
                low_follower_ids
            )
            low_follower_diagnostics.duplicate_qualified_count = max(
                0,
                len(raw_low_follower_evidence) - len(low_follower_ids),
            )
            if len(low_follower_ids) < MIN_LOW_FOLLOWER_VIDEO_COUNT:
                raise TopicCollectionFailed(
                    f"只有 {len(low_follower_ids)} 条有效家庭教育低粉榜视频，"
                    f"少于最低要求 {MIN_LOW_FOLLOWER_VIDEO_COUNT} 条；"
                    f"{_low_follower_diagnostic_summary(low_follower_diagnostics)}；"
                    "本轮已在高点赞榜查询前停止，以减少无效 API 成本",
                    session.calls,
                    low_follower_diagnostics,
                    warnings,
                )

            _report(
                progress,
                "低粉榜样本已达门槛，补充高点赞榜交叉证据…",
                "topic_samples",
                42,
            )
            high_heat_groups = await asyncio.gather(*(
                labelled_videos(
                    query,
                    endpoint=(
                        "/api/v1/douyin/billboard/"
                        "fetch_hot_total_high_like_list"
                    ),
                    date_window=168,
                    signal_type=TopicSignalType.HIGH_LIKE_VIDEO,
                    platform_label="TikHub 168 小时窗口高点赞率榜",
                )
                for query in DEFAULT_FAMILY_EDUCATION_QUERIES
            ))
            for group in high_heat_groups:
                evidence.extend(group)

            _report(
                progress,
                "批量补齐粉丝、播放与互动指标…",
                "topic_enrichment",
                62,
            )
            try:
                normalized = _merge_evidence(evidence)
            except Exception as exc:
                raise TopicCollectionFailed(
                    f"TikHub 数据结构无法安全归一化：{type(exc).__name__}",
                    session.calls,
                    low_follower_diagnostics,
                    warnings,
                ) from exc
            video_ids = [
                item.video_id
                for item in normalized
                if item.evidence_type == TopicEvidenceType.VIDEO
            ][:BATCH_DETAIL_LIMIT]
            detail_data = None
            if video_ids:
                detail_data = await optional_call(
                    "榜单视频批量详情不可用，保留平台榜单指标",
                    session.request(
                        "POST",
                        "/api/v1/douyin/web/fetch_multi_video",
                        json_body=video_ids,
                        request_label=f"批量补齐 {len(video_ids)} 条榜单视频详情",
                    ),
                )
            if detail_data is not None:
                normalized, enriched_count = _enrich_video_evidence(
                    normalized,
                    detail_data,
                    as_of=datetime.now(BEIJING_TZ),
                )
                if enriched_count == 0:
                    warnings.append("批量详情返回结构未匹配作品 ID，已保留榜单样本")

        usable_tiers = {
            TopicEvidenceTier.LOW_FOLLOWER_BILLBOARD,
            TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
            TopicEvidenceTier.EMERGING_LOW_FOLLOWER_BREAKOUT,
            TopicEvidenceTier.HIGH_LIKE_BILLBOARD,
            TopicEvidenceTier.HIGH_HEAT_BREAKOUT,
        }
        usable_videos = [
            item
            for item in normalized
            if item.evidence_type == TopicEvidenceType.VIDEO
            and item.quality_tier in usable_tiers
        ]
        low_follower_videos = [
            item
            for item in usable_videos
            if TopicSignalType.LOW_FOLLOWER_VIDEO in item.signal_types
        ]
        low_follower_diagnostics.unique_qualified_count = len(
            low_follower_videos
        )
        low_follower_diagnostics.strong_qualified_count = sum(
            item.quality_tier == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
            for item in low_follower_videos
        )
        low_follower_diagnostics.emerging_qualified_count = sum(
            item.quality_tier
            == TopicEvidenceTier.EMERGING_LOW_FOLLOWER_BREAKOUT
            for item in low_follower_videos
        )
        low_follower_diagnostics.billboard_only_count = sum(
            item.quality_tier == TopicEvidenceTier.LOW_FOLLOWER_BILLBOARD
            for item in low_follower_videos
        )
        low_follower_diagnostics.detail_enriched_count = sum(
            item.metrics_enriched for item in low_follower_videos
        )
        low_follower_diagnostics.missing_publish_time_count = sum(
            not item.metrics
            or item.metrics.published_age_hours is None
            for item in low_follower_videos
        )
        low_follower_diagnostics.older_than_72h_count = sum(
            bool(
                item.metrics
                and item.metrics.published_age_hours is not None
                and item.metrics.published_age_hours
                > FRESH_PRIORITY_MAX_AGE_HOURS
            )
            for item in low_follower_videos
        )
        low_follower_diagnostics.missing_follower_metrics_count = sum(
            not item.metrics or item.metrics.follower_count <= 0
            for item in low_follower_videos
        )
        if (
            len(usable_videos) < MIN_USABLE_VIDEO_COUNT
            or len(low_follower_videos) < MIN_LOW_FOLLOWER_VIDEO_COUNT
        ):
            raise TopicCollectionFailed(
                "可用的家庭教育榜单视频不足："
                f"当前 {len(usable_videos)} 条、其中 {len(low_follower_videos)} 条来自低粉榜；"
                f"至少需要 {MIN_USABLE_VIDEO_COUNT} 条榜单视频且包含 "
                f"{MIN_LOW_FOLLOWER_VIDEO_COUNT} 条低粉榜视频。"
                f"{_low_follower_diagnostic_summary(low_follower_diagnostics)}；"
                "本轮已停止，不会用普通搜索或泛母婴内容凑数",
                session.calls,
                low_follower_diagnostics,
                warnings,
            )
        _report(progress, "抖音样本已整理，准备编辑判断…", "topic_normalize", 72)
        return TopicResearchCollection(
            valid_through=valid_through,
            evidence=normalized[:80],
            calls=list(session.calls),
            warnings=warnings,
            low_follower_diagnostics=low_follower_diagnostics,
        )
