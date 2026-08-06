"""家庭教育选题研究的离线契约测试。

这些测试只使用内存仓储和 ``httpx.MockTransport``，不会访问 TikHub、
OpenRouter 或任何真实计费接口。
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

import httpx

from qijia_video.contracts import Actor, QuickSourceCardInput
from qijia_video.infrastructure.memory_repository import InMemoryAggregateRepository
from qijia_video.infrastructure.tikhub import (
    BEIJING_TZ,
    DEFAULT_FAMILY_EDUCATION_QUERIES,
    TikHubDouyinResearchProvider,
    _family_relevant,
    _video_evidence,
    _video_evidence_list,
)
from qijia_video.infrastructure.topic_providers import OpenRouterTopicEditor
from qijia_video.topic_contracts import (
    TikHubCallRecord,
    TopicCandidateProposal,
    TopicContentPillar,
    TopicEvidence,
    TopicEvidenceTier,
    TopicEvidenceType,
    TopicLowFollowerDiagnostics,
    TopicMetrics,
    TopicModelUsage,
    TopicSignalType,
)
from qijia_video.topic_ports import (
    TopicCollectionFailed,
    TopicEditorialFailed,
    TopicEditorialResult,
    TopicResearchCollection,
)
from qijia_video.topic_service import TopicResearchService


def video_evidence(index: int) -> TopicEvidence:
    video_id = f"730000000000000{index:02d}"
    return TopicEvidence(
        id=f"ev_{index:012x}",
        evidence_type=TopicEvidenceType.VIDEO,
        signal_types=[TopicSignalType.LOW_FOLLOWER_VIDEO],
        queries=["亲子沟通"],
        title=f"家庭教育视频样本 {index}",
        platform_labels=["TikHub 72 小时窗口低粉爆款榜"],
        quality_tier=TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
        qualification_reasons=["测试中的低粉爆款证据"],
        source_rank=index,
        video_id=video_id,
        video_url=f"https://www.douyin.com/video/{video_id}",
        author_name="测试作者",
        metrics=TopicMetrics(
            play_count=600_000 + index,
            like_count=36_000,
            comment_count=2_500,
            share_count=1_400,
            collect_count=1_000,
            follower_count=20_000,
            like_rate=0.06,
            deep_engagement_rate=0.008167,
            play_follower_ratio=30,
            published_age_hours=24,
            average_daily_plays=600_000,
        ),
    )


def topic_fixture() -> tuple[list[TopicEvidence], list[TopicCandidateProposal]]:
    term = TopicEvidence(
        id="ev_ffffffffffff",
        evidence_type=TopicEvidenceType.TREND_TERM,
        signal_types=[TopicSignalType.CREATIVE_KEYWORD],
        queries=["亲子沟通"],
        title="亲子沟通",
        platform_labels=["母婴垂类近 3 天创作热词"],
        source_rank=1,
    )
    videos = [video_evidence(index) for index in range(1, 11)]
    pillars = [
        TopicContentPillar.COMMUNICATION,
        TopicContentPillar.EMOTION,
        TopicContentPillar.LEARNING,
        TopicContentPillar.BOUNDARIES,
        TopicContentPillar.DIGITAL,
    ]
    proposals = [
        TopicCandidateProposal(
            content_pillar=pillar,
            title=f"家长如何看见孩子的真实需要 {index}",
            parent_question=f"面对第 {index} 类亲子冲突时，家长可以先做什么？",
            editorial_angle="从家长可观察的日常互动切入，提出一个需要继续查证的教育命题。",
            opening_hook="孩子真正需要的，也许不是你立刻给出的答案。",
            why_now="近期抖音家庭教育样本持续出现这一困惑，且代表视频已有可见互动数据。",
            evidence_refs=[
                term.id,
                videos[(index - 1) * 2].id,
                videos[(index - 1) * 2 + 1].id,
            ],
            risk_note="不诊断儿童，不承诺单一方法适用于所有家庭。",
        )
        for index, pillar in enumerate(pillars, start=1)
    ]
    return [term, *videos], proposals


class StaticTopicDataProvider:
    name = "fake-tikhub"
    request_budget = 15
    configured = True

    def __init__(self, evidence: list[TopicEvidence]):
        self.evidence = evidence
        self.calls = [
            TikHubCallRecord(
                endpoint="/date",
                request_id="req-date",
                response_code=200,
                succeeded=True,
            ),
            TikHubCallRecord(
                endpoint="/search",
                request_id="req-search",
                response_code=200,
                succeeded=True,
            ),
        ]

    async def collect_family_education(self, progress=None, on_calls=None):
        del progress
        if on_calls:
            await on_calls(self.calls)
        return TopicResearchCollection(
            valid_through="2026-08-04",
            evidence=self.evidence,
            calls=self.calls,
            warnings=[],
        )


class StaticTopicEditor:
    name = "fake-editor"
    model = "test/editor"
    configured = True

    def __init__(self, proposals: list[TopicCandidateProposal]):
        self.proposals = proposals

    async def propose(self, evidence, *, valid_through, on_usage=None):
        self.last_evidence = evidence
        self.last_valid_through = valid_through
        result = TopicEditorialResult(
            proposals=self.proposals,
            usage=TopicModelUsage(
                model=self.model,
                request_id="gen-test",
                request_count=1,
                succeeded=True,
                http_status_code=200,
                input_tokens=800,
                output_tokens=400,
                total_tokens=1200,
                reported_cost_usd=0.004,
            ),
        )
        if on_usage:
            await on_usage(result.usage)
        return result


class TopicNormalizationTests(unittest.TestCase):
    def test_topic_handoff_keeps_editorial_brief_out_of_verified_facts(self):
        card = QuickSourceCardInput(
            title="孩子发脾气时，父母先做什么？",
            source_material="这是一段已经由编辑人工核对、允许引用的资料摘记。",
            rights_confirmed=True,
            editorial_brief="从父母的即时反应切入，讨论共同调节而不是压制情绪。",
            parent_question="孩子情绪失控时，家长如何先稳定互动？",
        ).to_source_card_input()

        self.assertEqual(
            card.core_idea,
            "从父母的即时反应切入，讨论共同调节而不是压制情绪。",
        )
        self.assertEqual(
            card.verified_facts[0].text,
            "这是一段已经由编辑人工核对、允许引用的资料摘记。",
        )
        self.assertEqual(card.parent_question, "孩子情绪失控时，家长如何先稳定互动？")

    def test_video_normalization_uses_canonical_link_and_transparent_rates(self):
        as_of = datetime(2026, 8, 6, 12, tzinfo=BEIJING_TZ)
        item = _video_evidence(
            {
                "aweme_id": "73000000000000001",
                "desc": "孩子发脾气时，家长如何回应",
                "create_time": int((as_of - timedelta(hours=48)).timestamp()),
                "author": {"nickname": "家庭教育作者", "follower_count": 25_000},
                "statistics": {
                    "play_count": 500_000,
                    "digg_count": 25_000,
                    "comment_count": 2_000,
                    "share_count": 1_200,
                    "collect_count": 800,
                },
                "video": {"duration": 45_000},
                "share_url": "https://untrusted.example/video",
            },
            query="孩子情绪",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
        )

        self.assertIsNotNone(item)
        self.assertEqual(
            item.video_url,
            "https://www.douyin.com/video/73000000000000001",
        )
        self.assertEqual(item.duration_seconds, 45)
        self.assertEqual(item.metrics.like_rate, 0.05)
        self.assertEqual(item.metrics.play_follower_ratio, 20)
        self.assertEqual(item.metrics.deep_engagement_rate, 0.008)
        self.assertEqual(item.metrics.published_age_hours, 48)
        self.assertEqual(item.metrics.average_daily_plays, 250_000)
        self.assertEqual(
            item.quality_tier,
            TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
        )

    def test_video_normalization_accepts_age_adjusted_emerging_low_follower_hit(self):
        as_of = datetime(2026, 8, 6, 12, tzinfo=BEIJING_TZ)
        item = _video_evidence(
            {
                "itemId": "73000000000000007",
                "itemTitle": "别再这样和他说话",
                "itemCreateTime": int((as_of - timedelta(hours=12)).timestamp()),
                "authorFollowerCnt": 10_000,
                "itemPlayCnt": 120_000,
                "itemLikeCnt": 3_600,
                "itemCommentCnt": 200,
                "itemShareCnt": 100,
                "itemCollectCnt": 60,
            },
            query="亲子沟通",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
        )

        self.assertIsNotNone(item)
        self.assertEqual(
            item.quality_tier,
            TopicEvidenceTier.EMERGING_LOW_FOLLOWER_BREAKOUT,
        )
        self.assertEqual(item.metrics.play_follower_ratio, 12)
        self.assertEqual(item.metrics.like_rate, 0.03)
        self.assertEqual(item.metrics.deep_engagement_rate, 0.003)

    def test_billboard_sample_survives_missing_follower_and_publish_metrics(self):
        item = _video_evidence(
            {
                "itemId": "73000000000000009",
                "itemTitle": "孩子不愿沟通时父母先停一下",
                "itemPlayCnt": 80_000,
                "itemLikeCnt": 1_600,
            },
            query="亲子沟通",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=2,
        )

        self.assertIsNotNone(item)
        self.assertEqual(
            item.quality_tier,
            TopicEvidenceTier.LOW_FOLLOWER_BILLBOARD,
        )
        self.assertEqual(item.metrics.follower_count, 0)
        self.assertIsNone(item.metrics.published_age_hours)

    def test_video_diagnostics_separate_empty_schema_and_missing_title(self):
        as_of = datetime(2026, 8, 6, 12, tzinfo=BEIJING_TZ)
        diagnostics = TopicLowFollowerDiagnostics()

        empty = _video_evidence_list(
            {"list": [], "total": 0},
            query="家庭教育",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )
        unrecognized = _video_evidence_list(
            {"rows": [{"unknownVideoKey": "value"}], "total": 1},
            query="家庭教育",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )
        missing_title = _video_evidence_list(
            {"list": [{"itemId": "73000000000000008"}]},
            query="家庭教育",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )

        self.assertEqual(empty, [])
        self.assertEqual(unrecognized, [])
        self.assertEqual(missing_title, [])
        self.assertEqual(diagnostics.empty_or_unrecognized_query_count, 2)
        self.assertEqual(diagnostics.empty_query_count, 1)
        self.assertEqual(diagnostics.unrecognized_query_count, 1)
        self.assertEqual(diagnostics.received_count, 1)
        self.assertEqual(diagnostics.rejected_missing_identity_count, 1)
        self.assertEqual(diagnostics.rejected_missing_title_count, 1)

    def test_video_normalization_keeps_billboard_samples_and_only_rejects_scope(self):
        as_of = datetime(2026, 8, 6, 12, tzinfo=BEIJING_TZ)
        diagnostics = TopicLowFollowerDiagnostics()
        base = {
            "aweme_id": "73000000000000002",
            "desc": "孩子发脾气时家长如何回应",
            "create_time": int((as_of - timedelta(hours=24)).timestamp()),
            "author": {"follower_count": 20_000},
            "statistics": {
                "play_count": 99_999,
                "digg_count": 3_000,
                "comment_count": 2_500,
                "share_count": 1_500,
                "collect_count": 1_000,
            },
        }
        weak = _video_evidence(
            base,
            query="孩子情绪",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )
        excluded = _video_evidence(
            {
                **base,
                "aweme_id": "73000000000000003",
                "desc": "宝宝辅食好物推荐",
                "statistics": {
                    "play_count": 1_000_000,
                    "digg_count": 80_000,
                    "comment_count": 4_000,
                    "share_count": 2_500,
                    "collect_count": 1_500,
                },
            },
            query="孩子情绪",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )
        stale = _video_evidence(
            {
                **base,
                "aweme_id": "73000000000000004",
                "create_time": int((as_of - timedelta(hours=73)).timestamp()),
                "statistics": {
                    "play_count": 1_000_000,
                    "digg_count": 80_000,
                    "comment_count": 4_000,
                    "share_count": 2_500,
                    "collect_count": 1_500,
                },
            },
            query="孩子情绪",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )
        missing_time_payload = {
            **base,
            "aweme_id": "73000000000000005",
            "statistics": {
                "play_count": 1_000_000,
                "digg_count": 80_000,
                "comment_count": 4_000,
                "share_count": 2_500,
                "collect_count": 1_500,
            },
        }
        missing_time_payload.pop("create_time")
        missing_time = _video_evidence(
            missing_time_payload,
            query="孩子情绪",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )
        future = _video_evidence(
            {
                **base,
                "aweme_id": "73000000000000006",
                "create_time": int((as_of + timedelta(seconds=1)).timestamp()),
                "statistics": {
                    "play_count": 1_000_000,
                    "digg_count": 80_000,
                    "comment_count": 4_000,
                    "share_count": 2_500,
                    "collect_count": 1_500,
                },
            },
            query="孩子情绪",
            signal_type=TopicSignalType.LOW_FOLLOWER_VIDEO,
            label="TikHub 72 小时窗口低粉爆款榜",
            rank=1,
            as_of=as_of,
            low_follower_diagnostics=diagnostics,
        )

        self.assertIsNotNone(weak)
        self.assertEqual(
            weak.quality_tier,
            TopicEvidenceTier.LOW_FOLLOWER_BILLBOARD,
        )
        self.assertIsNone(excluded)
        self.assertIsNotNone(stale)
        self.assertIsNotNone(missing_time)
        self.assertIsNotNone(future)
        self.assertEqual(diagnostics.rejected_off_topic_count, 1)
        self.assertEqual(diagnostics.rejected_too_old_count, 0)
        self.assertEqual(diagnostics.rejected_invalid_publish_time_count, 0)

    def test_scope_filter_keeps_family_education_and_excludes_maternal_goods(self):
        self.assertTrue(_family_relevant("亲子沟通的三个误区"))
        self.assertTrue(_family_relevant("孩子情绪规则如何建立"))
        self.assertFalse(_family_relevant("孩子奶粉好物推荐"))


class TikHubProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_research_plan_matches_documented_tikhub_contracts(self):
        collection_now = datetime.now(BEIJING_TZ)
        seen_paths: list[str] = []
        sample_paths: set[str] = set()
        detail_by_id: dict[str, dict] = {}

        def aweme(video_id: str, title: str, sample_kind: str) -> dict:
            low_follower = sample_kind == "low"
            return {
                "itemID": video_id,
                "title": title,
                "publishDate": int((
                    collection_now - timedelta(hours=12 if low_follower else 72)
                ).timestamp()),
                "author": {
                    "nickName": "测试作者",
                    "fansCount": 20_000 if low_follower else 500_000,
                },
                "statistics": {
                    "playCount": 600_000 if low_follower else 1_200_000,
                    "diggCount": 36_000 if low_follower else 108_000,
                    "commentCount": 3_000 if low_follower else 6_000,
                    "shareCount": 1_800 if low_follower else 3_600,
                    "favoriteCount": 1_200 if low_follower else 2_400,
                },
                "video": {"durationSeconds": 36},
            }

        def response(data) -> httpx.Response:
            return httpx.Response(200, json={
                "code": 200,
                "request_id": f"req-{len(seen_paths)}",
                "message": "ok",
                "data": data,
            })

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            path = request.url.path
            seen_paths.append(path)
            if path.endswith("/fetch_hot_total_low_fan_list") or path.endswith(
                "/fetch_hot_total_high_like_list"
            ):
                sample_paths.add(path)
                body = json.loads(request.content.decode("utf-8"))
                low_follower = path.endswith("/fetch_hot_total_low_fan_list")
                self.assertEqual(body["page"], 1)
                self.assertEqual(body["page_size"], 20)
                self.assertEqual(body["date_window"], 72 if low_follower else 168)
                self.assertEqual(body["tags"], [])
                query = body["keyword"]
                query_index = DEFAULT_FAMILY_EDUCATION_QUERIES.index(query) + 1
                sample_kind = "low" if low_follower else "high"
                video_id = (
                    f"7310000000000{query_index:02d}"
                    f"{1 if low_follower else 4}"
                )
                full_detail = aweme(
                    video_id,
                    f"{query}的家庭教育爆款样本",
                    sample_kind,
                )
                detail_by_id[video_id] = full_detail
                return response({"list": [{
                    "itemID": video_id,
                    "title": full_detail["title"],
                    "publishDate": full_detail["publishDate"],
                }]})
            if path.endswith("/fetch_multi_video"):
                body = json.loads(request.content.decode("utf-8"))
                self.assertIsInstance(body, list)
                self.assertLessEqual(len(body), 50)
                return response({
                    "aweme_list": [detail_by_id[video_id] for video_id in body]
                })
            self.fail(f"unexpected request: {path}")

        provider = TikHubDouyinResearchProvider(
            api_key="test-key",
            base_url="https://api.tikhub.dev",
            request_budget=100,
            transport=httpx.MockTransport(handler),
        )

        persisted_call_counts: list[int] = []

        async def record_calls(calls):
            persisted_call_counts.append(len(calls))

        collection = await provider.collect_family_education(on_calls=record_calls)

        self.assertEqual(provider.request_budget, 100)
        self.assertEqual(len(collection.calls), 13)
        self.assertEqual(persisted_call_counts, list(range(1, 14)))
        self.assertTrue(all(item.succeeded for item in collection.calls))
        self.assertEqual(sample_paths, {
            "/api/v1/douyin/billboard/fetch_hot_total_low_fan_list",
            "/api/v1/douyin/billboard/fetch_hot_total_high_like_list",
        })
        sample_calls = [
            item
            for item in collection.calls
            if "/billboard/" in item.endpoint
        ]
        self.assertTrue(all(item.request_label for item in sample_calls))
        self.assertTrue(all("list" in item.data_shape for item in sample_calls))
        self.assertGreaterEqual(
            sum(item.evidence_type == TopicEvidenceType.VIDEO for item in collection.evidence),
            10,
        )
        self.assertGreaterEqual(sum(
            item.quality_tier == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
            for item in collection.evidence
        ), 5)
        self.assertEqual(collection.low_follower_diagnostics.received_count, 6)
        self.assertEqual(
            collection.low_follower_diagnostics.unique_qualified_count,
            6,
        )
        self.assertEqual(
            collection.low_follower_diagnostics.strong_qualified_count,
            6,
        )
        self.assertEqual(
            collection.low_follower_diagnostics.detail_enriched_count,
            6,
        )
        self.assertTrue(all(
            item.metrics_enriched
            for item in collection.evidence
            if item.evidence_type == TopicEvidenceType.VIDEO
        ))
        self.assertTrue(all(
            item.video_url.startswith("https://www.douyin.com/video/")
            for item in collection.evidence
            if item.evidence_type == TopicEvidenceType.VIDEO
        ))


class TopicResearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actor = Actor(user_id=7, username="editor", role="member")
        self.repository = InMemoryAggregateRepository()
        self.evidence, self.proposals = topic_fixture()

    async def test_success_persists_cost_quality_gates_and_human_selection(self):
        service = TopicResearchService(
            repository=self.repository,
            data_provider=StaticTopicDataProvider(self.evidence),
            editor=StaticTopicEditor(self.proposals),
            estimated_tikhub_cost_per_success_usd=0.001,
        )
        run = await service.create_run(self.actor)

        ready = await service.execute(run.id, self.actor)

        self.assertEqual(ready.status.value, "ready")
        self.assertEqual(len(ready.candidates), 5)
        self.assertEqual(ready.cost.tikhub_request_count, 2)
        self.assertEqual(ready.cost.estimated_tikhub_cost_usd, 0.002)
        self.assertEqual(ready.cost.estimated_total_cost_usd, 0.006)
        self.assertEqual(ready.cost.estimated_cost_per_candidate_usd, 0.0012)
        self.assertTrue(ready.cost.model_usage.succeeded)

        selected = await service.select_candidate(
            ready.id,
            ready.candidates[0].id,
            ready.revision,
            self.actor,
        )
        self.assertEqual(selected.selected_candidate_id, ready.candidates[0].id)
        self.assertEqual(selected.selected_by, "editor")

    async def test_tikhub_failure_keeps_spent_calls_and_skips_editor(self):
        calls = [
            TikHubCallRecord(
                endpoint="/date",
                request_id="paid-request",
                response_code=200,
                succeeded=True,
            ),
            TikHubCallRecord(
                endpoint="/search",
                response_code=502,
                succeeded=False,
            ),
        ]
        diagnostics = TopicLowFollowerDiagnostics(
            received_count=8,
            rejected_insufficient_plays_count=6,
            rejected_missing_followers_count=2,
        )

        class FailingProvider:
            name = "failing-tikhub"
            request_budget = 15
            configured = True

            async def collect_family_education(self, progress=None, on_calls=None):
                del progress
                if on_calls:
                    await on_calls(calls)
                raise TopicCollectionFailed(
                    "样本不足",
                    calls,
                    diagnostics,
                    ["一个低粉查询返回结构异常"],
                )

        editor = StaticTopicEditor(self.proposals)
        service = TopicResearchService(
            repository=self.repository,
            data_provider=FailingProvider(),
            editor=editor,
        )
        run = await service.create_run(self.actor)

        with self.assertRaises(TopicCollectionFailed):
            await service.execute(run.id, self.actor)

        stored = await service.get_run(run.id, self.actor)
        self.assertEqual(stored.cost.tikhub_request_count, 2)
        self.assertEqual(stored.cost.tikhub_success_count, 1)
        self.assertIsNone(stored.cost.model_usage)
        self.assertEqual(stored.low_follower_diagnostics.received_count, 8)
        self.assertEqual(
            stored.low_follower_diagnostics.rejected_insufficient_plays_count,
            6,
        )
        self.assertEqual(stored.warnings, ["一个低粉查询返回结构异常"])
        failed = await service.mark_failed(run.id, "样本不足", self.actor)
        self.assertEqual(failed.cost.estimated_total_cost_usd, 0.001)
        self.assertFalse(hasattr(editor, "last_evidence"))

    async def test_editor_failure_persists_reported_model_cost(self):
        class FailingEditor:
            name = "failing-editor"
            model = "test/editor"
            configured = True

            async def propose(self, evidence, *, valid_through, on_usage=None):
                del evidence, valid_through
                usage = TopicModelUsage(
                    model=self.model,
                    request_id="gen-failed",
                    request_count=1,
                    succeeded=False,
                    http_status_code=200,
                    total_tokens=900,
                    reported_cost_usd=0.003,
                )
                if on_usage:
                    await on_usage(usage)
                raise TopicEditorialFailed("结构不合格", usage)

        service = TopicResearchService(
            repository=self.repository,
            data_provider=StaticTopicDataProvider(self.evidence),
            editor=FailingEditor(),
        )
        run = await service.create_run(self.actor)

        with self.assertRaises(TopicEditorialFailed):
            await service.execute(run.id, self.actor)

        stored = await service.get_run(run.id, self.actor)
        self.assertEqual(stored.cost.model_usage.request_id, "gen-failed")
        self.assertFalse(stored.cost.model_usage.succeeded)
        self.assertEqual(stored.cost.estimated_total_cost_usd, 0.005)


class OpenRouterTopicEditorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_paid_response_still_exposes_usage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            reference_schema = payload["response_format"]["json_schema"]["schema"][
                "properties"
            ]["candidates"]["items"]["properties"]["evidence_refs"]["items"]
            self.assertEqual(
                reference_schema["enum"],
                [item.id for item in self._minimal_evidence()],
            )
            self.assertEqual(payload["reasoning"]["effort"], "medium")
            self.assertEqual(payload["max_completion_tokens"], 6000)
            self.assertTrue(payload["provider"]["require_parameters"])
            self.assertNotIn("plugins", payload)
            self.assertIn("不可信数据", payload["messages"][1]["content"])
            self.assertIn("白名单逐字复制", payload["messages"][1]["content"])
            return httpx.Response(
                200,
                headers={"x-request-id": "request-header-id"},
                json={
                    "id": "generation-id",
                    "model": "test/editor",
                    "choices": [{"message": {"content": '{"candidates":[]}'}}],
                    "usage": {
                        "prompt_tokens": 500,
                        "completion_tokens": 20,
                        "total_tokens": 520,
                        "cost": 0.0008,
                    },
                },
            )

        editor = OpenRouterTopicEditor(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/editor",
            transport=httpx.MockTransport(handler),
        )
        recorded_usage: list[TopicModelUsage] = []

        async def record_usage(usage):
            recorded_usage.append(usage)

        with self.assertRaises(TopicEditorialFailed) as raised:
            await editor.propose(
                self._minimal_evidence(),
                valid_through="2026-08-04",
                on_usage=record_usage,
            )

        usage = raised.exception.usage
        self.assertEqual(usage.request_count, 1)
        self.assertFalse(usage.succeeded)
        self.assertEqual(usage.request_id, "request-header-id")
        self.assertEqual(usage.total_tokens, 520)
        self.assertEqual(usage.reported_cost_usd, 0.0008)
        self.assertEqual(len(recorded_usage), 1)
        self.assertEqual(recorded_usage[0], usage)

    async def test_rejects_evidence_reference_outside_request_whitelist(self):
        evidence, proposals = topic_fixture()
        generated = {
            "candidates": [
                proposal.model_dump(mode="json") for proposal in proposals
            ]
        }
        generated["candidates"][0]["evidence_refs"][0] = "ev_283775de82f"

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            reference_schema = payload["response_format"]["json_schema"]["schema"][
                "properties"
            ]["candidates"]["items"]["properties"]["evidence_refs"]["items"]
            self.assertEqual(
                reference_schema["enum"],
                [item.id for item in evidence],
            )
            return httpx.Response(
                200,
                headers={"x-request-id": "request-unknown-evidence"},
                json={
                    "id": "generation-unknown-evidence",
                    "model": "test/editor",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    generated, ensure_ascii=False
                                )
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 800,
                        "completion_tokens": 500,
                        "total_tokens": 1300,
                        "cost": 0.002,
                    },
                },
            )

        editor = OpenRouterTopicEditor(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/editor",
            transport=httpx.MockTransport(handler),
        )
        recorded_usage: list[TopicModelUsage] = []

        async def record_usage(usage):
            recorded_usage.append(usage)

        with self.assertRaisesRegex(
            TopicEditorialFailed, "没有遵守本轮证据 ID 白名单"
        ) as raised:
            await editor.propose(
                evidence,
                valid_through="2026-08-04",
                on_usage=record_usage,
            )

        self.assertEqual(
            raised.exception.usage.request_id, "request-unknown-evidence"
        )
        self.assertFalse(raised.exception.usage.succeeded)
        self.assertEqual(len(recorded_usage), 1)
        self.assertEqual(recorded_usage[0], raised.exception.usage)

    @staticmethod
    def _minimal_evidence() -> list[TopicEvidence]:
        return [video_evidence(1), video_evidence(2)]
