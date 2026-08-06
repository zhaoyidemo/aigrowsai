"""家庭教育选题研究的离线契约测试。

这些测试只使用内存仓储和 ``httpx.MockTransport``，不会访问 TikHub、
OpenRouter 或任何真实计费接口。
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime

import httpx

from qijia_video.contracts import Actor, QuickSourceCardInput
from qijia_video.infrastructure.memory_repository import InMemoryAggregateRepository
from qijia_video.infrastructure.tikhub import (
    BEIJING_TZ,
    DEFAULT_FAMILY_EDUCATION_QUERIES,
    TikHubDouyinResearchProvider,
    _extract_terms,
    _video_evidence,
)
from qijia_video.infrastructure.topic_providers import OpenRouterTopicEditor
from qijia_video.topic_contracts import (
    TikHubCallRecord,
    TopicCandidateProposal,
    TopicContentPillar,
    TopicEvidence,
    TopicEvidenceType,
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
        signal_types=[TopicSignalType.SEARCH_VIDEO],
        queries=["亲子沟通"],
        title=f"家庭教育视频样本 {index}",
        platform_labels=["近 7 天综合搜索样本"],
        source_rank=index,
        video_id=video_id,
        video_url=f"https://www.douyin.com/video/{video_id}",
        author_name="测试作者",
        metrics=TopicMetrics(play_count=1000 + index, like_count=100),
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
    videos = [video_evidence(index) for index in range(1, 6)]
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
            evidence_refs=[term.id, videos[index - 1].id],
            risk_note="不诊断儿童，不承诺单一方法适用于所有家庭。",
        )
        for index, pillar in enumerate(pillars, start=1)
    ]
    return [term, *videos], proposals


class StaticTopicDataProvider:
    name = "fake-tikhub"
    request_budget = 13
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
        item = _video_evidence(
            {
                "aweme_id": "73000000000000001",
                "desc": "孩子发脾气时，家长如何回应",
                "create_time": 1_722_787_200,
                "author": {"nickname": "家庭教育作者", "follower_count": 500},
                "statistics": {
                    "play_count": 10_000,
                    "digg_count": 500,
                    "comment_count": 80,
                    "share_count": 40,
                    "collect_count": 100,
                },
                "video": {"duration": 45_000},
                "share_url": "https://untrusted.example/video",
            },
            query="孩子情绪",
            signal_type=TopicSignalType.SEARCH_VIDEO,
            label="近 7 天综合搜索样本",
            rank=1,
        )

        self.assertIsNotNone(item)
        self.assertEqual(
            item.video_url,
            "https://www.douyin.com/video/73000000000000001",
        )
        self.assertEqual(item.duration_seconds, 45)
        self.assertEqual(item.metrics.like_rate, 0.05)
        self.assertEqual(item.metrics.play_follower_ratio, 20)

    def test_term_filter_keeps_family_education_and_excludes_maternal_goods(self):
        terms = _extract_terms({
            "items": [
                {"keyword": "亲子沟通的三个误区"},
                {"keyword": "奶粉好物推荐"},
                {"keyword": "情绪规则如何建立"},
            ]
        })

        self.assertIn("亲子沟通的三个误区", terms)
        self.assertIn("情绪规则如何建立", terms)
        self.assertNotIn("奶粉好物推荐", terms)


class TikHubProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_research_plan_matches_documented_tikhub_contracts(self):
        today = datetime.now(BEIJING_TZ).strftime("%Y%m%d")
        search_ids = {
            query: f"731000000000000{index:02d}"
            for index, query in enumerate(DEFAULT_FAMILY_EDUCATION_QUERIES, start=1)
        }
        seen_paths: list[str] = []
        item_labels: set[str] = set()

        def aweme(video_id: str, title: str) -> dict:
            return {
                "aweme_id": video_id,
                "desc": title,
                "author": {"nickname": "测试作者", "follower_count": 800},
                "statistics": {
                    "play_count": 20_000,
                    "digg_count": 900,
                    "comment_count": 60,
                    "share_count": 30,
                    "collect_count": 120,
                },
                "video": {"duration": 36_000},
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
            if path.endswith("/fetch_content_valid_date"):
                return response({"content": {"end_date": today}})
            if path.endswith("/fetch_content_creative_keywords"):
                self.assertEqual(request.url.params["tag_id"], "617")
                return response({"items": [
                    {"keyword": "亲子沟通"},
                    {"keyword": "孩子情绪管理"},
                ]})
            if path.endswith("/fetch_content_creative_topic"):
                self.assertEqual(request.url.params["rank_type"], "rise")
                return response({"items": [
                    {"topic_name": "青春期亲子边界"},
                    {"topic_name": "孩子学习习惯"},
                ]})
            if path.endswith("/fetch_video_search_v2"):
                payload = json.loads(request.content)
                self.assertEqual(payload["publish_time"], "7")
                self.assertEqual(payload["filter_duration"], "0-1")
                query = payload["keyword"]
                return response({
                    "business_data": [{"data": {"aweme_info": aweme(
                        search_ids[query], f"{query}的家庭教育样本"
                    )}}]
                })
            if path.endswith("/fetch_content_creative_keyword_items"):
                keyword = request.url.params["keyword"]
                suffix = "91" if keyword == "亲子沟通" else "92"
                return response({"items": [aweme(
                    f"731000000000000{suffix}", f"{keyword}贡献视频"
                )]})
            if path.endswith("/fetch_item_query"):
                self.assertEqual(request.url.params["category_id"], "617")
                item_labels.add(request.url.params["label_type"])
                label = request.url.params["label_type"]
                return response({"items": [aweme(
                    f"7310000000000008{label}", "家庭教育平台标签样本"
                )]})
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
        self.assertEqual(item_labels, {"1", "2"})
        self.assertGreaterEqual(
            sum(item.evidence_type == TopicEvidenceType.VIDEO for item in collection.evidence),
            6,
        )
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

        class FailingProvider:
            name = "failing-tikhub"
            request_budget = 13
            configured = True

            async def collect_family_education(self, progress=None, on_calls=None):
                del progress
                if on_calls:
                    await on_calls(calls)
                raise TopicCollectionFailed("样本不足", calls)

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
            self.assertEqual(payload["reasoning"]["effort"], "medium")
            self.assertEqual(payload["max_completion_tokens"], 6000)
            self.assertTrue(payload["provider"]["require_parameters"])
            self.assertNotIn("plugins", payload)
            self.assertIn("不可信数据", payload["messages"][1]["content"])
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

    @staticmethod
    def _minimal_evidence() -> list[TopicEvidence]:
        return [video_evidence(1), video_evidence(2)]
