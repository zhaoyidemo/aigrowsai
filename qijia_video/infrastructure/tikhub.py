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
    TopicMetrics,
    TopicSignalType,
)


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
# TikHub 当前筛选表将 617 定义为“母婴”。家庭教育只是其中的子集，
# 所以后续仍会再做关键词相关性过滤，绝不把整个母婴垂类都当作候选。
TIKHUB_PARENTING_TAG_ID = "617"
PLANNED_MAX_TIKHUB_CALLS = 15
MAX_TIKHUB_REQUEST_BUDGET = 100

# TikHub 没有公开“低粉爆款”的内部阈值。下面是齐家自己的二次复核门槛，
# 只把同时通过平台精选标签和可见指标校验的视频交给选题编辑模型。
LOW_FOLLOWER_MAX_FOLLOWERS = 50_000
LOW_FOLLOWER_MIN_PLAYS = 500_000
LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO = 20.0
LOW_FOLLOWER_MIN_LIKE_RATE = 0.05
BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE = 0.008
FRESHEST_BREAKOUT_MAX_AGE_HOURS = 24
LOW_FOLLOWER_MAX_AGE_HOURS = 72
HIGH_HEAT_MIN_PLAYS = 1_000_000
HIGH_HEAT_MIN_LIKE_RATE = 0.08
HIGH_HEAT_MAX_AGE_HOURS = 168
MIN_QUALIFIED_VIDEO_COUNT = 10
MIN_LOW_FOLLOWER_VIDEO_COUNT = 5
DEFAULT_FAMILY_EDUCATION_QUERIES = (
    "亲子沟通",
    "孩子情绪",
    "学习习惯",
    "规则边界 孩子",
    "手机沉迷 孩子",
    "青春期 亲子",
)

_VALID_ENDPOINTS = {
    "/api/v1/douyin/index/fetch_content_valid_date",
    "/api/v1/douyin/index/fetch_content_creative_keywords",
    "/api/v1/douyin/index/fetch_content_creative_topic",
    "/api/v1/douyin/index/fetch_item_query",
}
_TERM_KEYS = (
    "keyword",
    "word",
    "topic_name",
    "tag_name",
    "name",
    "title",
    "content",
    "text",
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


def evidence_quality_policy() -> dict[str, dict[str, float | int]]:
    """返回给前端展示的同一份确定性门槛，避免文案和后端规则漂移。"""

    return {
        "low_follower_breakout": {
            "max_followers": LOW_FOLLOWER_MAX_FOLLOWERS,
            "min_plays": LOW_FOLLOWER_MIN_PLAYS,
            "min_play_follower_ratio": LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO,
            "min_like_rate": LOW_FOLLOWER_MIN_LIKE_RATE,
            "min_deep_engagement_rate": BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE,
            "max_age_hours": LOW_FOLLOWER_MAX_AGE_HOURS,
            "freshest_age_hours": FRESHEST_BREAKOUT_MAX_AGE_HOURS,
        },
        "high_heat_breakout": {
            "min_plays": HIGH_HEAT_MIN_PLAYS,
            "min_like_rate": HIGH_HEAT_MIN_LIKE_RATE,
            "min_deep_engagement_rate": BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE,
            "max_age_hours": HIGH_HEAT_MAX_AGE_HOURS,
            "freshest_age_hours": FRESHEST_BREAKOUT_MAX_AGE_HOURS,
        },
        "research_gate": {
            "min_qualified_videos": MIN_QUALIFIED_VIDEO_COUNT,
            "min_low_follower_videos": MIN_LOW_FOLLOWER_VIDEO_COUNT,
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
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if endpoint not in _VALID_ENDPOINTS:
            raise ValueError("不允许调用未声明的 TikHub 端点")
        self._reserve()
        started = time.perf_counter()
        body: dict[str, Any] = {}
        response_code: int | None = None
        request_id = ""
        cache_message = ""
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
                request_id=request_id,
                response_code=response_code,
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
                cache_message=cache_message,
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


def _extract_terms(value: Any, *, limit: int = 16) -> list[str]:
    found: list[str] = []

    def add(candidate: Any) -> None:
        text = _clean_term(candidate)
        if 2 <= len(text) <= 80 and text not in found and _family_relevant(text):
            found.append(text)

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            matched = False
            for key in _TERM_KEYS:
                if key in node and isinstance(node[key], str):
                    add(node[key])
                    matched = True
                    break
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child)
            if not matched and len(node) == 1:
                walk(next(iter(node.values())))
            return
        if isinstance(node, str):
            add(node)

    walk(value)
    return found[:limit]


def _walk_dates(value: Any) -> list[datetime]:
    dates: list[datetime] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, (str, int)):
            text = str(node)
            for match in re.findall(r"20\d{2}(?:[-/]?\d{2}){2}", text):
                normalized = re.sub(r"[-/]", "", match)
                try:
                    dates.append(datetime.strptime(normalized, "%Y%m%d"))
                except ValueError:
                    continue

    walk(value)
    return dates


def _latest_valid_date(value: Any) -> tuple[str, str]:
    dates = _walk_dates(value)
    if not dates:
        raise ProviderUnavailable(
            "TikHub 创作指南有效日期缺少可识别字段；为避免伪造数据日期，研究已停止"
        )
    latest = max(dates)
    return latest.strftime("%Y%m%d"), latest.date().isoformat()


def _deep_value(node: Any, aliases: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        for alias in aliases:
            if alias in node and node[alias] not in (None, ""):
                return node[alias]
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
            if any(key in node for key in ("aweme_id", "itemId", "item_id")):
                found.append(node)
                return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


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
    if isinstance(value, (int, float)) or str(value).isdigit():
        raw = int(value)
        if raw > 10_000_000_000:
            raw //= 1000
        try:
            return datetime.fromtimestamp(raw, timezone.utc).astimezone(
                BEIJING_TZ
            )
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
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


def _term_evidence(
    term: str,
    *,
    signal_type: TopicSignalType,
    label: str,
    rank: int,
) -> TopicEvidence:
    return TopicEvidence(
        id=_evidence_id(signal_type.value, term),
        evidence_type=TopicEvidenceType.TREND_TERM,
        signal_types=[signal_type],
        queries=[term],
        title=term,
        platform_labels=[label],
        quality_tier=TopicEvidenceTier.TREND_SIGNAL,
        qualification_reasons=["家庭教育相关的近期抖音创作趋势"],
        source_rank=rank,
    )


def _qualified_video_tier(
    metrics: TopicMetrics,
    signal_type: TopicSignalType,
) -> tuple[TopicEvidenceTier, list[str]] | None:
    """以平台精选标签为入口，再用公开可见指标做严格的本地复核。"""

    deep_rate = metrics.deep_engagement_rate or 0.0
    like_rate = metrics.like_rate or 0.0
    play_follower_ratio = metrics.play_follower_ratio or 0.0
    age_hours = metrics.published_age_hours
    if (
        signal_type == TopicSignalType.LOW_FOLLOWER_VIDEO
        and age_hours is not None
        and age_hours <= LOW_FOLLOWER_MAX_AGE_HOURS
        and 0 < metrics.follower_count <= LOW_FOLLOWER_MAX_FOLLOWERS
        and metrics.play_count >= LOW_FOLLOWER_MIN_PLAYS
        and play_follower_ratio >= LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO
        and like_rate >= LOW_FOLLOWER_MIN_LIKE_RATE
        and deep_rate >= BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE
    ):
        return TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT, [
            "TikHub 低粉爆款精选标签",
            f"发布不超过 {LOW_FOLLOWER_MAX_AGE_HOURS} 小时",
            f"作者粉丝不超过 {LOW_FOLLOWER_MAX_FOLLOWERS}",
            f"播放不少于 {LOW_FOLLOWER_MIN_PLAYS}",
            f"播粉比不低于 {LOW_FOLLOWER_MIN_PLAY_FOLLOWER_RATIO:g}",
            f"赞播比不低于 {LOW_FOLLOWER_MIN_LIKE_RATE:.0%}",
            f"深度互动率不低于 {BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE:.1%}",
            f"采集时日均播放约 {metrics.average_daily_plays or 0}",
        ]
    if (
        signal_type == TopicSignalType.HIGH_LIKE_VIDEO
        and age_hours is not None
        and age_hours <= HIGH_HEAT_MAX_AGE_HOURS
        and metrics.play_count >= HIGH_HEAT_MIN_PLAYS
        and like_rate >= HIGH_HEAT_MIN_LIKE_RATE
        and deep_rate >= BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE
    ):
        return TopicEvidenceTier.HIGH_HEAT_BREAKOUT, [
            "TikHub 高点赞率精选标签",
            f"发布不超过 {HIGH_HEAT_MAX_AGE_HOURS} 小时",
            f"播放不少于 {HIGH_HEAT_MIN_PLAYS}",
            f"赞播比不低于 {HIGH_HEAT_MIN_LIKE_RATE:.0%}",
            f"深度互动率不低于 {BREAKOUT_MIN_DEEP_ENGAGEMENT_RATE:.1%}",
            f"采集时日均播放约 {metrics.average_daily_plays or 0}",
        ]
    return None


def _video_evidence(
    node: dict[str, Any],
    *,
    query: str,
    signal_type: TopicSignalType,
    label: str,
    rank: int,
    as_of: datetime | None = None,
) -> TopicEvidence | None:
    video_id = str(
        _deep_value(node, ("aweme_id", "itemId", "item_id")) or ""
    ).strip()[:64]
    title = _clean_term(
        _deep_value(node, ("desc", "itemTitle", "item_title", "caption"))
    )
    if not video_id or not title:
        return None
    if not _family_relevant(title):
        return None
    play_count = _number(
        _deep_value(node, ("play_count", "playCount", "itemPlayCnt", "play_cnt"))
    )
    like_count = _number(
        _deep_value(node, ("digg_count", "like_count", "likeCount", "itemLikeCnt"))
    )
    comment_count = _number(
        _deep_value(node, ("comment_count", "commentCount", "itemCommentCnt"))
    )
    share_count = _number(
        _deep_value(node, ("share_count", "shareCount", "itemShareCnt"))
    )
    collect_count = _number(
        _deep_value(node, ("collect_count", "collectCount", "itemCollectCnt"))
    )
    follower_count = _number(
        _deep_value(
            node,
            ("follower_count", "fans_count", "authorFollowerCnt", "authorFansCnt"),
        )
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", video_id):
        return None
    published_at, published_age_hours, average_daily_plays = _publication_metrics(
        _deep_value(node, ("create_time", "publishTime", "itemCreateTime")),
        play_count=play_count,
        as_of=as_of,
    )
    if published_age_hours is None:
        return None
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
    if qualification is None:
        return None
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
            _deep_value(node, ("nickname", "authorNickName", "author_name")) or ""
        )[:200],
        published_at=published_at,
        duration_seconds=_duration_seconds(
            _deep_value(node, ("duration", "itemDuration"))
        ),
        metrics=metrics,
    )


def _merge_evidence(items: list[TopicEvidence]) -> list[TopicEvidence]:
    tier_priority = {
        TopicEvidenceTier.UNASSESSED: 0,
        TopicEvidenceTier.TREND_SIGNAL: 1,
        TopicEvidenceTier.HIGH_HEAT_BREAKOUT: 2,
        TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT: 3,
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
    return list(merged.values())


def _video_evidence_list(
    data: Any,
    *,
    query: str,
    signal_type: TopicSignalType,
    label: str,
    limit: int = 3,
    as_of: datetime | None = None,
) -> list[TopicEvidence]:
    evidence: list[TopicEvidence] = []
    for rank, node in enumerate(_video_nodes(data), start=1):
        item = _video_evidence(
            node,
            query=query,
            signal_type=signal_type,
            label=label,
            rank=rank,
            as_of=as_of,
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
            _report(progress, "读取抖音创作指南的数据日期…", "topic_date", 8)
            try:
                valid_data = await session.request(
                    "GET", "/api/v1/douyin/index/fetch_content_valid_date"
                )
                end_date, valid_through = _latest_valid_date(valid_data)
                valid_date = datetime.strptime(valid_through, "%Y-%m-%d").date()
                today = datetime.now(BEIJING_TZ).date()
                if (valid_date - today).days > 1:
                    raise ProviderUnavailable(
                        "TikHub 创作指南返回了未来数据日期；为避免使用错误窗口，研究已停止"
                    )
            except TopicCollectionFailed:
                raise
            except ProviderUnavailable as exc:
                raise TopicCollectionFailed(
                    str(exc), session.calls
                ) from exc
            age_days = (today - valid_date).days
            if age_days > 14:
                warnings.append(
                    f"TikHub 创作指南数据已滞后 {age_days} 天，候选只宜作为补充线索"
                )

            async def optional_call(label: str, coroutine):
                try:
                    return await coroutine
                except TopicCollectionFailed:
                    raise
                except ProviderUnavailable as exc:
                    warnings.append(f"{label}：{exc}")
                    return None

            _report(progress, "发现母婴垂类中的家庭教育趋势…", "topic_discovery", 18)
            keyword_data, topic_data = await asyncio.gather(
                optional_call(
                    "创作热门关键词不可用",
                    session.request(
                        "POST",
                        "/api/v1/douyin/index/fetch_content_creative_keywords",
                        params={
                            "tag_id": TIKHUB_PARENTING_TAG_ID,
                            "period": "3",
                            "end_date": end_date,
                        },
                    ),
                ),
                optional_call(
                    "创作飙升话题不可用",
                    session.request(
                        "POST",
                        "/api/v1/douyin/index/fetch_content_creative_topic",
                        params={
                            "tag_id": TIKHUB_PARENTING_TAG_ID,
                            "period": "3",
                            "end_date": end_date,
                            "rank_type": "rise",
                        },
                    ),
                ),
            )
            keywords = _extract_terms(keyword_data, limit=12)
            topics = _extract_terms(topic_data, limit=12)
            evidence.extend(
                _term_evidence(
                    term,
                    signal_type=TopicSignalType.CREATIVE_KEYWORD,
                    label="母婴垂类近 3 天创作热词",
                    rank=index,
                )
                for index, term in enumerate(keywords, start=1)
            )
            evidence.extend(
                _term_evidence(
                    term,
                    signal_type=TopicSignalType.RISING_TOPIC,
                    label="母婴垂类近 3 天飙升话题",
                    rank=index,
                )
                for index, term in enumerate(topics, start=1)
            )

            _report(
                progress,
                "筛选六类家庭教育低粉爆款与高热样本…",
                "topic_samples",
                36,
            )
            sample_semaphore = asyncio.Semaphore(4)

            async def labelled_videos(
                query: str,
                *,
                label_type: int,
                date_type: int,
                signal_type: TopicSignalType,
                platform_label: str,
            ) -> list[TopicEvidence]:
                async with sample_semaphore:
                    data = await optional_call(
                        f"“{query}”{platform_label}视频不可用",
                        session.request(
                            "POST",
                            "/api/v1/douyin/index/fetch_item_query",
                            params={
                                "query": query,
                                "category_id": TIKHUB_PARENTING_TAG_ID,
                                "date_type": date_type,
                                "label_type": label_type,
                                "duration_type": 6,
                            },
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
                    )
                except Exception as exc:
                    warnings.append(
                        f"“{query}”{platform_label}结构无法识别：{type(exc).__name__}"
                    )
                    return []

            low_follower_groups = await asyncio.gather(*(
                labelled_videos(
                    query,
                    label_type=1,
                    date_type=3,
                    signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
                    platform_label="TikHub 近 3 天低粉爆款标签",
                )
                for query in DEFAULT_FAMILY_EDUCATION_QUERIES
            ))
            for group in low_follower_groups:
                evidence.extend(group)
            low_follower_ids = {
                item.id
                for item in evidence
                if item.quality_tier
                == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
            }
            if len(low_follower_ids) < MIN_LOW_FOLLOWER_VIDEO_COUNT:
                raise TopicCollectionFailed(
                    f"严格复核后只有 {len(low_follower_ids)} 条低粉爆款，"
                    f"少于最低要求 {MIN_LOW_FOLLOWER_VIDEO_COUNT} 条；"
                    "本轮已在高热补充查询前停止，以减少无效 API 成本",
                    session.calls,
                )

            _report(
                progress,
                "低粉爆款已达门槛，补充高热交叉证据…",
                "topic_samples",
                52,
            )
            high_heat_groups = await asyncio.gather(*(
                labelled_videos(
                    query,
                    label_type=4,
                    date_type=7,
                    signal_type=TopicSignalType.HIGH_LIKE_VIDEO,
                    platform_label="TikHub 近 7 天高点赞率标签",
                )
                for query in DEFAULT_FAMILY_EDUCATION_QUERIES
            ))
            for group in high_heat_groups:
                evidence.extend(group)

            _report(
                progress,
                "正在复核播放、粉丝与互动门槛…",
                "topic_enrichment",
                62,
            )

        try:
            normalized = _merge_evidence(evidence)
        except Exception as exc:
            raise TopicCollectionFailed(
                f"TikHub 数据结构无法安全归一化：{type(exc).__name__}",
                session.calls,
            ) from exc
        qualified_videos = [
            item
            for item in normalized
            if item.evidence_type == TopicEvidenceType.VIDEO
            and item.quality_tier in {
                TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
                TopicEvidenceTier.HIGH_HEAT_BREAKOUT,
            }
        ]
        low_follower_count = sum(
            item.quality_tier == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
            for item in qualified_videos
        )
        if (
            len(qualified_videos) < MIN_QUALIFIED_VIDEO_COUNT
            or low_follower_count < MIN_LOW_FOLLOWER_VIDEO_COUNT
        ):
            raise TopicCollectionFailed(
                "通过爆款复核的家庭教育视频不足："
                f"当前 {len(qualified_videos)} 条合格、其中 {low_follower_count} 条低粉爆款；"
                f"至少需要 {MIN_QUALIFIED_VIDEO_COUNT} 条合格视频且包含 "
                f"{MIN_LOW_FOLLOWER_VIDEO_COUNT} 条低粉爆款。"
                "本轮已停止，不会用普通搜索视频凑数",
                session.calls,
            )
        _report(progress, "抖音样本已整理，准备编辑判断…", "topic_normalize", 72)
        return TopicResearchCollection(
            valid_through=valid_through,
            evidence=normalized[:80],
            calls=list(session.calls),
            warnings=warnings,
        )
