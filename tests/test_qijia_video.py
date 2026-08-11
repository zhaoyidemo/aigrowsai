import asyncio
import base64
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qijia_video import api as qijia_api
from qijia_video import runtime as qijia_runtime
from qijia_video.contracts import (
    Actor,
    AssetRef,
    CreativeBrief,
    CreativeRequestInput,
    GenerationSettings,
    JobState,
    NewsResearchBrief,
    NewsTopicInput,
    PersonResearchBrief,
    PersonViewpointInput,
    PreGenerationMediaMode,
    QuickSourceCardInput,
    QualityReport,
    ResearchDiagnostics,
    RenderManifest,
    ScriptDraft,
    SourceCard,
    SourceCardInput,
    StoryboardPlan,
    StoryboardShot,
    ProviderTaskState,
    ProviderTask,
    ProviderUsageRecord,
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    SEEDANCE_RETIRED_MODEL,
    VideoJob,
    VisualGenerationRequest,
    content_hash,
    timestamp,
)
from qijia_video.errors import (
    AccessDenied,
    InvalidTransition,
    ProviderUnavailable,
    QualityGateFailed,
    ResourceNotFound,
    RevisionConflict,
)
from qijia_video.director_skill_registry import (
    default_director_skill_registry,
)
from qijia_video.infrastructure.memory_repository import InMemoryAggregateRepository
from qijia_video.infrastructure.media import FfmpegMediaPackager
from qijia_video.infrastructure.image_providers import (
    MockImageProvider,
    SEEDREAM_MAX_SEED,
    SeedreamImageProvider,
)
from qijia_video.infrastructure.mock_providers import (
    SilentTtsProvider,
    TemplateScriptProvider,
    TemplateStoryboardProvider,
)
from qijia_video.infrastructure.script_providers import (
    OPENROUTER_REASONING_EFFORT,
    OpenRouterScriptProvider,
    OpenRouterStoryboardProvider,
    SCRIPT_MAX_COMPLETION_TOKENS,
    SCRIPT_PROMPT_VERSION,
    STORYBOARD_MAX_COMPLETION_TOKENS,
)
from qijia_video.infrastructure.storage import (
    TOS_CONTROL_TIMEOUT_SECONDS,
    TOS_DOWNLOAD_ATTEMPTS,
    TOS_DOWNLOAD_PART_SIZE_BYTES,
    TOS_DOWNLOAD_TASK_COUNT,
    TOS_SOCKET_TIMEOUT_SECONDS,
    LocalArtifactStorage,
    TosArtifactStorage,
)
from qijia_video.infrastructure.tts_providers import VolcengineTtsProvider
from qijia_video.infrastructure.video_providers import (
    MockVideoProvider,
    SeedanceVideoProvider,
)
from qijia_video.prompts import SCRIPT_HARD_MAX_CHARS, narration_char_count
from qijia_video.prompt_orchestration import compile_script_skill_prompt
from qijia_video.service import QijiaVideoService, REQUIRED_PACKAGE_NAMES
from qijia_video.script_skill_registry import default_script_skill_registry
from qijia_video.skill_registry import default_skill_registry
from qijia_video.visual_prompting import (
    compile_first_frame_prompt,
    compile_storyboard_base_style,
    compile_video_prompt,
)
from qijia_video.visual_style_registry import (
    default_prompt_writing_profile_registry,
    default_visual_style_registry,
)
from qijia_video import auth as auth_service


def valid_card(**updates) -> SourceCardInput:
    value = {
        "content_domain": "parent_education",
        "content_format": "concept_explainer",
        "subject": {"type": "concept", "name": "自主练习"},
        "title": "如何给孩子自主练习的空间？",
        "core_idea": "能力需要在安全边界内通过练习形成。",
        "parent_question": "什么时候帮助孩子，什么时候让孩子尝试？",
        "sources": [{
            "id": "source_01",
            "type": "book",
            "title": "测试资料",
            "author": "内容团队",
            "publisher": "测试出版社",
            "edition": "2026 年第 1 版",
            "locator": "第 1 章",
            "rights_status": "verified_for_citation",
        }],
        "verified_facts": [{
            "id": "fact_01",
            "text": "这是一条已核验、仅用于工作流测试的事实。",
            "source_refs": ["source_01"],
        }],
        "interpretation_boundary": [{
            "id": "boundary_01",
            "text": "不得表述为对具体儿童的诊断或治疗建议。",
        }],
    }
    value.update(updates)
    return SourceCardInput.model_validate(value)


def valid_creative_brief(*, evidence_refs=None) -> dict:
    return {
        "central_question": "这项观点究竟在什么依据和边界下成立？",
        "core_thesis": "先核验依据与语境，再理解观点本身。",
        "audience_promise": "帮助普通观众形成一条可复用的判断路径。",
        "narrative_arc": [
            "直接呈现需要辨析的判断",
            "交代必要证据与语境",
            "解释核心逻辑",
            "回到问题并克制收束",
        ],
        "tone": "准确、克制、具体",
        "visual_concept": "用同一主体和关键物件的状态变化贯穿全片",
        "continuity_anchors": ["同一核心主体", "同一关键物件"],
        "must_include": ["保留证据强度"],
        "must_avoid": ["不制造焦虑"],
        "evidence_refs": list(evidence_refs or []),
    }


class FakeRenderer:
    name = "fake-remotion"

    def __init__(self):
        self.render_calls = 0
        self.cover_calls = 0

    async def render(self, manifest, storage, workspace: Path) -> Path:
        del manifest, storage
        self.render_calls += 1
        output = workspace / "raw.mp4"
        output.write_bytes(b"raw-video")
        return output

    async def render_cover(self, manifest, storage, workspace: Path) -> Path:
        del manifest, storage
        self.cover_calls += 1
        output = workspace / "cover.jpg"
        output.write_bytes(b"jpeg-cover")
        return output

    def metadata(self) -> dict:
        return {"name": self.name, "remotion_version": "test"}


class FakeMediaPackager:
    name = "fake-ffmpeg"

    async def normalize(self, source: Path, destination: Path) -> Path:
        shutil.copyfile(source, destination)
        return destination

    async def prepare_video_for_timeline(
        self,
        source: Path,
        destination: Path,
        *,
        minimum_duration_seconds: float,
    ) -> tuple[Path, float]:
        shutil.copyfile(source, destination)
        return destination, minimum_duration_seconds + (1 / 30)

    async def prepare_uploaded_video_for_timeline(
        self,
        source: Path,
        destination: Path,
        *,
        chapter_duration_seconds: float,
    ) -> tuple[Path, float]:
        shutil.copyfile(source, destination)
        return destination, chapter_duration_seconds + (1 / 30)

class PassingQualityChecker:
    name = "fake-ffprobe"

    async def inspect(self, path: Path, manifest: RenderManifest) -> QualityReport:
        del path, manifest
        return QualityReport(
            automatic_status="review_ready",
            checks=[{"id": "fixture", "passed": True, "detail": "ok"}],
            generated_at=timestamp(),
        )


class RecordingImageProvider(MockImageProvider):
    def __init__(self):
        self.reference_image_urls: list[str] = []
        self.prompts: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_url: str = "",
    ):
        self.reference_image_urls.append(reference_image_url)
        self.prompts.append(prompt)
        return await super().generate(
            prompt,
            seed=seed,
            reference_image_url=reference_image_url,
        )


class QijiaVideoContractTests(unittest.TestCase):
    def test_content_skills_are_versioned_workflow_presets_without_prompts(self):
        catalog = default_skill_registry.public_catalog()
        self.assertEqual(
            {item["skill_id"] for item in catalog},
            {"explain-expert-view"},
        )
        expert = default_skill_registry.resolve("explain-expert-view")
        news = default_skill_registry.resolve("brief-recent-news")
        self.assertEqual(expert.version, "3.0.0")
        self.assertEqual(news.version, "2.1.0")
        self.assertEqual(expert.input_mode.value, "creative_request")
        self.assertEqual(expert.knowledge_mode.value, "model_knowledge")
        self.assertEqual(expert.research_mode.value, "none")
        self.assertTrue(expert.available_for_new_jobs)
        self.assertFalse(news.available_for_new_jobs)
        self.assertEqual(news.research_mode.value, "recent_news_required")
        self.assertNotEqual(expert.manifest_hash, news.manifest_hash)
        self.assertIn("quote-integrity", expert.policy_ids)
        self.assertIn("news-freshness", news.policy_ids)
        self.assertEqual(expert.snapshot().research_prompt, "")
        self.assertEqual(expert.snapshot().script_system_prompt, "")
        self.assertEqual(expert.snapshot().visual_policy, "")
        self.assertTrue(all(
            "generation_defaults" not in item
            for item in catalog
        ))
        self.assertTrue(all(
            item["external_retrieval"] is False
            for item in catalog
        ))

    def test_visual_styles_and_prompt_method_are_provider_independent(self):
        director_catalog = default_director_skill_registry.public_catalog()
        catalog = default_visual_style_registry.public_catalog()
        self.assertEqual(
            {item["skill_id"] for item in director_catalog},
            {"animated-explainer"},
        )
        self.assertTrue(all(
            item["role"] == "director_owner" for item in director_catalog
        ))
        self.assertTrue(all(
            item["role"] == "visual_language" for item in catalog
        ))
        self.assertEqual(
            {item["style_id"] for item in catalog},
            {
                "content-skill-default",
                "paper-collage-explainer",
                "papercraft-stop-motion",
            },
        )
        collage = default_visual_style_registry.resolve(
            "paper-collage-explainer"
        )
        papercraft = default_visual_style_registry.resolve(
            "papercraft-stop-motion"
        )
        profile = default_prompt_writing_profile_registry.resolve(
            "h3-prompt-writing"
        )
        legacy_profile = default_prompt_writing_profile_registry.resolve(
            "structured-multimodal"
        )
        self.assertEqual(collage.version, "1.1.0")
        self.assertIn("编辑纸张拼贴", collage.director_prompt)
        self.assertIn("纸片", collage.motion_rules)
        self.assertIn("纸偶关节", papercraft.storyboard_rules)
        self.assertIn("先判断输入模式", profile.planning_framework)
        self.assertIn("I2V", profile.planning_framework)
        self.assertIn("一次且仅一次创作决策", profile.creative_brief_framework)
        self.assertIn("CreativeBrief 是下游唯一创作总纲", profile.creative_brief_framework)
        self.assertEqual(profile.research_framework, "")
        self.assertEqual(profile.script_framework, "")
        self.assertIn("主体定义", legacy_profile.planning_framework)
        self.assertNotIn("MiniMax", profile.planning_framework)
        self.assertNotIn("Seedance", profile.planning_framework)
        director = default_director_skill_registry.resolve(
            "animated-explainer"
        )
        self.assertIn("具体事件", director.scene_design_rules)
        self.assertIn("blocking", director.shot_design_rules)
        self.assertIn("图片是默认媒介", director.media_rules)
        maximum_shot = StoryboardShot(
            shot_id="shot_01",
            segment_id="n01",
            narration_excerpt="长度预算测试",
            visual_type="video",
            visual_intent="意" * 600,
            first_frame_prompt="帧" * 1800,
            motion_prompt="动" * 1800,
        )
        maximum_prompt = compile_video_prompt(
            "风" * 3200,
            collage.snapshot(),
            profile.snapshot(),
            maximum_shot,
            has_reference_image=False,
        )
        self.assertLessEqual(len(maximum_prompt), 4000)
        self.assertNotIn("视频提示词结构", maximum_prompt)
        self.assertNotIn("风格动作语法", maximum_prompt)
        revision_intent = "动作更克制，只做一次缓慢推进。"
        revised_prompt = compile_video_prompt(
            "风格基线",
            collage.snapshot(),
            profile.snapshot(),
            maximum_shot,
            has_reference_image=False,
            revision_intent=revision_intent,
        )
        self.assertIn(maximum_shot.motion_prompt, revised_prompt)
        self.assertIn(f"本次重点调整：{revision_intent}", revised_prompt)
        self.assertIn("唯一视觉基准", revised_prompt)
        self.assertIn("只生成无声画面", revised_prompt)
        maximum_frame_prompt = compile_first_frame_prompt(
            "风" * 3200,
            collage.snapshot(),
            profile.snapshot(),
            maximum_shot,
            has_reference_image=True,
        )
        self.assertLessEqual(len(maximum_frame_prompt), 2000)
        legacy_request = VisualGenerationRequest(
            request_id="shot_01",
            prompt=maximum_prompt,
        )
        legacy_payload = legacy_request.model_dump(mode="json")
        legacy_payload.pop("model_id")
        legacy_payload.pop("revision_intent")
        self.assertEqual(
            legacy_request.fingerprint(),
            content_hash(legacy_payload),
        )
        revised_request = legacy_request.model_copy(update={
            "revision_intent": revision_intent,
        })
        self.assertNotEqual(
            legacy_request.fingerprint(),
            revised_request.fingerprint(),
        )

        api_request = qijia_api.ShotRegenerationRequest(
            expected_revision=2,
            revision_intent=revision_intent,
        )
        self.assertEqual(api_request.revision_intent, revision_intent)
        with self.assertRaises(ValidationError):
            qijia_api.ShotRegenerationRequest(
                expected_revision=2,
                revision_intent=revision_intent,
                prompt="不得再接受前端直接传 Provider 提示词",
            )

    def test_h3_keeps_brief_style_and_reference_responsibilities_separate(self):
        style = default_visual_style_registry.resolve(
            "paper-collage-explainer"
        ).snapshot()
        profile = default_prompt_writing_profile_registry.resolve(
            "h3-prompt-writing"
        ).snapshot()
        expert_brief = CreativeBrief.model_validate(valid_creative_brief())
        news_brief = CreativeBrief.model_validate({
            **valid_creative_brief(),
            "central_question": "这次发布真正确认了什么？",
            "core_thesis": "只讲已经确认的变化，并保留仍待验证的影响。",
        })

        expert_base = compile_storyboard_base_style(
            style.director_prompt,
            style,
            profile,
            has_reference_image=False,
            creative_brief=expert_brief,
        )
        news_base = compile_storyboard_base_style(
            style.director_prompt,
            style,
            profile,
            has_reference_image=False,
            creative_brief=news_brief,
        )
        self.assertNotEqual(expert_base, news_base)
        self.assertIn(expert_brief.core_thesis, expert_base)
        self.assertIn(news_brief.core_thesis, news_base)
        self.assertNotIn("内容视觉策略", expert_base + news_base)
        self.assertIn("高级编辑纸张拼贴", expert_base)
        self.assertIn("高级编辑纸张拼贴", news_base)

        referenced = compile_storyboard_base_style(
            style.director_prompt,
            style,
            profile,
            has_reference_image=True,
            creative_brief=news_brief,
        )
        self.assertIn("参考图只约束", referenced)
        self.assertIn(news_brief.core_thesis, referenced)
        self.assertIn("高级编辑纸张拼贴", referenced)
        self.assertIn("参考素材已经确定的视觉属性", referenced)

        shot = StoryboardShot(
            shot_id="shot_01",
            segment_id="n01",
            narration_excerpt="测试",
            visual_type="video",
            visual_intent="展示一个可见变化",
            first_frame_prompt="纸艺人物把手放在桌边，积木尚未完成。",
            motion_prompt="手缓慢收回，孩子放稳最后一块积木，镜头轻推后停住。",
        )
        frame_prompt = compile_first_frame_prompt(
            style.director_prompt,
            style,
            profile,
            shot,
            has_reference_image=False,
        )
        video_prompt = compile_video_prompt(
            style.director_prompt,
            style,
            profile,
            shot,
            has_reference_image=False,
        )
        self.assertTrue(frame_prompt.startswith(shot.first_frame_prompt))
        self.assertIn(shot.motion_prompt, video_prompt)
        for meta_label in (
            "H3 编排方法",
            "首帧输出标准",
            "视频提示词结构",
            "风格动作语法",
            "本镜头视觉意图",
        ):
            self.assertNotIn(meta_label, frame_prompt + video_prompt)

    def test_news_topic_is_a_research_request_not_a_verified_news_fact(self):
        card = NewsTopicInput(
            topic="  TERA   LAB  ",
            focus="最近一周的重要公开动态",
        ).to_source_card_input()
        self.assertEqual(card.subject.name, "TERA LAB")
        self.assertEqual(card.content_format.value, "recent_news_briefing")
        self.assertEqual(card.content_domain.value, "technology")
        self.assertEqual(card.sources, [])
        self.assertEqual(card.verified_facts, [])
        self.assertIn("必须先完成联网研究", card.interpretation_boundary[0].text)

    def test_recent_news_brief_accepts_one_timed_traceable_source(self):
        brief = NewsResearchBrief(
            topic="TERA LAB",
            as_of=timestamp(),
            summary="官方发布了一项可核验的新变化。",
            core_tension="已发布能力与长期效果仍需区分。",
            audience_relevance=["用户需要判断当前是否可用。"],
            content_angles=["解释已确认变化和仍待观察的部分。"],
            evidence=[{
                "claim": "官方发布记录确认了本次变化。",
                "source_title": "官方发布记录",
                "source_url": "https://official.example/releases/tera-lab",
                "source_kind": "official",
                "published_at": "2026-08-09",
                "event_at": "2026-08-09",
            }],
        )

        self.assertEqual(len(brief.evidence), 1)

    def test_legacy_research_diagnostics_restore_post_match_counts(self):
        diagnostics = ResearchDiagnostics.model_validate({
            "web_search_requests": 0,
            "citation_count": 12,
            "candidate_evidence_count": 9,
            "accepted_evidence_count": 0,
            "rejected_counts": {"missing_claim": 9},
        })

        self.assertIsNone(diagnostics.web_search_requests)
        self.assertEqual(diagnostics.matched_citation_count, 9)

    def test_person_viewpoint_expands_to_internal_content_boundary(self):
        idea = PersonViewpointInput(
            person_name="阿尔弗雷德·阿德勒",
            viewpoint="真正影响孩子的，是孩子如何理解自己在家庭中的位置。",
        )
        card = idea.to_source_card_input()
        self.assertEqual(card.subject.type, "person")
        self.assertEqual(card.subject.name, idea.person_name)
        self.assertEqual(card.content_format.value, "person_idea_explainer")
        self.assertEqual(card.content_domain.value, "general_knowledge")
        self.assertEqual(
            card.target_audience,
            "关注该人物与观点的普通中文受众",
        )
        self.assertEqual(card.core_idea, idea.viewpoint)
        self.assertEqual(card.sources, [])
        self.assertEqual(card.verified_facts, [])
        self.assertIn("可以结合模型已有知识", card.interpretation_boundary[0].text)
        self.assertIn("不得虚构精确来源", card.interpretation_boundary[0].text)

    def test_creative_request_is_frozen_without_frontend_field_splitting(self):
        original = (
            "黄宗羲，以及“大丈夫行事，论是非不论利害，论顺逆不论成败，"
            "论万世不论一生”这段话。\n我想解释它的出处、真正含义和今天的价值。"
        )
        request = CreativeRequestInput(creative_request=f"  {original}  ")

        card = request.to_source_card_input()

        self.assertEqual(request.creative_request, original)
        self.assertEqual(card.core_idea, original)
        self.assertEqual(card.subject.type, "topic")
        self.assertIn("黄宗羲", card.title)
        self.assertEqual(card.sources, [])
        self.assertEqual(card.verified_facts, [])
        self.assertIn("不会联网核验", card.interpretation_boundary[0].text)
        self.assertIn("模型可以使用其已有的稳定知识", card.interpretation_boundary[0].text)

    def test_unified_creative_request_api_keeps_legacy_routes_compatible(self):
        paths = {route.path for route in qijia_api.api_router.routes}
        prefix = "/api/qijia-video"

        self.assertIn(f"{prefix}/source-cards/creative-request", paths)
        self.assertIn(
            f"{prefix}/source-cards/creative-request-with-reference",
            paths,
        )
        self.assertIn(f"{prefix}/source-cards/idea", paths)
        self.assertIn(f"{prefix}/source-cards/idea-with-reference", paths)
        self.assertNotIn(f"{prefix}/source-cards/news-topic", paths)
        self.assertNotIn(
            f"{prefix}/jobs/{{job_id}}/actions/retry-news-research",
            paths,
        )
        delete_route = next(
            route
            for route in qijia_api.api_router.routes
            if route.path == f"{prefix}/jobs/{{job_id}}"
            and "DELETE" in route.methods
        )
        self.assertIn("DELETE", delete_route.methods)

    def test_script_skill_uses_model_knowledge_from_complete_original_input(self):
        request = CreativeRequestInput(
            creative_request=(
                "黄宗羲，以及“大丈夫行事，论是非不论利害，论顺逆不论成败，"
                "论万世不论一生”这段话。我想解释它的出处、真正含义和今天的价值。"
            ),
        )
        card = SourceCard(
            **request.to_source_card_input().model_dump(mode="json"),
            id="card-huang-zongxi",
            revision=1,
            status="verified",
        )
        skill = default_skill_registry.resolve(
            "explain-expert-view"
        ).snapshot()
        script_skill = default_script_skill_registry.resolve(
            "evidence-led-explainer"
        ).snapshot()

        compiled = compile_script_skill_prompt(
            card,
            content_policy=skill,
            script_skill=script_skill,
            research_brief=None,
            minimum_characters=220,
            maximum_characters=300,
        )

        self.assertIn(request.creative_request, compiled)
        self.assertIn("【唯一 ContextPack（用户提供材料）】", compiled)
        self.assertIn('"external_retrieval": false', compiled)
        self.assertIn("禁止联网搜索、浏览器、检索插件和外部研究工具", compiled)
        self.assertIn("可以使用模型训练中已有的稳定知识", compiled)
        self.assertIn("不得写成已经核验的人物逐字原话", compiled)
        self.assertNotIn("至少执行三个意图不同的查询", compiled)
        self.assertNotIn("openrouter:web_search", compiled)
        self.assertNotIn("H3", compiled)

    def test_verified_research_wording_becomes_a_source_bound_quote(self):
        idea = PersonViewpointInput(
            person_name="黄宗羲",
            viewpoint="论是非不论利害，论万世不论一生。",
        )
        card = SourceCard(
            **idea.to_source_card_input().model_dump(mode="json"),
            id="card-verified-wording",
            revision=1,
            status="verified",
        )
        brief = PersonResearchBrief(
            person_name=idea.person_name,
            viewpoint=idea.viewpoint,
            input_type="attributed_quote",
            research_focus="核验归属、原文与上下文。",
            attribution_status="verified",
            verified_wording=idea.viewpoint,
            attribution_note="可靠来源直接支持该文字与人物归属。",
            source_context="来源提供了理解这段文字所需的上下文。",
            summary="研究已确认归属与基本语境。",
            core_tension="当下利害与长期是非之间的选择。",
            audience_relevance=["帮助普通受众区分原则和即时结果。"],
            content_angles=["先讲出处，再解释三组对举。"],
            evidence=[{
                "claim": "可靠来源收录了该段文字及其上下文。",
                "source_title": "可靠原始资料",
                "source_url": "https://example.edu/huang-zongxi",
                "source_kind": "primary",
                "evidence_type": "attribution",
            }],
            generated_at=timestamp(),
        )

        enriched = QijiaVideoService._card_with_person_research(card, brief)

        self.assertEqual(len(enriched.verified_quotes), 1)
        self.assertEqual(enriched.verified_quotes[0].text, idea.viewpoint)
        self.assertTrue(
            enriched.verified_quotes[0].source_id.startswith("research_source")
        )
        self.assertTrue(any(
            "verified_quote" in item.text
            for item in enriched.interpretation_boundary
        ))

    def test_generation_settings_default_to_semantic_storyboard_and_preserve_legacy(self):
        settings = GenerationSettings(
            script_prompt="测试脚本写法",
            seedance_prompt="测试镜头风格",
        )
        self.assertEqual(settings.image_count, 0)
        self.assertEqual(settings.shot_count, 0)
        self.assertEqual(settings.video_resolution, "1080p")
        self.assertEqual(settings.seedance_model, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(
            settings.tts_voice_id,
            "zh_female_vv_uranus_bigtts",
        )
        self.assertEqual(settings.tts_speed_ratio, 1.2)
        legacy_job = VideoJob.model_validate({
            "id": "legacy-resolution-job",
            "state": "card_verified",
            "source_card_id": "legacy-card",
            "source_card_revision": 1,
            "source_card_snapshot": {},
            "generation_settings": {
                "script_prompt": "旧任务脚本写法",
                "seedance_prompt": "旧任务镜头风格",
                "shot_count": 5,
            },
        })
        self.assertEqual(
            legacy_job.generation_settings.video_resolution,
            "480p",
        )
        self.assertEqual(
            legacy_job.generation_settings.seedance_model,
            SEEDANCE_FLAGSHIP_MODEL,
        )
        self.assertEqual(legacy_job.generation_settings.image_count, 2)
        self.assertEqual(legacy_job.generation_settings.shot_count, 5)
        self.assertEqual(legacy_job.generation_settings.tts_speed_ratio, 1.0)
        self.assertEqual(
            legacy_job.pre_generation_media_mode,
            PreGenerationMediaMode.AUTOMATIC,
        )
        self.assertEqual(legacy_job.generation_settings.skill_id, "")
        self.assertIsNone(legacy_job.skill_snapshot)
        self.assertEqual(
            {
                GenerationSettings(video_resolution=resolution).video_resolution
                for resolution in ("480p", "720p", "1080p")
            },
            {"480p", "720p", "1080p"},
        )
        v2_compatible = GenerationSettings(script_prompt="", seedance_prompt="")
        self.assertEqual(v2_compatible.script_prompt, "")
        self.assertEqual(v2_compatible.seedance_prompt, "")
        with self.assertRaises(ValidationError):
            GenerationSettings(video_resolution="2160p")
        with self.assertRaises(ValidationError):
            GenerationSettings(seedance_model="unknown-seedance")
        one_image_legacy = GenerationSettings(image_count=1)
        self.assertEqual(one_image_legacy.image_count, 1)
        self.assertEqual(one_image_legacy.shot_count, 4)
        with self.assertRaises(ValidationError):
            GenerationSettings(image_count=11)
        with self.assertRaises(ValidationError):
            GenerationSettings(image_count=10, shot_count=5)
        with self.assertRaises(ValidationError):
            GenerationSettings(tts_speed_ratio=1.3)
        with self.assertRaises(ValidationError):
            GenerationSettings(tts_voice_id="unverified-voice")

    def test_legacy_visual_request_fingerprint_does_not_change(self):
        payload = {
            "request_id": "shot_01",
            "prompt": "旧任务镜头",
            "resolution": "480p",
            "ratio": "9:16",
            "duration_seconds": 8,
            "generate_audio": False,
            "seed": None,
            "first_frame_asset_id": "",
        }
        self.assertEqual(
            VisualGenerationRequest.model_validate(payload).fingerprint(),
            content_hash(payload),
        )

    def test_unsubmitted_retired_seedance_requests_migrate_without_touching_paid_tasks(self):
        retired_request = VisualGenerationRequest(
            request_id="shot_01",
            prompt="旧模型未提交成功的镜头",
            model_id=SEEDANCE_RETIRED_MODEL,
            resolution="1080p",
        )
        base_payload = {
            "id": "retired-seedance-job",
            "state": "failed",
            "source_card_id": "card_01",
            "source_card_revision": 1,
            "source_card_snapshot": {},
            "generation_settings": {
                "seedance_model": SEEDANCE_RETIRED_MODEL,
            },
            "visual_requests": [retired_request.model_dump(mode="json")],
        }

        unsubmitted = VideoJob.model_validate(base_payload)
        self.assertEqual(
            unsubmitted.generation_settings.seedance_model,
            SEEDANCE_EFFICIENT_MODEL,
        )
        self.assertEqual(
            unsubmitted.visual_requests[0].model_id,
            SEEDANCE_EFFICIENT_MODEL,
        )

        new_draft = VideoJob(
            **{
                key: value
                for key, value in base_payload.items()
                if key not in {"generation_settings", "visual_requests"}
            },
            generation_settings=GenerationSettings(
                seedance_model=SEEDANCE_RETIRED_MODEL,
            ),
        )
        self.assertEqual(
            new_draft.generation_settings.seedance_model,
            SEEDANCE_EFFICIENT_MODEL,
        )

        submitted_payload = dict(base_payload)
        submitted_payload["video_tasks"] = [{
            "provider": "volcengine-seedance",
            "provider_task_id": "paid_task_01",
            "request_fingerprint": retired_request.fingerprint(),
            "request_id": retired_request.request_id,
            "model_id": SEEDANCE_RETIRED_MODEL,
            "state": "running",
        }]
        submitted = VideoJob.model_validate(submitted_payload)
        self.assertEqual(
            submitted.generation_settings.seedance_model,
            SEEDANCE_RETIRED_MODEL,
        )
        self.assertEqual(
            submitted.visual_requests[0].model_id,
            SEEDANCE_RETIRED_MODEL,
        )

    def test_default_seedance_prompt_is_only_a_visual_style(self):
        prompt = GenerationSettings().seedance_prompt
        self.assertIn("现代编辑插画视觉", prompt)
        self.assertIn("2.5D", prompt)
        self.assertIn("米白", prompt)
        self.assertNotIn("同一组虚构东亚家庭成员", prompt)
        self.assertNotIn("视频镜头", prompt)
        self.assertNotIn("医疗诊断", prompt)

    def test_subtitle_cues_are_normalized_and_limited_to_one_line(self):
        chunks = QijiaVideoService._split_subtitle_text(
            "父母先停一下，\n让孩子把自己的想法完整说出来，再一起讨论下一步。"
        )
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertTrue(all("\n" not in chunk for chunk in chunks))
        self.assertEqual(
            "".join(chunks),
            "父母先停一下，让孩子把自己的想法完整说出来，再一起讨论下一步。",
        )

    def test_default_script_prompt_uses_one_brief_without_visual_direction(self):
        prompt = GenerationSettings().script_prompt
        self.assertIn("唯一的 H3 CreativeBrief", prompt)
        self.assertIn("一个中心问题、一个核心判断、一条完整论证路径", prompt)
        self.assertIn("不得套用预设领域、受众焦虑或平台话术", prompt)
        self.assertIn("只有 verified_quote 可以作为人物逐字原话", prompt)
        self.assertIn("先写完整口播", prompt)
        self.assertIn("脚本不设计逐镜头画面", prompt)
        self.assertIn("后续 H3 Visual Director", prompt)
        self.assertNotIn("降低 2 秒流失率", prompt)
        self.assertNotIn("visual_direction 只写", prompt)
        self.assertNotIn("写成一版面向家长", prompt)

    def test_longest_later_chapters_become_images(self):
        self.assertEqual(
            QijiaVideoService._visual_types_for_durations(
                [7.23, 9.53, 6.87, 10.47, 13.9]
            ),
            ("video", "video", "video", "image", "image"),
        )

    def test_contract_rejects_unknown_schema_version(self):
        with self.assertRaises(ValidationError):
            valid_card(schema_version="2.0")

    def test_source_card_rejects_unknown_source_reference(self):
        with self.assertRaises(ValidationError):
            valid_card(verified_facts=[{
                "id": "fact_01",
                "text": "事实",
                "source_refs": ["missing"],
            }])

    def test_source_card_accepts_only_one_global_reference_image(self):
        image_asset = {
            "asset_id": "reference_image_test",
            "object_key": "qijia-video/reference-images/test.png",
            "sha256": "a" * 64,
            "size_bytes": 128,
            "media_type": "image/png",
        }
        card = valid_card(reference_assets=[image_asset])
        self.assertEqual(card.reference_assets[0]["asset_id"], "reference_image_test")
        with self.assertRaises(ValidationError):
            valid_card(reference_assets=[image_asset, image_asset])
        with self.assertRaises(ValidationError):
            valid_card(reference_assets=[{
                **image_asset,
                "media_type": "video/mp4",
            }])

    def test_quick_source_card_expands_to_traceable_internal_contract(self):
        quick = QuickSourceCardInput.model_validate({
            "title": "孩子遇到困难时，父母应该立刻帮忙吗？",
            "source_material": (
                "https://example.com/article "
                "儿童需要在安全边界内获得持续、可承担的自主练习。"
            ),
            "rights_confirmed": True,
        })
        card = quick.to_source_card_input()
        self.assertEqual(card.subject.type, "concept")
        self.assertEqual(card.parent_question, quick.title)
        self.assertEqual(card.sources[0].url, "https://example.com/article")
        self.assertEqual(card.sources[0].rights_status, "verified_for_citation")
        self.assertTrue(card.sources[0].accessed_at)
        self.assertNotIn("https://", card.verified_facts[0].text)
        self.assertIn("诊断或治疗", card.interpretation_boundary[0].text)

    def test_quick_source_card_requires_content_beyond_a_link_and_confirmation(self):
        with self.assertRaises(ValidationError):
            QuickSourceCardInput.model_validate({
                "title": "一个选题",
                "source_material": "https://example.com/article",
                "rights_confirmed": True,
            })
        with self.assertRaises(ValidationError):
            QuickSourceCardInput.model_validate({
                "title": "一个选题",
                "source_material": "这是一段已经由用户核对过的关键参考内容。",
                "rights_confirmed": False,
            })

    def test_render_manifest_defaults_to_no_embedded_ai_label_or_brand(self):
        audio = {
            "asset_id": "audio",
            "object_key": "audio.wav",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "audio/wav",
        }
        base = {
            "job_id": "job",
            "duration_in_frames": 1350,
            "assets": [audio],
            "audio_asset_id": "audio",
            "visual_blocks": [{
                "id": "v1",
                "type": "title_card",
                "start_frame": 0,
                "duration_in_frames": 1350,
            }],
        }
        manifest = RenderManifest(**base)
        self.assertFalse(manifest.ai_content_label.enabled)
        # Read compatibility for manifests saved before embedded labels were removed.
        legacy = RenderManifest(**base, ai_content_label={"enabled": True})
        self.assertTrue(legacy.ai_content_label.enabled)
        with self.assertRaises(ValidationError):
            RenderManifest(**base, brand_overlay={"text": "齐家 AI"})

    def test_render_manifest_accepts_three_vertical_resolution_pairs(self):
        audio = {
            "asset_id": "audio",
            "object_key": "audio.wav",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "audio/wav",
        }
        base = {
            "job_id": "job",
            "duration_in_frames": 1350,
            "assets": [audio],
            "audio_asset_id": "audio",
            "visual_blocks": [{
                "id": "v1",
                "type": "title_card",
                "start_frame": 0,
                "duration_in_frames": 1350,
            }],
        }
        manifest = RenderManifest(**base)
        self.assertEqual((manifest.width, manifest.height), (480, 854))
        hd = RenderManifest(**base, width=720, height=1280)
        self.assertEqual((hd.width, hd.height), (720, 1280))
        full_hd = RenderManifest(**base, width=1080, height=1920)
        self.assertEqual((full_hd.width, full_hd.height), (1080, 1920))
        with self.assertRaises(ValidationError):
            RenderManifest(**base, width=720, height=1920)

    def test_script_v1_hash_is_stable_while_v2_has_independent_tracks(self):
        old_payload = {
            "schema_version": "1.0",
            "source_card_id": "card-old",
            "source_card_revision": 1,
            "video_title": "旧脚本",
            "cover_text": "旧封面",
            "hook": "第一段",
            "narration_segments": [
                {"id": "n01", "text": "第一段", "segment_type": "hook", "source_refs": ["fact_01"], "quote_ref": None},
                {"id": "n02", "text": "第二段", "segment_type": "explanation", "source_refs": ["fact_01"], "quote_ref": None},
                {"id": "n03", "text": "第三段", "segment_type": "closing", "source_refs": ["fact_01"], "quote_ref": None},
            ],
            "closing": "第三段",
            "estimated_duration_seconds": 45,
            "caption": "旧脚本",
            "hashtags": [],
        }
        restored = ScriptDraft.model_validate(old_payload)
        self.assertEqual(content_hash(restored), content_hash(old_payload))
        self.assertEqual(restored.beats[0].narration, "第一段")
        self.assertEqual(restored.model_dump(mode="json"), old_payload)

        v2_payload = {
            **old_payload,
            "schema_version": "2.0",
            "beats": [
                {
                    "id": f"n{index:02d}",
                    "role": role,
                    "narration": f"旁白 {index}",
                    "visual_direction": f"画面 {index}",
                    "on_screen_text": "重点" if index == 1 else "",
                    "source_refs": ["fact_01"],
                    "quote_ref": None,
                }
                for index, role in enumerate(
                    ["hook", "context", "explanation", "application", "closing"], 1
                )
            ],
        }
        v2_payload.pop("narration_segments")
        current = ScriptDraft.model_validate(v2_payload)
        self.assertIn("beats", current.model_dump(mode="json"))
        self.assertEqual(current.hook, "旁白 1")
        self.assertEqual(current.closing, "旁白 5")

    def test_h3_v3_accepts_three_semantic_beats_and_storyboard_chapters(self):
        roles = ["hook", "explanation", "closing"]
        script = ScriptDraft.model_validate({
            "schema_version": "3.0",
            "source_card_id": "card-semantic",
            "source_card_revision": 1,
            "video_title": "只需要三次语义推进的脚本",
            "cover_text": "不为凑数切段",
            "hook": "兼容字段",
            "beats": [
                {
                    "id": f"n{index:02d}",
                    "role": role,
                    "narration": f"第 {index} 段完成一次真实的语义推进。",
                    "on_screen_text": "",
                    "source_refs": [],
                    "quote_ref": None,
                }
                for index, role in enumerate(roles, 1)
            ],
            "closing": "兼容字段",
            "estimated_duration_seconds": 45,
            "caption": "三段已经完整，不额外拆分。",
            "hashtags": ["语义结构"],
        })

        QijiaVideoService._validate_generated_script_length(script)
        plan = StoryboardPlan(
            shots=[
                StoryboardShot(
                    shot_id=f"shot_{index:02d}",
                    segment_id=beat.id,
                    beat_ids=[beat.id],
                    narration_excerpt=beat.narration,
                    visual_type="image",
                    visual_intent=f"承载第 {index} 次语义推进",
                    first_frame_prompt="主体关系清楚的竖屏编辑插画，画面无文字",
                    motion_prompt="后期缓慢推进取景",
                )
                for index, beat in enumerate(script.beats, 1)
            ],
            input_hash="a" * 64,
            created_at=timestamp(),
        )

        self.assertEqual(len(script.beats), 3)
        self.assertEqual(len(plan.shots), 3)

    def test_storyboard_v3_contract_rejects_non_executable_direction(self):
        def shot(index, *, visual_type='image'):
            return {
                'shot_id': f'shot_{index:02d}',
                'segment_id': f'n{index:02d}',
                'beat_ids': [f'n{index:02d}'],
                'narration_excerpt': f'第 {index} 段旁白',
                'visual_type': visual_type,
                'context': {
                    'semantic_goal': f'完成第 {index} 次语义推进',
                    'concrete_event': f'人物在场景 {index} 完成动作并得到反馈',
                    'blocking': f'人物从画面左侧走到位置 {index}',
                    'subject': '同一位主人公',
                    'action': f'完成动作 {index}',
                    'environment': f'场景 {index}',
                    'composition': '竖屏中景，行动路径清楚',
                    'continuity_handoff': '服装与随身物件保持一致',
                    'start_state': f'动作 {index} 尚未开始',
                    'end_state': f'动作 {index} 已完成并出现反馈',
                    'camera_intent': '固定中景跟随一次主体移动',
                    'media_rationale': '静态画面足以表达事件结果',
                },
            }

        payload = {
            'schema_version': '3.0',
            'shots': [shot(index) for index in range(1, 4)],
            'input_hash': 'a' * 64,
            'created_at': timestamp(),
        }
        plan = StoryboardPlan.model_validate(payload)
        self.assertEqual(len(plan.shots), 3)

        duplicate_event = json.loads(json.dumps(payload))
        duplicate_event['shots'][1]['context']['concrete_event'] = (
            duplicate_event['shots'][0]['context']['concrete_event']
        )
        with self.assertRaises(ValidationError):
            StoryboardPlan.model_validate(duplicate_event)

        unchanged_state = json.loads(json.dumps(payload))
        unchanged_state['shots'][0]['context']['end_state'] = (
            unchanged_state['shots'][0]['context']['start_state']
        )
        with self.assertRaises(ValidationError):
            StoryboardPlan.model_validate(unchanged_state)

        too_many_videos = {
            **payload,
            'shots': [shot(index, visual_type='video') for index in range(1, 5)],
        }
        with self.assertRaises(ValidationError):
            StoryboardPlan.model_validate(too_many_videos)


class SeedanceProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_status_and_bounded_download_use_official_task_shape(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["model"], SEEDANCE_EFFICIENT_MODEL)
                self.assertEqual(body["resolution"], "480p")
                self.assertEqual(body["ratio"], "9:16")
                self.assertEqual(body["duration"], 8)
                self.assertFalse(body["generate_audio"])
                self.assertFalse(body["watermark"])
                self.assertEqual(body["content"][1], {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://media.volces.com/frame.jpg",
                    },
                    "role": "first_frame",
                })
                return httpx.Response(
                    200, json={"id": "cgt-1", "status": "queued"}
                )
            if request.url.host == "media.volces.com":
                return httpx.Response(
                    200,
                    content=b"video-bytes",
                    headers={
                        "content-type": "video/mp4",
                        "content-length": "11",
                    },
                )
            return httpx.Response(200, json={
                "id": "cgt-1",
                "model": SEEDANCE_EFFICIENT_MODEL,
                "status": "succeeded",
                "content": {"video_url": "https://media.volces.com/result.mp4"},
                "usage": {"total_tokens": 40500},
            })

        provider = SeedanceVideoProvider(
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        request = VisualGenerationRequest(
            request_id="shot_01",
            prompt="克制的家庭生活场景",
            model_id=SEEDANCE_EFFICIENT_MODEL,
            first_frame_asset_id="first_frame_frame_shot_01_01",
        )
        fingerprint = request.fingerprint()
        submitted = await provider.submit(
            request,
            first_frame_url="https://media.volces.com/frame.jpg",
        )
        self.assertEqual(submitted.state, ProviderTaskState.QUEUED)
        self.assertEqual(submitted.model_id, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(submitted.request_fingerprint, fingerprint)
        status = await provider.get_status("cgt-1", request.fingerprint())
        self.assertEqual(status.state, ProviderTaskState.SUCCEEDED)
        self.assertEqual(status.model_id, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(status.usage_total_tokens, 40500)
        with tempfile.TemporaryDirectory() as directory:
            output = await provider.download("cgt-1", Path(directory) / "shot.mp4")
            self.assertEqual(output.read_bytes(), b"video-bytes")
        self.assertEqual(sum(item.method == "POST" for item in requests), 1)
        media_request = next(
            item for item in requests if item.url.host == "media.volces.com"
        )
        self.assertNotIn("authorization", media_request.headers)

    async def test_ambiguous_submit_failure_is_not_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("unknown", request=request)

        provider = SeedanceVideoProvider(
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(Exception, "避免重复扣费"):
            await provider.submit(VisualGenerationRequest(
                request_id="shot_01", prompt="测试镜头"
            ))
        self.assertEqual(calls, 1)


class SeedreamProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_and_download_use_official_image_shape(self):
        requests = []
        image_bytes = b"\x89PNG\r\n\x1a\nfixture"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(
                    body["model"], "doubao-seedream-5-0-lite-260128"
                )
                self.assertEqual(body["size"], "1440x2560")
                self.assertEqual(body["seed"], 42)
                self.assertEqual(body["sequential_image_generation"], "disabled")
                self.assertEqual(body["response_format"], "url")
                self.assertFalse(body["watermark"])
                self.assertEqual(
                    body["image"],
                    ["https://private.volces.com/reference.png"],
                )
                return httpx.Response(200, json={
                    "model": "doubao-seedream-5-0-lite-260128",
                    "data": [{
                        "url": "https://media.volces.com/frame.png",
                        "size": "1440x2560",
                    }],
                    "usage": {"total_tokens": 1234},
                })
            return httpx.Response(
                200,
                content=image_bytes,
                headers={
                    "content-type": "image/png",
                    "content-length": str(len(image_bytes)),
                },
            )

        provider = SeedreamImageProvider(
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            size="1440x2560",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        generated = await provider.generate(
            "竖屏动画首帧",
            seed=42,
            reference_image_url="https://private.volces.com/reference.png",
        )
        self.assertEqual(generated.usage_total_tokens, 1234)
        with tempfile.TemporaryDirectory() as directory:
            output = await provider.download(
                generated.url, Path(directory) / "frame.png"
            )
            self.assertEqual(output.read_bytes(), image_bytes)
        self.assertEqual(sum(item.method == "POST" for item in requests), 1)
        media_request = next(
            item for item in requests if item.url.host == "media.volces.com"
        )
        self.assertNotIn("authorization", media_request.headers)

    async def test_ambiguous_image_submit_is_not_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("unknown", request=request)

        provider = SeedreamImageProvider(
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            size="1440x2560",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderUnavailable, "避免重复扣费"):
            await provider.generate("测试首帧", seed=1)
        self.assertEqual(calls, 1)

    async def test_seedream_rejects_seed_outside_signed_int32_before_submit(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        provider = SeedreamImageProvider(
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            size="1440x2560",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderUnavailable, "int32"):
            await provider.generate(
                "测试首帧", seed=SEEDREAM_MAX_SEED + 1
            )
        self.assertEqual(calls, 0)


class RealProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_packager_normalizes_audio_without_reencoding_video(self):
        packager = FfmpegMediaPackager()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rendered.mp4"
            destination = root / "ready.mp4"
            source.write_bytes(b"rendered")

            async def fake_run(*args, **kwargs):
                del kwargs
                Path(args[-1]).write_bytes(b"remuxed")
                return "ok"

            runner = AsyncMock(side_effect=fake_run)
            with (
                patch(
                    "qijia_video.infrastructure.media.shutil.which",
                    return_value="ffmpeg",
                ),
                patch.object(packager, "_run", runner),
            ):
                result = await packager.normalize(source, destination)

        self.assertEqual(result, destination)
        arguments = runner.await_args.args
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "copy")
        self.assertEqual(
            arguments[arguments.index("-af") + 1],
            "loudnorm=I=-16:TP=-1.5:LRA=7",
        )
        self.assertEqual(arguments[arguments.index("-c:a") + 1], "aac")
        self.assertEqual(arguments[arguments.index("-b:a") + 1], "192k")
        self.assertEqual(arguments[arguments.index("-ar") + 1], "48000")
        self.assertIn("+faststart", arguments)
        self.assertNotIn("libx264", arguments)

    async def test_media_packager_holds_last_frame_instead_of_slowing_video(self):
        packager = FfmpegMediaPackager()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "seedance.mp4"
            destination = root / "timeline.mp4"
            source.write_bytes(b"seedance")

            async def fake_run(*args, **kwargs):
                del kwargs
                Path(args[-1]).write_bytes(b"prepared")
                return "ok"

            runner = AsyncMock(side_effect=fake_run)
            with (
                patch(
                    "qijia_video.infrastructure.media.shutil.which",
                    return_value="ffmpeg",
                ),
                patch.object(
                    packager,
                    "_probe_duration",
                    AsyncMock(side_effect=[8.0, 13.95]),
                ),
                patch.object(packager, "_run", runner),
            ):
                result, duration = await packager.prepare_video_for_timeline(
                    source,
                    destination,
                    minimum_duration_seconds=13.9,
                )

        self.assertEqual(result, destination)
        self.assertEqual(duration, 13.95)
        arguments = runner.await_args.args
        self.assertIn("tpad=stop_mode=clone", " ".join(arguments))
        self.assertIn("libx264", arguments)
        self.assertNotIn("setpts", " ".join(arguments))

    async def test_media_packager_normalizes_uploaded_video_for_timeline(self):
        packager = FfmpegMediaPackager()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "editor.webm"
            destination = root / "timeline.mp4"
            source.write_bytes(b"editor-video")

            async def fake_run(*args, **kwargs):
                del kwargs
                Path(args[-1]).write_bytes(b"prepared")
                return "ok"

            runner = AsyncMock(side_effect=fake_run)
            with (
                patch(
                    "qijia_video.infrastructure.media.shutil.which",
                    return_value="ffmpeg",
                ),
                patch.object(
                    packager,
                    "_probe_duration",
                    AsyncMock(side_effect=[2.0, 6.05]),
                ),
                patch.object(packager, "_run", runner),
            ):
                result, duration = (
                    await packager.prepare_uploaded_video_for_timeline(
                        source,
                        destination,
                        chapter_duration_seconds=6.0,
                    )
                )

        self.assertEqual(result, destination)
        self.assertEqual(duration, 6.05)
        arguments = runner.await_args.args
        self.assertEqual(arguments[arguments.index("-map") + 1], "0:v:0")
        self.assertIn("-an", arguments)
        self.assertIn("-sn", arguments)
        self.assertIn("-dn", arguments)
        video_filter = arguments[arguments.index("-vf") + 1]
        self.assertIn("scale=1080:-2", video_filter)
        self.assertIn("fps=30", video_filter)
        self.assertIn("tpad=stop_mode=clone", video_filter)
        self.assertNotIn("setpts", video_filter)
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "libx264")
        self.assertEqual(arguments[arguments.index("-pix_fmt") + 1], "yuv420p")
        self.assertIn("+faststart", arguments)

    async def test_tts_limit_fallback_uses_minimum_chunks(self):
        texts = ["甲" * 200 + "。", "乙" * 200 + "。"]
        chunks = VolcengineTtsProvider._synthesis_chunks(texts)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(item.encode("utf-8")) <= 1000 for item in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), "".join(texts))

    async def test_unconfigured_tos_is_reportable_without_startup_failure(self):
        storage = TosArtifactStorage(
            access_key_id="",
            secret_access_key="",
            bucket="",
            region="cn-shanghai",
        )
        self.assertFalse(storage.configured)
        with self.assertRaises(ProviderUnavailable):
            storage._client()

    async def test_tos_allows_slow_video_transfers_and_keeps_sdk_retries(self):
        storage = TosArtifactStorage(
            access_key_id="test-ak",
            secret_access_key="test-sk",
            bucket="test-bucket",
            region="cn-shanghai",
        )
        with patch("tos.TosClientV2") as client_class:
            storage._client()
        kwargs = client_class.call_args.kwargs
        self.assertEqual(kwargs["max_retry_count"], 3)
        self.assertEqual(kwargs["socket_timeout"], TOS_SOCKET_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["request_timeout"], TOS_SOCKET_TIMEOUT_SECONDS)

        with patch("tos.TosClientV2") as client_class:
            storage._control_client()
        control_kwargs = client_class.call_args.kwargs
        self.assertEqual(control_kwargs["max_retry_count"], 0)
        self.assertEqual(
            control_kwargs["socket_timeout"],
            TOS_CONTROL_TIMEOUT_SECONDS,
        )

    async def test_tos_direct_upload_signs_and_verifies_exact_object_metadata(self):
        storage = TosArtifactStorage(
            access_key_id="test-ak",
            secret_access_key="test-sk",
            bucket="test-bucket",
            region="cn-shanghai",
        )
        digest = hashlib.sha256(b"uploaded-video").hexdigest()

        class Signed:
            signed_url = "https://test-bucket.tos-cn-shanghai.volces.com/staged.mp4"
            signed_header = {
                "host": "test-bucket.tos-cn-shanghai.volces.com",
                "Content-Type": "video/mp4",
                "x-tos-meta-asset-id": "raw_upload_01",
                "x-tos-meta-sha256": digest,
                "x-tos-meta-size-bytes": "14",
            }

        class Head:
            content_type = "video/mp4"
            content_length = 14
            meta = {
                "asset-id": "raw_upload_01",
                "sha256": digest,
                "size-bytes": "14",
            }

        class Client:
            def __init__(self):
                self.presign_args = None
                self.presign_kwargs = None

            def pre_signed_url(self, *args, **kwargs):
                self.presign_args = args
                self.presign_kwargs = kwargs
                return Signed()

            def head_object(self, bucket, key):
                self.head = (bucket, key)
                return Head()

        client = Client()
        with patch.object(storage, "_client", return_value=client):
            grant = await storage.create_direct_upload(
                object_key="qijia-video/staged-uploads/job/shot/upload.mp4",
                asset_id="raw_upload_01",
                media_type="video/mp4",
                sha256=digest,
                size_bytes=14,
            )
            asset = await storage.complete_direct_upload(
                object_key="qijia-video/staged-uploads/job/shot/upload.mp4",
                asset_id="raw_upload_01",
                media_type="video/mp4",
                sha256=digest,
                size_bytes=14,
            )

        self.assertEqual(grant["method"], "PUT")
        self.assertNotIn("host", {
            name.lower(): value for name, value in grant["headers"].items()
        })
        self.assertTrue(client.presign_kwargs["is_signed_all_headers"])
        self.assertEqual(
            client.presign_kwargs["header"]["x-tos-meta-sha256"],
            digest,
        )
        self.assertEqual(asset.sha256, digest)
        self.assertEqual(asset.size_bytes, 14)
        self.assertEqual(
            client.head,
            (
                "test-bucket",
                "qijia-video/staged-uploads/job/shot/upload.mp4",
            ),
        )

    async def test_tos_direct_upload_rejects_tampered_size(self):
        storage = TosArtifactStorage(
            access_key_id="test-ak",
            secret_access_key="test-sk",
            bucket="test-bucket",
            region="cn-shanghai",
        )
        digest = hashlib.sha256(b"uploaded-video").hexdigest()

        class Head:
            content_type = "video/mp4"
            content_length = 13
            meta = {
                "asset-id": "raw_upload_01",
                "sha256": digest,
                "size-bytes": "14",
            }

        client = type("Client", (), {
            "head_object": lambda self, bucket, key: Head(),
        })()
        with patch.object(storage, "_client", return_value=client):
            with self.assertRaisesRegex(QualityGateFailed, "大小校验失败"):
                await storage.complete_direct_upload(
                    object_key="qijia-video/staged-uploads/job/shot/upload.mp4",
                    asset_id="raw_upload_01",
                    media_type="video/mp4",
                    sha256=digest,
                    size_bytes=14,
                )

    async def test_tos_download_resumes_with_checkpoint_after_read_timeouts(self):
        payload = b"seedance-video" * 1024
        asset = AssetRef(
            asset_id="visual_shot_01",
            object_key="qijia-video/job/video/shot_01.mp4",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="video/mp4",
        )
        storage = TosArtifactStorage(
            access_key_id="test-ak",
            secret_access_key="test-sk",
            bucket="test-bucket",
            region="cn-shanghai",
        )

        class FlakyDownloadClient:
            def __init__(self):
                self.calls: list[dict] = []

            def download_file(self, bucket, key, file_path, **kwargs):
                self.calls.append({
                    "bucket": bucket,
                    "key": key,
                    "file_path": file_path,
                    **kwargs,
                })
                if len(self.calls) < TOS_DOWNLOAD_ATTEMPTS:
                    raise TimeoutError("Read timed out")
                Path(file_path).write_bytes(payload)

        client = FlakyDownloadClient()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            storage, "_client", return_value=client
        ), patch(
            "qijia_video.infrastructure.storage.asyncio.sleep",
            new=AsyncMock(),
        ):
            destination = Path(directory) / "shot_01.mp4"
            result = await storage.materialize(asset, destination)
            downloaded = result.read_bytes()

        self.assertEqual(downloaded, payload)
        self.assertEqual(len(client.calls), TOS_DOWNLOAD_ATTEMPTS)
        self.assertTrue(all(
            item["enable_checkpoint"] is True
            and item["part_size"] == TOS_DOWNLOAD_PART_SIZE_BYTES
            and item["task_num"] == TOS_DOWNLOAD_TASK_COUNT
            for item in client.calls
        ))

    def test_reference_normalization_does_not_hide_ambiguous_or_unknown_ids(self):
        card = SourceCard(
            **valid_card(verified_facts=[
                {
                    "id": "fact_01",
                    "text": "第一条事实",
                    "source_refs": ["source_01"],
                },
                {
                    "id": "fact_02",
                    "text": "第二条事实",
                    "source_refs": ["source_01"],
                },
            ]).model_dump(mode="json"),
            id="card-ambiguous-reference",
            revision=1,
            status="verified",
        )
        generated = {
            "beats": [{
                "source_refs": ["source_01", "invented_01"],
                "quote_ref": None,
            }]
        }

        OpenRouterScriptProvider._normalize_generated_source_refs(card, generated)

        self.assertEqual(
            generated["beats"][0]["source_refs"],
            ["source_01", "invented_01"],
        )

    async def test_openrouter_generates_contract_in_exactly_one_call(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            generated = {
                "creative_brief": valid_creative_brief(
                    evidence_refs=["fact_01"]
                ),
                "schema_version": "3.0",
                "video_title": "帮助之前，先给孩子一点尝试空间",
                "cover_text": "别急着替孩子完成",
                "beats": [
                    {
                        "id": f"n{index:02d}",
                        "narration": text.ljust(40, "。"),
                        "role": kind,
                        "on_screen_text": "" if index > 1 else "先别急着出手",
                        # Models sometimes confuse the source record with its
                        # single supported fact. The provider must repair this
                        # deterministic metadata without another model call.
                        "source_refs": (
                            ["source_01"] if index == 3
                            else ["fact_01"] if index == 4
                            else []
                        ),
                        "quote_ref": None,
                    }
                    for index, (kind, text) in enumerate([
                        ("hook", "孩子卡住时，父母越快出手就越好吗？"),
                        ("context", "帮助的关键，不只是眼前有没有完成。"),
                        ("explanation", "能力需要在安全边界内通过练习逐渐形成。"),
                        ("application", "先判断风险，再给孩子一小段可以承担的尝试时间。"),
                        ("closing", "今天先少替孩子完成一步，多观察一步。"),
                    ], 1)
                ],
                "caption": "帮助孩子，不等于替孩子完成。",
                "hashtags": ["家庭教育", "家长成长", "亲子沟通"],
            }
            return httpx.Response(200, json={
                "id": "generation-script-1",
                "model": "resolved/test-model",
                "choices": [{"message": {"content": json.dumps(
                    generated, ensure_ascii=False
                )}}],
                "usage": {
                    "prompt_tokens": 800,
                    "completion_tokens": 200,
                    "total_tokens": 1000,
                    "cost": 0.0123,
                    "prompt_tokens_details": {"cached_tokens": 120},
                    "completion_tokens_details": {"reasoning_tokens": 40},
                },
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-1",
            revision=2,
            status="verified",
        )
        usage_records: list[ProviderUsageRecord] = []

        async def record_usage(usage: ProviderUsageRecord) -> None:
            usage_records.append(usage)

        brief, script = await provider.generate_with_brief(
            card, on_usage=record_usage
        )
        review = await provider.review(card, script)
        self.assertEqual(len(calls), 1)
        request_body = json.loads(calls[0].content)
        response_format = request_body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(
            response_format["json_schema"]["name"],
            "qijia_script_draft_v3",
        )
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertFalse(
            response_format["json_schema"]["schema"]["additionalProperties"]
        )
        self.assertEqual(
            request_body["plugins"], [{"id": "response-healing"}]
        )
        self.assertNotIn("tools", request_body)
        self.assertNotIn("tool_choice", request_body)
        self.assertNotIn("max_tool_calls", request_body)
        self.assertTrue(request_body["provider"]["require_parameters"])
        self.assertEqual(
            request_body["reasoning"]["effort"],
            OPENROUTER_REASONING_EFFORT,
        )
        self.assertTrue(request_body["reasoning"]["exclude"])
        self.assertEqual(
            request_body["max_tokens"],
            SCRIPT_MAX_COMPLETION_TOKENS,
        )
        self.assertNotIn("max_completion_tokens", request_body)
        self.assertNotIn("temperature", request_body)
        prompt = request_body["messages"][1]["content"]
        self.assertIn("唯一的 H3 Creative Director", prompt)
        self.assertIn("【不可变原始输入】", prompt)
        self.assertIn("【唯一 EvidencePack】", prompt)
        self.assertIn("【H3 CreativeBrief 方法】", prompt)
        self.assertIn("不为凑数量切碎论证", prompt)
        self.assertIn("最少 3 段、最多 12 段", prompt)
        self.assertIn("脚本阶段不写 visual_direction", prompt)
        self.assertIn("事实主张或直接引语", prompt)
        self.assertIn('"title": "测试资料"', prompt)
        self.assertEqual(brief.core_thesis, "先核验依据与语境，再理解观点本身。")
        self.assertEqual(brief.evidence_refs, ["fact_01"])
        self.assertEqual(script.schema_version, "3.0")
        self.assertEqual(script.source_card_id, card.id)
        self.assertEqual(script.beats[0].source_refs, [])
        self.assertEqual(script.beats[2].source_refs, ["fact_01"])
        self.assertEqual(script.beats[3].source_refs, ["fact_01"])
        self.assertEqual(script.hook, script.narration_segments[0].text)
        self.assertEqual(script.closing, script.narration_segments[-1].text)
        self.assertEqual(narration_char_count(script.narration_text()), 200)
        self.assertTrue(review.passed)
        self.assertEqual(review.input_hash, content_hash(script))
        self.assertEqual(review.prompt_version, SCRIPT_PROMPT_VERSION)
        self.assertEqual(len(usage_records), 1)
        self.assertTrue(usage_records[0].succeeded)
        self.assertEqual(usage_records[0].request_id, "generation-script-1")
        self.assertEqual(usage_records[0].model_id, "resolved/test-model")
        self.assertEqual(usage_records[0].total_tokens, 1000)
        self.assertEqual(usage_records[0].cached_tokens, 120)
        self.assertEqual(usage_records[0].reasoning_tokens, 40)
        self.assertEqual(usage_records[0].reported_currency, "USD")
        self.assertAlmostEqual(usage_records[0].reported_cost, 0.0123)
        custom_prompt = provider._prompt(card, "只用短句，语气平静。")
        self.assertTrue(custom_prompt.startswith("只用短句，语气平静。"))
        self.assertIn("【系统输出格式】", custom_prompt)
        self.assertNotIn("【本次来源卡】", custom_prompt)

    async def test_openrouter_reports_truncated_json_with_the_exact_stage(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"x-request-id": "req-truncated-script"},
                json={
                    "choices": [{
                        "message": {
                            "content": '{"schema_version":"2.0","beats":['
                        },
                        "finish_reason": "length",
                    }]
                },
            )

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-truncated-script",
            revision=1,
            status="verified",
        )

        with self.assertRaisesRegex(
            ProviderUnavailable,
            "OpenRouter 脚本生成输出被截断.*req-truncated-script",
        ):
            await provider.generate(card)

    async def test_openrouter_keeps_a_complete_over_target_script_in_one_call(self):
        calls: list[httpx.Request] = []

        def generated(segment_text: str) -> dict:
            kinds = ["hook", "context", "explanation", "application", "closing"]
            return {
                "creative_brief": valid_creative_brief(
                    evidence_refs=["fact_01"]
                ),
                "schema_version": "3.0",
                "video_title": "给孩子留出尝试空间",
                "cover_text": "帮助不等于替代",
                "beats": [
                    {
                        "id": f"n{index:02d}",
                        "narration": segment_text,
                        "role": kind,
                        "on_screen_text": "",
                        "source_refs": ["fact_01"],
                        "quote_ref": None,
                    }
                    for index, kind in enumerate(kinds, 1)
                ],
                "caption": "给孩子留出尝试空间。",
                "hashtags": ["家庭教育", "亲子沟通", "家长成长"],
            }

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            result = generated("长" * 80)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(
                    result, ensure_ascii=False
                )}}]
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-long-script",
            revision=1,
            status="verified",
        )
        script = await provider.generate(card)

        self.assertEqual(len(calls), 1)
        self.assertEqual(narration_char_count(script.narration_text()), 400)
        self.assertEqual(script.estimated_duration_seconds, 75)

    async def test_openrouter_keeps_a_concise_script_in_one_call(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-short-script",
            revision=1,
            status="verified",
        )
        calls: list[httpx.Request] = []

        def generated(segment_text: str) -> dict:
            kinds = ["hook", "context", "explanation", "application", "closing"]
            return {
                "creative_brief": valid_creative_brief(
                    evidence_refs=["fact_01"]
                ),
                "schema_version": "3.0",
                "video_title": "给孩子留出尝试空间",
                "cover_text": "帮助不等于替代",
                "beats": [
                    {
                        "id": f"n{index:02d}",
                        "narration": segment_text,
                        "role": kind,
                        "on_screen_text": "",
                        "source_refs": ["fact_01"],
                        "quote_ref": None,
                    }
                    for index, kind in enumerate(kinds, 1)
                ],
                "caption": "给孩子留出尝试空间。",
                "hashtags": ["家庭教育", "亲子沟通", "家长成长"],
            }

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            result = generated("短" * 20)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(
                    result, ensure_ascii=False
                )}}]
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        script = await provider.generate(card)

        self.assertEqual(len(calls), 1)
        self.assertEqual(narration_char_count(script.narration_text()), 100)

    async def test_openrouter_storyboard_keeps_structured_shot_ids(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-storyboard",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        target_ids = [item.id for item in script.narration_segments]
        beat_groups = [[item] for item in target_ids]
        calls = []

        def storyboard_handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(request.content)
            self.assertEqual(
                payload["reasoning"]["effort"],
                OPENROUTER_REASONING_EFFORT,
            )
            self.assertEqual(
                payload["max_tokens"],
                STORYBOARD_MAX_COMPLETION_TOKENS,
            )
            prompt = payload["messages"][1]["content"]
            self.assertIn("first_frame_prompt", prompt)
            self.assertIn("motion_prompt", prompt)
            self.assertIn("不先用空镜", prompt)
            self.assertIn("连续的竖屏视觉叙事", prompt)
            self.assertIn("完整旁白与冻结的 CreativeBrief", prompt)
            self.assertNotIn("前 2 秒", prompt)
            self.assertNotIn("前 5 秒", prompt)
            self.assertNotIn("竖屏家庭微故事", prompt)
            self.assertNotIn("所有章节使用同一组虚构东亚家庭成员", prompt)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "shots": [
                        {
                            "segment_id": segment_id,
                            "visual_type": [
                                "video", "image", "image", "video", "video"
                            ][index - 1],
                            "visual_intent": f"意图 {index}",
                            "first_frame_prompt": f"首帧 {index}",
                            "motion_prompt": f"动作 {index}",
                        }
                        for index, segment_id in enumerate(target_ids, 1)
                    ],
                }, ensure_ascii=False)}}],
            })

        storyboard_provider = OpenRouterStoryboardProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/storyboard",
            transport=httpx.MockTransport(storyboard_handler),
        )
        plan = await storyboard_provider.generate(
            script,
            "统一编辑插画风格",
            beat_groups,
            ["video", "image", "image", "video", "video"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [item.shot_id for item in plan.shots],
            [f"shot_{index:02d}" for index in range(1, 6)],
        )
        self.assertEqual(
            [item.narration_excerpt for item in plan.shots],
            [item.text for item in script.narration_segments],
        )

    async def test_openrouter_director_v3_owns_chapters_and_concrete_events(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-director-v3",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        beat_ids = [item.id for item in script.beats]
        durations = {item.id: 6.0 for item in script.beats}
        groups = [[beat_ids[0]], beat_ids[1:3], beat_ids[3:]]
        calls: list[httpx.Request] = []

        def context(index: int) -> dict:
            return {
                "semantic_goal": f"解释第 {index} 章",
                "concrete_event": (
                    f"第 {index} 章的主体在书房移动一件关键物品，"
                    "同伴立即调整位置，桌面关系形成新的可见结果。"
                ),
                "blocking": (
                    "主体位于画面左侧中景，沿桌边向右移动物品；"
                    "同伴在右后方退让一步，物品停在两人之间。"
                ),
                "visual_metaphor": "",
                "subject": "同一位成年人、同伴与关键物品",
                "action": "成年人移动物品，同伴调整站位并作出反馈",
                "environment": "门窗与桌面位置固定的安静书房",
                "composition": "竖屏中景，人物分居两侧，桌面是视觉焦点",
                "continuity_handoff": (
                    "开场建立人物关系" if index == 1 else "承接上一章结果"
                ),
                "start_state": f"第 {index} 章行动尚未发生",
                "end_state": f"第 {index} 章行动结果已经可见",
                "camera_intent": "固定胸口高度机位，轻微向右跟随并停在桌面关系",
                "media_rationale": (
                    "连续调度不可由静态关系替代"
                    if index == 1 else "静态决定性瞬间足以表达关系"
                ),
                "reference_roles": [],
            }

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(request.content)
            prompt = payload["messages"][1]["content"]
            self.assertIn("真实 TTS 时长", prompt)
            self.assertIn('"duration_seconds": 6.0', prompt)
            schema = payload["response_format"]["json_schema"]["schema"]
            context_schema = schema["properties"]["shots"]["items"][
                "properties"
            ]["context"]
            self.assertIn("concrete_event", context_schema["required"])
            self.assertIn("blocking", context_schema["required"])
            self.assertNotIn(
                "first_frame_prompt",
                schema["properties"]["shots"]["items"]["properties"],
            )
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "visual_bible": {
                        "core_visual_idea": "用物品位置变化解释选择与后果。",
                        "visual_world": "统一的现代编辑插画书房。",
                        "recurring_subjects": ["成年人", "同伴", "关键物品"],
                        "scene_anchors": ["固定书桌", "右侧窗光"],
                        "continuity_rules": ["人物身份稳定", "物品位置承接"],
                        "color_material_system": "低饱和纸张色与稳定侧光。",
                        "composition_system": "竖屏中景，桌面关系优先。",
                        "reference_strategy": "无参考图，不声明参考角色。",
                        "forbidden_elements": ["可读文字", "漂浮符号"],
                    },
                    "shots": [
                        {
                            "beat_ids": group,
                            "visual_type": "video" if index == 1 else "image",
                            "context": context(index),
                        }
                        for index, group in enumerate(groups, 1)
                    ],
                }, ensure_ascii=False)}}],
            })

        provider = OpenRouterStoryboardProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/director",
            transport=httpx.MockTransport(handler),
        )
        bible, plan = await provider.generate_director_plan(
            script,
            "【导演方法】动画解说导演",
            durations,
            director_skill_id="animated-explainer",
            director_skill_version="1.0.0",
            input_hash="a" * 64,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(plan.schema_version, "3.0")
        self.assertEqual([shot.beat_ids for shot in plan.shots], groups)
        self.assertEqual(
            [shot.visual_type for shot in plan.shots],
            ["video", "image", "image"],
        )
        self.assertTrue(all(
            shot.context.concrete_event and shot.context.blocking
            for shot in plan.shots
        ))
        self.assertTrue(all(
            shot.context.visual_metaphor == "" for shot in plan.shots
        ))
        self.assertEqual(bible.director_skill_id, "animated-explainer")
        self.assertEqual(bible.input_hash, "a" * 64)

    async def test_storyboard_normalizes_a_missing_shot_without_second_call(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-storyboard-repair",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        target_ids = [item.id for item in script.narration_segments]
        beat_groups = [[item] for item in target_ids]
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(request.content)
            prompt = payload["messages"][1]["content"]
            for segment_id in target_ids:
                self.assertIn(f'"segment_id": "{segment_id}"', prompt)
            returned_ids = [
                target_ids[0], target_ids[1], target_ids[3], target_ids[4]
            ]
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "shots": [
                        {
                            "segment_id": segment_id,
                            "visual_intent": f"模型意图 {segment_id}",
                            "first_frame_prompt": f"模型首帧 {segment_id}",
                            "motion_prompt": f"模型动作 {segment_id}",
                        }
                        for segment_id in returned_ids
                    ],
                }, ensure_ascii=False)}}],
            })

        provider = OpenRouterStoryboardProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/storyboard",
            transport=httpx.MockTransport(handler),
        )
        plan = await provider.generate(
            script,
            "统一编辑插画风格",
            beat_groups,
            ["video", "image", "image", "video", "video"],
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(plan.shots), 5)
        self.assertEqual(
            [item.segment_id for item in plan.shots], target_ids
        )
        self.assertEqual(
            [item.visual_type for item in plan.shots],
            ["video", "image", "image", "video", "video"],
        )
        self.assertIn("关系与机制", plan.shots[2].visual_intent)
        self.assertTrue(plan.shots[2].first_frame_prompt)
        self.assertTrue(plan.shots[2].motion_prompt)
        self.assertEqual(
            plan.shots[3].visual_intent,
            f"模型意图 {target_ids[3]}",
        )

    async def test_tts_stream_decoder_accepts_audio_and_terminal_event(self):
        encoded = base64.b64encode(b"mp3-bytes").decode("ascii")
        audio, duration, done = VolcengineTtsProvider._decode_line(json.dumps({
            "code": 0,
            "data": encoded,
            "addition": {"duration": "1250"},
        }))
        self.assertEqual(audio, b"mp3-bytes")
        self.assertEqual(duration, 1.25)
        self.assertFalse(done)
        _, _, done = VolcengineTtsProvider._decode_line(
            '{"code":20000000,"message":"done"}'
        )
        self.assertTrue(done)

        with self.assertRaises(ProviderUnavailable):
            VolcengineTtsProvider._decode_line(
                '{"code":0,"sequence":"not-a-number"}'
            )

    async def test_tts_synthesizes_normal_script_once_and_keeps_one_audio_asset(self):
        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="zh_female_vv_uranus_bigtts",
            api_key="speech-key",
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-tts",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        synthesized_texts: list[str] = []

        async def synthesize_segment(text: str, path: Path, **options) -> float:
            synthesized_texts.append(text)
            self.assertEqual(
                options["voice_id"],
                "zh_female_vv_uranus_bigtts",
            )
            self.assertEqual(options["speed_ratio"], 1.0)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"full-audio")
            return 48.0

        provider._synthesize_segment = synthesize_segment
        with tempfile.TemporaryDirectory() as directory:
            manifest, generated = await provider.synthesize(script, Path(directory))

        self.assertEqual(len(synthesized_texts), 1)
        synthesized_without_spacing = "".join(synthesized_texts[0].split())
        self.assertTrue(all(
            "".join(
                VolcengineTtsProvider._prepare_text(item.narration).split()
            ) in synthesized_without_spacing
            for item in script.beats
        ))
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0].asset_id, "narration_full")
        self.assertEqual(
            {item.asset_id for item in manifest.segments},
            {"narration_full"},
        )
        self.assertEqual(manifest.segments[0].start_seconds, 0.0)
        self.assertAlmostEqual(
            manifest.segments[-1].start_seconds
            + manifest.segments[-1].duration_seconds,
            manifest.total_duration_seconds,
            places=3,
        )
        for left, right in zip(manifest.segments, manifest.segments[1:]):
            self.assertAlmostEqual(
                left.start_seconds + left.duration_seconds,
                right.start_seconds,
                places=3,
            )

    async def test_tts_segment_uses_v3_api_key_headers_and_ndjson(self):
        requests = []
        encoded = base64.b64encode(b"fake-mp3").decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(
                body["req_params"]["speaker"],
                "zh_male_ruyayichen_saturn_bigtts",
            )
            self.assertEqual(
                body["req_params"]["audio_params"]["speech_rate"],
                20,
            )
            return httpx.Response(200, text=(
                json.dumps({"code": 0, "data": encoded})
                + "\n"
                + json.dumps({"code": 20000000, "message": "done"})
                + "\n"
            ))

        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="zh_female_vv_uranus_bigtts",
            api_key="speech-key",
            transport=httpx.MockTransport(handler),
        )

        async def fixed_duration(path: Path) -> float:
            self.assertEqual(path.read_bytes(), b"fake-mp3")
            return 1.5

        provider._probe_duration = fixed_duration
        usage_records: list[ProviderUsageRecord] = []

        async def record_usage(usage: ProviderUsageRecord) -> None:
            usage_records.append(usage)

        with tempfile.TemporaryDirectory() as directory:
            duration = await provider._synthesize_segment(
                "这是一句真实接口契约测试。",
                Path(directory) / "voice.mp3",
                voice_id="zh_male_ruyayichen_saturn_bigtts",
                speed_ratio=1.2,
                on_usage=record_usage,
            )
        self.assertEqual(duration, 1.5)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["x-api-key"], "speech-key")
        self.assertEqual(
            requests[0].headers["x-api-resource-id"], "seed-tts-2.0"
        )
        self.assertEqual(len(usage_records), 1)
        self.assertTrue(usage_records[0].succeeded)
        self.assertEqual(usage_records[0].operation, "tts_synthesis")
        self.assertEqual(usage_records[0].quantity, len("这是一句真实接口契约测试。"))
        self.assertIn("语速 1.2x", usage_records[0].note)

    async def test_tts_preview_uses_selected_settings_and_separate_cost_stage(self):
        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="zh_female_vv_uranus_bigtts",
            api_key="speech-key",
        )
        captured = {}

        async def synthesize_segment(text, path, **options):
            captured.update(options)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preview-audio")
            return 2.5

        provider._synthesize_segment = synthesize_segment
        with tempfile.TemporaryDirectory() as directory:
            generated = await provider.synthesize_preview(
                "这是当前脚本的开场。",
                Path(directory),
                voice_id="zh_female_santongyongns_saturn_bigtts",
                speed_ratio=1.1,
            )
            self.assertEqual(generated.path.read_bytes(), b"preview-audio")
        self.assertEqual(
            captured["voice_id"],
            "zh_female_santongyongns_saturn_bigtts",
        )
        self.assertEqual(captured["speed_ratio"], 1.1)
        self.assertEqual(captured["operation"], "tts_preview")
        self.assertFalse(captured["probe_duration"])

    async def test_tts_legacy_credentials_use_app_key_header(self):
        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="voice-test",
            app_id="legacy-app-id",
            access_token="legacy-access-token",
        )
        headers = provider._headers("request-id")
        self.assertEqual(headers["X-Api-App-Key"], "legacy-app-id")
        self.assertEqual(headers["X-Api-Access-Key"], "legacy-access-token")
        self.assertNotIn("X-Api-Key", headers)


class QijiaVideoUploadApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_and_batch_apply_share_the_shot_edit_mutex(self):
        actor = Actor(user_id=7, username="editor", role="member")
        actions = {
            "prepare_shot_media": {"shot_id": "shot_01"},
            "apply_pending_shot_media": {
                "expected_pending_fingerprint": "a" * 64,
                "batch_id": "batch_01",
            },
        }
        for index, (action, parameters) in enumerate(actions.items(), 1):
            task_id = f"task_mutex_{index}"
            create = AsyncMock(return_value=(task_id, True))
            get_task = AsyncMock(return_value={
                "job_payload": {
                    "action": action,
                    "job_id": "job",
                    "parameters": parameters,
                },
            })
            with (
                patch.object(
                    qijia_runtime.task_service,
                    "create_or_get_running_task_async",
                    create,
                ),
                patch.object(
                    qijia_runtime.task_service,
                    "get_task_async",
                    get_task,
                ),
            ):
                run = await qijia_runtime.start_run(
                    action,
                    "job",
                    actor,
                    parameters,
                )

            self.assertTrue(run.reused)
            self.assertEqual(run.task_id, task_id)
            self.assertEqual(
                create.await_args.args[0],
                "齐家短视频:shot-edit:job",
            )

    async def test_media_plan_preparation_and_production_share_one_mutex(self):
        actor = Actor(user_id=7, username="editor", role="member")
        for index, action in enumerate(
            ("prepare_media_review", "produce"),
            1,
        ):
            task_id = f"task_produce_mutex_{index}"
            create = AsyncMock(return_value=(task_id, True))
            get_task = AsyncMock(return_value={
                "job_payload": {
                    "action": action,
                    "job_id": "job",
                    "parameters": {},
                },
            })
            with (
                patch.object(
                    qijia_runtime.task_service,
                    "create_or_get_running_task_async",
                    create,
                ),
                patch.object(
                    qijia_runtime.task_service,
                    "get_task_async",
                    get_task,
                ),
            ):
                run = await qijia_runtime.start_run(action, "job", actor)

            self.assertTrue(run.reused)
            self.assertEqual(
                create.await_args.args[0],
                "齐家短视频:produce:job",
            )

    async def test_script_approval_can_pause_before_visual_generation(self):
        user = {"id": 7, "username": "editor", "role": "member"}
        approved_job = type("ApprovedJob", (), {"id": "job"})()
        service = type("Service", (), {})()
        service.approve_script = AsyncMock(return_value=approved_job)
        service.get_job = AsyncMock(return_value=approved_job)
        start = AsyncMock(return_value=type("Run", (), {
            "task_id": "task_media_plan",
            "reused": False,
        })())
        with (
            patch.object(qijia_api.runtime, "service", service),
            patch.object(qijia_api, "start_run", start),
            patch.object(
                qijia_api,
                "public_job_payload",
                return_value={"id": "job"},
            ),
        ):
            result = await qijia_api.approve_script(
                "job",
                qijia_api.ScriptApprovalRequest(
                    expected_revision=8,
                    script_hash="a" * 64,
                    prepare_media_first=True,
                ),
                user,
            )

        service.approve_script.assert_awaited_once_with(
            "job",
            8,
            "a" * 64,
            unittest.mock.ANY,
            prepare_media_first=True,
        )
        start.assert_awaited_once_with(
            "prepare_media_review",
            "job",
            unittest.mock.ANY,
        )
        self.assertTrue(result["data"]["media_review_requested"])
        self.assertEqual(result["data"]["task_id"], "task_media_plan")

    async def test_confirming_media_plan_queues_only_remaining_production(self):
        user = {"id": 7, "username": "editor", "role": "member"}
        confirmed_job = type("ConfirmedJob", (), {"id": "job"})()
        service = type("Service", (), {})()
        service.confirm_pre_generation_media = AsyncMock(
            return_value=confirmed_job
        )
        service.get_job = AsyncMock(return_value=confirmed_job)
        start = AsyncMock(return_value=type("Run", (), {
            "task_id": "task_remaining_production",
            "reused": False,
        })())
        with (
            patch.object(qijia_api.runtime, "service", service),
            patch.object(qijia_api, "start_run", start),
            patch.object(
                qijia_api,
                "public_job_payload",
                return_value={"id": "job"},
            ),
        ):
            result = await qijia_api.confirm_pre_generation_media(
                "job",
                qijia_api.RevisionRequest(expected_revision=12),
                user,
            )

        service.confirm_pre_generation_media.assert_awaited_once_with(
            "job",
            12,
            unittest.mock.ANY,
        )
        start.assert_awaited_once_with(
            "produce",
            "job",
            unittest.mock.ANY,
        )
        self.assertEqual(
            result["data"]["task_id"],
            "task_remaining_production",
        )

    async def test_direct_upload_init_and_complete_only_queue_after_head_verification(self):
        user = {
            "id": 7,
            "username": "editor",
            "role": "member",
            "permissions": ["qijia_video"],
        }
        digest = hashlib.sha256(b"uploaded-video").hexdigest()
        service = type("Service", (), {})()
        service.validate_shot_media_action = AsyncMock(return_value="")
        service.get_job = AsyncMock(return_value=object())
        storage = type("Storage", (), {})()
        storage.create_direct_upload = AsyncMock(return_value={
            "url": "https://test-bucket.tos-cn-shanghai.volces.com/upload.mp4",
            "method": "PUT",
            "headers": {
                "Content-Type": "video/mp4",
                "x-tos-meta-sha256": digest,
            },
            "expires_in_seconds": 900,
        })
        raw_asset = AssetRef(
            asset_id="raw_pending",
            object_key="qijia-video/staged-uploads/job/shot/upload.mp4",
            sha256=digest,
            size_bytes=14,
            media_type="video/mp4",
        )
        storage.complete_direct_upload = AsyncMock(return_value=raw_asset)
        start = AsyncMock(return_value=type("Run", (), {
            "task_id": "task_upload_01",
            "reused": False,
        })())

        with (
            patch.object(qijia_api.runtime, "service", service),
            patch.object(qijia_api.runtime, "storage", storage),
            patch.object(qijia_api, "start_run", start),
            patch.object(qijia_api, "public_job_payload", return_value={"id": "job"}),
            patch.object(qijia_api.settings, "SESSION_SECRET", "s" * 48),
        ):
            initiated = await qijia_api.initiate_shot_media_upload(
                "job",
                "shot",
                qijia_api.ShotMediaUploadInitiateRequest(
                    expected_revision=8,
                    original_filename="clip.mp4",
                    media_kind="video",
                    size_bytes=14,
                    sha256=digest,
                ),
                user,
            )
            grant = initiated["data"]
            completed = await qijia_api.complete_shot_media_upload(
                "job",
                "shot",
                qijia_api.ShotMediaUploadTokenRequest(
                    upload_token=grant["upload_token"]
                ),
                user,
            )

        self.assertEqual(grant["upload_mode"], "direct")
        self.assertEqual(completed["data"]["task_id"], "task_upload_01")
        storage.complete_direct_upload.assert_awaited_once()
        start.assert_awaited_once()
        self.assertEqual(start.await_args.args[0], "prepare_shot_media")
        parameters = start.await_args.args[3]
        self.assertEqual(parameters["media_kind"], "video")
        self.assertEqual(parameters["raw_asset"]["sha256"], digest)
        self.assertEqual(service.validate_shot_media_action.await_count, 2)

    async def test_local_storage_upload_init_keeps_multipart_fallback(self):
        service = type("Service", (), {})()
        service.validate_shot_media_action = AsyncMock(return_value="")
        storage = type("Storage", (), {})()
        storage.create_direct_upload = AsyncMock(return_value=None)
        user = {"id": 7, "username": "editor", "role": "member"}
        with (
            patch.object(qijia_api.runtime, "service", service),
            patch.object(qijia_api.runtime, "storage", storage),
        ):
            result = await qijia_api.initiate_shot_media_upload(
                "job",
                "shot",
                qijia_api.ShotMediaUploadInitiateRequest(
                    expected_revision=8,
                    original_filename="clip.mp4",
                    media_kind="video",
                    size_bytes=14,
                    sha256=hashlib.sha256(b"uploaded-video").hexdigest(),
                ),
                user,
            )

        self.assertEqual(result["data"], {"upload_mode": "multipart"})

    async def test_pending_media_apply_queues_one_batch_action(self):
        user = {"id": 7, "username": "editor", "role": "member"}
        service = type("Service", (), {})()
        service.validate_pending_shot_media_action = AsyncMock(
            return_value="a" * 64
        )
        service.get_job = AsyncMock(return_value=object())
        start = AsyncMock(return_value=type("Run", (), {
            "task_id": "task_batch_01",
            "reused": False,
        })())
        with (
            patch.object(qijia_api.runtime, "service", service),
            patch.object(qijia_api, "start_run", start),
            patch.object(qijia_api, "public_job_payload", return_value={"id": "job"}),
        ):
            result = await qijia_api.apply_pending_shot_media(
                "job",
                qijia_api.RevisionRequest(expected_revision=12),
                user,
            )

        self.assertEqual(result["data"]["task_id"], "task_batch_01")
        self.assertEqual(start.await_args.args[0], "apply_pending_shot_media")
        parameters = start.await_args.args[3]
        self.assertEqual(parameters["expected_pending_fingerprint"], "a" * 64)
        self.assertRegex(parameters["batch_id"], r"^batch_[a-f0-9]{24}$")

    async def test_selecting_an_upload_only_stages_it_without_a_task(self):
        user = {"id": 7, "username": "editor", "role": "member"}
        staged_job = object()
        service = type("Service", (), {})()
        service.stage_shot_media_selection = AsyncMock(return_value=staged_job)
        with (
            patch.object(qijia_api.runtime, "service", service),
            patch.object(qijia_api, "public_job_payload", return_value={
                "id": "job",
                "pending_shot_media_edits": [{
                    "shot_id": "shot",
                    "media_id": "upload_01",
                }],
            }),
            patch.object(qijia_api, "start_run", AsyncMock()) as start,
        ):
            result = await qijia_api.select_uploaded_shot_media(
                "job",
                "shot",
                "upload_01",
                qijia_api.ShotVersionSelectionRequest(expected_revision=9),
                user,
            )

        service.stage_shot_media_selection.assert_awaited_once_with(
            "job",
            "shot",
            "upload_01",
            9,
            unittest.mock.ANY,
        )
        start.assert_not_awaited()
        self.assertEqual(
            result["data"]["pending_shot_media_edits"][0]["media_id"],
            "upload_01",
        )

    async def test_tos_rejects_legacy_sync_upload_before_second_storage_hop(self):
        storage = type("Storage", (), {"name": "tos"})()
        upload = UploadFile(
            filename="clip.mp4",
            file=io.BytesIO(bytes.fromhex("000000186674797069736f6d")),
        )
        with patch.object(qijia_api.runtime, "storage", storage):
            with self.assertRaises(HTTPException) as rejected:
                await qijia_api.upload_shot_media(
                    "job",
                    "shot",
                    8,
                    upload,
                    {"id": 7, "username": "editor", "role": "member"},
                )

        self.assertEqual(rejected.exception.status_code, 409)
        self.assertIn("链路已升级", rejected.exception.detail)


class QijiaVideoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = InMemoryAggregateRepository()
        self.storage = LocalArtifactStorage(self.root / "storage")
        self.service = QijiaVideoService(
            repository=self.repository,
            script_provider=TemplateScriptProvider(),
            storyboard_provider=TemplateStoryboardProvider(),
            image_provider=MockImageProvider(),
            tts_provider=SilentTtsProvider(),
            video_provider=MockVideoProvider(),
            renderer=FakeRenderer(),
            storage=self.storage,
            quality_checker=PassingQualityChecker(),
            media_packager=FakeMediaPackager(),
            work_root=self.root / "work",
        )
        self.actor = Actor(user_id=7, username="editor", role="member")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_generic_source_card_requires_evidence_before_verification(self):
        card = await self.service.create_source_card(
            valid_card(sources=[], verified_facts=[]),
            self.actor,
        )

        with self.assertRaisesRegex(QualityGateFailed, "至少需要一条真实来源"):
            await self.service.verify_source_card(
                card.id,
                card.revision,
                self.actor,
            )

    async def test_job_freezes_recommended_skill_and_rejects_incompatible_skill(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )

        job = await self.service.create_job(card.id, self.actor)

        self.assertEqual(job.skill_snapshot.skill_id, "explain-expert-view")
        self.assertEqual(job.skill_snapshot.version, "3.0.0")
        self.assertEqual(job.skill_snapshot.knowledge_mode.value, "model_knowledge")
        self.assertEqual(job.skill_snapshot.research_mode.value, "none")
        self.assertEqual(
            job.generation_settings.skill_id,
            job.skill_snapshot.skill_id,
        )
        self.assertEqual(
            job.generation_settings.skill_version,
            job.skill_snapshot.version,
        )
        self.assertEqual(len(job.skill_snapshot.manifest_hash), 64)
        self.assertEqual(job.skill_snapshot.script_system_prompt, "")
        self.assertEqual(job.skill_snapshot.visual_policy, "")
        self.assertEqual(job.pipeline_version, "v2")
        self.assertEqual(job.script_skill_snapshot.skill_id, "evidence-led-explainer")
        self.assertEqual(job.generation_settings.script_prompt, "")
        self.assertEqual(job.generation_settings.seedance_prompt, "")

        with self.assertRaisesRegex(QualityGateFailed, "不支持内容格式"):
            await self.service.create_job(
                card.id,
                self.actor,
                GenerationSettings(skill_id="brief-recent-news"),
            )

    async def test_job_deletion_is_hidden_but_keeps_auditable_history(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        with self.assertRaisesRegex(InvalidTransition, "正在处理中"):
            await self.service.delete_job(
                job.id,
                job.revision,
                self.actor,
                active_run=True,
            )

        deleted = await self.service.delete_job(
            job.id,
            job.revision,
            self.actor,
        )

        self.assertTrue(deleted.deleted_at)
        self.assertEqual(deleted.deleted_by, self.actor.username)
        self.assertEqual(deleted.revision, job.revision + 1)
        self.assertEqual(await self.service.list_jobs(self.actor), [])
        retained = await self.service.list_jobs(
            self.actor,
            include_deleted=True,
        )
        self.assertEqual([item.id for item in retained], [job.id])
        with self.assertRaisesRegex(ResourceNotFound, "视频任务不存在"):
            await self.service.get_job(job.id, self.actor)
        with self.assertRaisesRegex(ResourceNotFound, "视频任务不存在"):
            await self.service.view_job(job.id, self.actor)

    async def test_job_freezes_single_owner_pipeline_snapshots(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )

        job = await self.service.create_job(
            card.id,
            self.actor,
            GenerationSettings(
                visual_style_id="paper-collage-explainer",
            ),
        )

        self.assertEqual(job.pipeline_version, "v2")
        self.assertEqual(job.script_skill_snapshot.skill_id, "evidence-led-explainer")
        self.assertEqual(
            job.director_skill_snapshot.skill_id,
            "animated-explainer",
        )
        self.assertEqual(job.director_skill_snapshot.version, "1.0.0")
        self.assertEqual(job.director_skill_snapshot.mode, "animated_explainer")
        self.assertEqual(
            job.provider_adapter_snapshot.adapter_id,
            "seedream-seedance",
        )
        self.assertEqual(job.provider_adapter_snapshot.version, "1.3.0")
        self.assertEqual(
            job.visual_style_snapshot.style_id,
            "paper-collage-explainer",
        )
        self.assertIsNone(job.prompt_writing_profile_snapshot)
        self.assertIsNone(job.creative_brief)
        self.assertEqual(
            job.generation_settings.visual_style_version,
            job.visual_style_snapshot.version,
        )
        self.assertEqual(
            job.generation_settings.director_skill_version,
            job.director_skill_snapshot.version,
        )
        self.assertEqual(job.generation_settings.script_prompt, "")
        self.assertEqual(job.generation_settings.seedance_prompt, "")
        job = await self.service.generate_script(job.id, self.actor)
        self.assertIsNotNone(job.editorial_plan)
        self.assertIsNone(job.creative_brief)
        compiled = self.service._storyboard_base_style(job)
        self.assertIn("【唯一视觉负责人】", compiled)
        self.assertIn("已确认脚本是内容唯一真相", compiled)
        self.assertIn("高级编辑纸张拼贴", compiled)
        self.assertIn("【导演方法】动画解说导演@1.0.0", compiled)
        self.assertIn("【独立 Visual Style】编辑纸张拼贴@1.1.0", compiled)
        self.assertIn("VisualBible", compiled)
        self.assertIn("ShotContextIR", compiled)
        self.assertNotIn("H3", compiled)
        self.assertNotIn(job.editorial_plan.core_thesis, compiled)

        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        self.assertEqual(job.storyboard_plan.schema_version, "3.0")
        self.assertIsNotNone(job.visual_bible)
        self.assertTrue(all(shot.context for shot in job.storyboard_plan.shots))
        self.assertTrue(all(
            shot.context.concrete_event and shot.context.blocking
            for shot in job.storyboard_plan.shots
        ))
        self.assertTrue(all(
            shot.context.visual_metaphor == ""
            for shot in job.storyboard_plan.shots
        ))
        self.assertTrue(all(
            "【Provider Adapter】seedream-seedance@1.3.0" in item.prompt
            and "【VisualBible】" in item.prompt
            and "【ShotContextIR】" in item.prompt
            and job.visual_bible.visual_world in item.prompt
            and "【Director Skill 艺术语言】" not in item.prompt
            for item in job.first_frame_candidates
        ))
        self.assertTrue(all(
            "【Provider Adapter】seedream-seedance@1.3.0" in item.prompt
            and "最高且唯一视觉基准" in item.prompt
            and job.visual_bible.visual_world in item.prompt
            and "【Director Skill 动态语言】" not in item.prompt
            for item in job.visual_requests
        ))

        with self.assertRaisesRegex(
            QualityGateFailed,
            "不要再提交 seedance_prompt",
        ):
            await self.service.create_job(
                card.id,
                self.actor,
                GenerationSettings(
                    visual_style_id="paper-collage-explainer",
                    seedance_prompt="自定义低饱和纸张拼贴导演设定。",
                ),
            )

        with self.assertRaisesRegex(
            QualityGateFailed,
            "不要再提交 script_prompt",
        ):
            await self.service.create_job(
                card.id,
                self.actor,
                GenerationSettings(script_prompt="另一套脚本方法。"),
            )

        with self.assertRaisesRegex(
            QualityGateFailed,
            "不要再提交 image_count 或 shot_count",
        ):
            await self.service.create_job(
                card.id,
                self.actor,
                GenerationSettings(image_count=2),
            )

        with self.assertRaisesRegex(
            QualityGateFailed,
            "Prompt Writing Profile 仅用于 Pipeline v1",
        ):
            await self.service.create_job(
                card.id,
                self.actor,
                GenerationSettings(
                    prompt_writing_profile_id="structured-multimodal",
                ),
            )

        with self.assertRaisesRegex(QualityGateFailed, "未知视觉风格"):
            await self.service.create_job(
                card.id,
                self.actor,
                GenerationSettings(visual_style_id="missing-style"),
            )

    async def test_confirmed_script_is_the_directors_only_content_truth(self):
        class RecordingDirectorProvider(TemplateStoryboardProvider):
            confirmed_script = None
            director_instruction = ""

            async def generate_with_direction(
                self,
                script,
                director_instruction,
                beat_groups,
                visual_types,
                **kwargs,
            ):
                self.confirmed_script = script.model_copy(deep=True)
                self.director_instruction = director_instruction
                return await super().generate_with_direction(
                    script,
                    director_instruction,
                    beat_groups,
                    visual_types,
                    **kwargs,
                )

        director = RecordingDirectorProvider()
        self.service.storyboard_provider = director
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        initial_plan_hash = job.editorial_plan.draft_script_hash
        initial_thesis = job.editorial_plan.core_thesis
        self.assertEqual(initial_plan_hash, job.script_hash)

        edited = job.script.model_copy(deep=True)
        editor_truth = "这是人工确认后的唯一解释路径，导演必须以这一版内容为准。"
        edited.beats[1].narration = editor_truth
        edited = ScriptDraft.model_validate(edited.model_dump(mode="json"))
        job = await self.service.update_script(
            job.id,
            edited,
            job.revision,
            self.actor,
        )

        self.assertEqual(job.editorial_plan.draft_script_hash, initial_plan_hash)
        self.assertNotEqual(job.editorial_plan.draft_script_hash, job.script_hash)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)

        self.assertEqual(content_hash(director.confirmed_script), job.script_hash)
        self.assertIn(editor_truth, director.confirmed_script.narration_text())
        self.assertIn(
            editor_truth,
            "\n".join(
                shot.narration_excerpt for shot in job.storyboard_plan.shots
            ),
        )
        self.assertIn("【唯一视觉负责人】", director.director_instruction)
        self.assertNotIn(initial_thesis, director.director_instruction)

    async def test_model_knowledge_generation_never_calls_research_provider(self):
        original_request = (
            "黄宗羲，大丈夫行事，论是非不论利害，论顺逆不论成败，"
            "论万世不论一生。请解释这段话的思想力量。"
        )

        class ModelKnowledgeProvider(TemplateScriptProvider):
            def __init__(self):
                self.research_calls = 0
                self.generated_prompt = ""

            async def research_for_skill(self, *args, **kwargs):
                del args, kwargs
                self.research_calls += 1
                raise AssertionError("模型知识任务不得调用研究 Provider")

            async def research_person_viewpoint(self, *args, **kwargs):
                del args, kwargs
                self.research_calls += 1
                raise AssertionError("模型知识任务不得调用人物搜索")

            async def generate_with_plan(
                self, card, prompt, *, on_usage=None
            ):
                self.generated_prompt = prompt
                return await super().generate_with_plan(
                    card, prompt, on_usage=on_usage
                )

        provider = ModelKnowledgeProvider()
        self.service.script_provider = provider
        card = await self.service.create_source_card(
            CreativeRequestInput(
                creative_request=original_request,
            ).to_source_card_input(),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        generated = await self.service.generate_script(job.id, self.actor)

        self.assertEqual(generated.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(provider.research_calls, 0)
        self.assertIsNone(generated.research_prompt_snapshot)
        self.assertIsNone(generated.research_brief)
        self.assertEqual(generated.research_warning, "")
        self.assertEqual(generated.skill_snapshot.research_mode.value, "none")
        self.assertEqual(
            generated.skill_snapshot.knowledge_mode.value,
            "model_knowledge",
        )
        self.assertIn(original_request, provider.generated_prompt)
        self.assertIn("【唯一 ContextPack（用户提供材料）】", provider.generated_prompt)
        self.assertIn('"external_retrieval": false', provider.generated_prompt)
        self.assertIn("禁止联网搜索", provider.generated_prompt)
        self.assertIn(
            "【Script Skill】evidence-led-explainer@1.1.0",
            provider.generated_prompt,
        )
        self.assertNotIn("openrouter:web_search", provider.generated_prompt)
        self.assertIsNotNone(generated.editorial_plan)
        self.assertIsNone(generated.creative_brief)

    async def test_script_retry_does_not_add_a_research_call(self):
        class OneTimeScriptFailureProvider(TemplateScriptProvider):
            def __init__(self):
                self.generate_calls = 0
                self.research_calls = 0

            async def research_for_skill(self, *args, **kwargs):
                del args, kwargs
                self.research_calls += 1
                raise AssertionError("脚本重试不得调用研究 Provider")

            async def generate_with_plan(
                self, card, prompt, *, on_usage=None
            ):
                self.generate_calls += 1
                if self.generate_calls == 1:
                    raise ProviderUnavailable("脚本服务暂时不可用")
                return await super().generate_with_plan(
                    card, prompt, on_usage=on_usage
                )

        provider = OneTimeScriptFailureProvider()
        self.service.script_provider = provider
        card = await self.service.create_source_card(
            CreativeRequestInput(
                creative_request="黄宗羲的这段话如何理解？请讲清原则、成败与时间尺度。",
            ).to_source_card_input(),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        with self.assertRaisesRegex(ProviderUnavailable, "脚本服务暂时不可用"):
            await self.service.generate_script(job.id, self.actor)
        completed = await self.service.generate_script(job.id, self.actor)

        self.assertEqual(completed.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(provider.generate_calls, 2)
        self.assertEqual(provider.research_calls, 0)
        self.assertIsNone(completed.research_prompt_snapshot)
        self.assertEqual(completed.research_warning, "")

    async def test_recent_news_cards_cannot_create_new_jobs(self):
        card = await self.service.create_source_card(
            NewsTopicInput(topic="TERA LAB").to_source_card_input(),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )

        with self.assertRaisesRegex(QualityGateFailed, "已停用该入口"):
            await self.service.create_job(card.id, self.actor)

        self.assertEqual(await self.service.list_jobs(self.actor), [])

    async def test_seedance_cost_keeps_submission_price_snapshot(self):
        job = VideoJob.model_validate({
            "id": "job-cost-snapshot",
            "state": "producing",
            "source_card_id": "card-cost",
            "source_card_revision": 1,
            "source_card_snapshot": {"title": "成本快照"},
        })
        queued = ProviderTask(
            provider="volcengine-seedance",
            provider_task_id="seedance-task-cost",
            request_fingerprint="c" * 64,
            request_id="shot_01",
            state="queued",
        )
        self.service._record_video_task_usage(job, queued)
        self.assertEqual(queued.pricing_rate_cny_per_million, 8)
        self.assertIsNone(queued.estimated_cost_cny)

        succeeded = queued.model_copy(update={
            "state": ProviderTaskState.SUCCEEDED,
            "usage_total_tokens": 100000,
        })
        self.service.seedance_price_per_million_tokens = 99
        self.service._record_video_task_usage(job, succeeded, queued)

        self.assertEqual(succeeded.pricing_rate_cny_per_million, 8)
        self.assertAlmostEqual(succeeded.estimated_cost_cny, 0.8)
        matching = [
            item for item in job.usage_records
            if item.request_id == "seedance-task-cost"
        ]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(matching[0].estimated_cost, 0.8)

    async def test_six_script_beats_are_all_grouped_into_five_visual_shots(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job.generation_settings = GenerationSettings(image_count=2)
        extra = job.script.beats[1].model_copy(deep=True)
        extra.id = "n01b"
        extra.role = "suspense"
        extra.narration = "真正值得追问的是，父母立刻帮忙时，孩子失去了什么？"
        extra.visual_direction = "家长的手停在任务上方，孩子抬头看向家长。"
        job.script.beats.insert(1, extra)

        groups = self.service._storyboard_beat_groups(job)

        self.assertEqual([len(group) for group in groups], [2, 1, 1, 1, 1])
        self.assertEqual(
            [beat.id for group in groups for beat in group],
            [beat.id for beat in job.script.beats],
        )

    async def test_legacy_fixed_count_never_duplicates_script_beats(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job.generation_settings = GenerationSettings(image_count=10)

        groups = self.service._storyboard_beat_groups(job)
        visual_types = self.service._storyboard_visual_types(job, groups)
        flat_ids = [beat.id for group in groups for beat in group]
        self.assertEqual(len(groups), len(job.script.beats))
        self.assertEqual(flat_ids, [beat.id for beat in job.script.beats])
        self.assertEqual(visual_types, ())
        self.assertEqual(len(flat_ids), len(set(flat_ids)))

    async def test_true_overlong_narration_is_rejected_without_hidden_rewrite(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        script = job.script.model_copy(deep=True)
        for segment in script.beats:
            segment.narration = "长" * 130

        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 650 字"):
            await self.service.update_script(
                job.id,
                script,
                job.revision,
                self.actor,
            )

        unchanged = await self.service.get_job(job.id, self.actor)
        self.assertEqual(unchanged.script_hash, job.script_hash)

    async def test_approval_never_rewrites_the_reviewed_script(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        legacy_script = job.script.model_copy(deep=True)
        for index, segment in enumerate(legacy_script.narration_segments):
            segment.text = "长" * (148 if index < 4 else 147)
        legacy_script.hook = legacy_script.narration_segments[0].text
        legacy_script.closing = legacy_script.narration_segments[-1].text
        job.script = legacy_script
        job.script_hash = content_hash(legacy_script)
        job.script_review = await self.service.script_provider.review(
            card, legacy_script
        )
        job = await self.service._save_job(job, self.actor)

        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 739 字"):
            await self.service.approve_script(
                job.id,
                job.revision,
                job.script_hash,
                self.actor,
            )

    async def test_generated_script_target_length_is_advisory(self):
        class UnderlongScriptProvider(TemplateScriptProvider):
            async def generate(self, card, prompt=None):
                script = await super().generate(card, prompt)
                for segment in script.narration_segments:
                    segment.text = "短" * 10
                script.hook = script.narration_segments[0].text
                script.closing = script.narration_segments[-1].text
                return script

        self.service.script_provider = UnderlongScriptProvider()
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        generated = await self.service.generate_script(job.id, self.actor)
        self.assertEqual(generated.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(narration_char_count(generated.script.narration_text()), 50)

    async def test_overlong_tts_stops_before_visual_cost_and_can_reopen_script(self):
        class OverlongTtsProvider(SilentTtsProvider):
            name = "overlong-tts-test"

            async def synthesize(
                self,
                script,
                workspace,
                *,
                voice_id=None,
                speed_ratio=1.0,
            ):
                manifest, files = await super().synthesize(
                    script,
                    workspace,
                    voice_id=voice_id,
                    speed_ratio=speed_ratio,
                )
                manifest.provider = self.name
                manifest.total_duration_seconds = 146.582
                return manifest, files

        image_provider = RecordingImageProvider()
        self.service.tts_provider = OverlongTtsProvider()
        self.service.image_provider = image_provider
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )

        with self.assertRaisesRegex(
            QualityGateFailed, "narration_duration_range=146.582"
        ):
            await self.service.produce(job.id, self.actor)

        failed = await self.service.get_job(job.id, self.actor)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.failed_stage, "production")
        self.assertEqual(image_provider.prompts, [])
        self.assertEqual(failed.first_frame_candidates, [])
        reopened = await self.service.reopen_script_review(
            failed.id,
            failed.revision,
            self.actor,
        )
        self.assertEqual(reopened.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertIsNone(reopened.narration_manifest)
        self.assertIsNone(reopened.render_manifest)
        self.assertEqual(reopened.approvals, [])

    async def test_overlong_script_failed_in_production_returns_to_editor(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        legacy_script = job.script.model_copy(deep=True)
        for index, segment in enumerate(legacy_script.narration_segments):
            segment.text = "长" * (148 if index < 4 else 147)
        legacy_script.hook = legacy_script.narration_segments[0].text
        legacy_script.closing = legacy_script.narration_segments[-1].text
        job.script = legacy_script
        job.script_hash = content_hash(legacy_script)
        job = await self.service._save_job(job, self.actor)
        self.assertEqual(job.state, JobState.SCRIPT_APPROVED)
        self.assertTrue(self.service.needs_script_revision(job))

        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 739 字"):
            await self.service.produce(job.id, self.actor)

        failed = await self.service.get_job(job.id, self.actor)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.failed_stage, "production")
        self.assertTrue(self.service.needs_script_revision(failed))
        reopened = await self.service.reopen_script_review(
            failed.id,
            failed.revision,
            self.actor,
        )
        self.assertEqual(reopened.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(reopened.failed_stage, "")
        self.assertEqual(reopened.error, "")
        self.assertEqual(reopened.approvals, [])
        self.assertEqual(
            narration_char_count(reopened.script.narration_text()), 739
        )
        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 739 字"):
            await self.service.approve_script(
                reopened.id,
                reopened.revision,
                reopened.script_hash,
                self.actor,
            )

    async def test_duration_recovery_reuses_already_paid_visuals(self):
        image_provider = RecordingImageProvider()
        self.service.image_provider = image_provider
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        original_script = job.script.model_copy(deep=True)
        image_call_count = len(image_provider.prompts)
        video_call_count = len(self.service.video_provider._requests)
        request_fingerprints = [
            item.fingerprint() for item in job.visual_requests
        ]

        long_script = job.script.model_copy(deep=True)
        for index, segment in enumerate(long_script.narration_segments):
            segment.text = "长" * (148 if index < 4 else 147)
        long_script.hook = long_script.narration_segments[0].text
        long_script.closing = long_script.narration_segments[-1].text
        job.script = long_script
        job.script_hash = content_hash(long_script)
        job.script_review = await self.service.script_provider.review(
            card, long_script
        )
        job = await self.service._save_job(job, self.actor)

        failed = await self.service.mark_execution_failed(
            job.id,
            "quality",
            "成片自动质检未通过：duration_range=146.582",
            self.actor,
        )
        reopened = await self.service.reopen_script_review(
            failed.id,
            failed.revision,
            self.actor,
        )
        self.assertIsNotNone(reopened.storyboard_plan)
        self.assertIsNotNone(reopened.render_manifest)
        self.assertEqual(
            len(reopened.visual_requests),
            len(request_fingerprints),
        )
        self.assertEqual(
            narration_char_count(reopened.script.narration_text()), 739
        )
        reopened = await self.service.update_script(
            reopened.id,
            original_script,
            reopened.revision,
            self.actor,
        )
        self.assertEqual(
            [item.fingerprint() for item in reopened.visual_requests],
            request_fingerprints,
        )
        self.assertEqual(
            reopened.storyboard_plan.input_hash,
            self.service._storyboard_input_hash(reopened),
        )
        approved = await self.service.approve_script(
            reopened.id,
            reopened.revision,
            reopened.script_hash,
            self.actor,
        )
        completed = await self.service.produce(approved.id, self.actor)

        self.assertEqual(completed.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(len(image_provider.prompts), image_call_count)
        self.assertEqual(
            len(self.service.video_provider._requests), video_call_count
        )

    async def test_reference_image_upload_is_private_and_size_bounded(self):
        upload = UploadFile(
            filename="style.png",
            file=io.BytesIO(MockImageProvider._PNG),
        )
        with patch.object(qijia_api.runtime, "storage", self.storage):
            asset = await qijia_api._store_reference_image(upload)
        self.assertEqual(asset.media_type, "image/png")
        self.assertLessEqual(asset.size_bytes, qijia_api.MAX_REFERENCE_IMAGE_BYTES)
        self.assertTrue(asset.object_key.startswith("qijia-video/reference-images/"))
        self.assertNotIn("style.png", asset.object_key)
        self.assertTrue(self.storage.path_for(asset.object_key).is_file())

    async def test_shot_media_upload_uses_magic_bytes_and_private_object_keys(self):
        image_upload = UploadFile(
            filename="../产品界面.png",
            file=io.BytesIO(MockImageProvider._PNG),
        )
        with patch.object(qijia_api.runtime, "storage", self.storage):
            image, image_kind, image_name = await qijia_api._store_shot_media(
                image_upload,
                job_id="job_upload_test",
                shot_id="shot_01",
                media_id="upload_image_test",
            )
        self.assertEqual((image_kind, image.media_type), ("image", "image/png"))
        self.assertEqual(image_name, "产品界面.png")
        self.assertNotIn("产品界面", image.object_key)
        self.assertLessEqual(image.size_bytes, qijia_api.MAX_SHOT_IMAGE_BYTES)

        mov_bytes = (
            bytes.fromhex("000000186674797071742020")
            + b"editor-video-fixture"
        )
        video_upload = UploadFile(
            filename="folder\\采访.mov",
            file=io.BytesIO(mov_bytes),
        )
        with patch.object(qijia_api.runtime, "storage", self.storage):
            video, video_kind, video_name = await qijia_api._store_shot_media(
                video_upload,
                job_id="job_upload_test",
                shot_id="shot_02",
                media_id="upload_video_test",
            )
        self.assertEqual(
            (video_kind, video.media_type),
            ("video", "video/quicktime"),
        )
        self.assertEqual(video_name, "采访.mov")
        self.assertTrue(video.object_key.endswith(".mov"))
        self.assertLessEqual(video.size_bytes, qijia_api.MAX_SHOT_VIDEO_BYTES)

    async def test_direct_upload_token_binds_user_job_file_and_expiry(self):
        claims = qijia_api.ShotMediaUploadClaims(
            version=qijia_api.SHOT_MEDIA_UPLOAD_TOKEN_VERSION,
            user_id=self.actor.user_id,
            username=self.actor.username,
            job_id="job_upload_test",
            shot_id="shot_02",
            media_id="upload_video_test",
            object_key=(
                "qijia-video/staged-uploads/job_upload_test/shot_02/"
                "upload_video_test.mov"
            ),
            media_kind="video",
            media_type="video/quicktime",
            original_filename="采访.mov",
            size_bytes=1024,
            sha256=hashlib.sha256(b"video").hexdigest(),
            expected_revision=8,
            expected_selected_media_id="",
            expires_at=int(qijia_api.time.time()) + 900,
        )
        with patch.object(qijia_api.settings, "SESSION_SECRET", "s" * 48):
            token = qijia_api._create_shot_media_upload_token(claims)
            decoded = qijia_api._bound_shot_media_upload_claims(
                token,
                job_id="job_upload_test",
                shot_id="shot_02",
                actor=self.actor,
            )
            with self.assertRaises(HTTPException) as tampered:
                qijia_api._decode_shot_media_upload_token(
                    token[:-1] + ("0" if token[-1] != "0" else "1")
                )
            with self.assertRaises(HTTPException) as wrong_job:
                qijia_api._bound_shot_media_upload_claims(
                    token,
                    job_id="another_job",
                    shot_id="shot_02",
                    actor=self.actor,
                )
            expired_token = qijia_api._create_shot_media_upload_token(
                claims.model_copy(update={"expires_at": int(qijia_api.time.time()) - 1})
            )
            with self.assertRaises(HTTPException) as expired:
                qijia_api._decode_shot_media_upload_token(expired_token)

        self.assertEqual(decoded.object_key, claims.object_key)
        self.assertEqual(tampered.exception.status_code, 422)
        self.assertEqual(wrong_job.exception.status_code, 403)
        self.assertEqual(expired.exception.status_code, 422)

    async def test_global_reference_image_reaches_all_seedream_requests(self):
        class RecordingStoryboardProvider(TemplateStoryboardProvider):
            base_style = ""

            async def generate_with_direction(
                self,
                script,
                director_instruction,
                beat_groups,
                visual_types,
                **kwargs,
            ):
                self.base_style = director_instruction
                return await super().generate_with_direction(
                    script,
                    director_instruction,
                    beat_groups,
                    visual_types,
                    **kwargs,
                )

        reference_path = self.root / "reference.png"
        reference_path.write_bytes(MockImageProvider._PNG)
        reference_asset = await self.storage.put_file(
            object_key="qijia-video/reference-images/test.png",
            path=reference_path,
            asset_id="reference_image_test",
            media_type="image/png",
        )
        provider = RecordingImageProvider()
        storyboard_provider = RecordingStoryboardProvider()
        self.service.image_provider = provider
        self.service.storyboard_provider = storyboard_provider
        card = await self.service.create_source_card(
            valid_card(reference_assets=[reference_asset.model_dump(mode="json")]),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(
            card.id,
            self.actor,
            GenerationSettings(
                visual_style_id="paper-collage-explainer",
            ),
        )
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)

        self.assertEqual(len(provider.reference_image_urls), 5)
        self.assertEqual(
            set(provider.reference_image_urls),
            {"local://qijia-video/reference-images/test.png"},
        )
        self.assertIn("【参考素材边界】", storyboard_provider.base_style)
        self.assertIn("reference_roles", storyboard_provider.base_style)
        self.assertIn("【唯一视觉负责人】", storyboard_provider.base_style)
        self.assertNotIn("CreativeBrief", storyboard_provider.base_style)
        self.assertIn("【导演方法】", storyboard_provider.base_style)
        self.assertIn("高级编辑纸张拼贴", storyboard_provider.base_style)
        self.assertTrue(all(
            "【参考素材保留协议】" in prompt
            and '"reference_roles": ["identity"]' in prompt
            for prompt in provider.prompts
        ))
        self.assertTrue(all(
            "最高且唯一视觉基准" in request.prompt
            for request in job.visual_requests
        ))

    async def test_two_distinct_approvals_create_complete_package(self):
        call_order: list[str] = []

        class OrderedImageProvider(MockImageProvider):
            async def generate(
                self,
                prompt: str,
                *,
                seed: int,
                reference_image_url: str = "",
            ):
                call_order.append("image")
                return await super().generate(
                    prompt,
                    seed=seed,
                    reference_image_url=reference_image_url,
                )

        class OrderedVideoProvider(MockVideoProvider):
            def __init__(self):
                super().__init__()
                self._refreshing_status = False

            async def submit(self, request, *, first_frame_url=""):
                if not self._refreshing_status:
                    call_order.append("video_submit")
                return await super().submit(
                    request,
                    first_frame_url=first_frame_url,
                )

            async def get_status(
                self, provider_task_id, request_fingerprint
            ):
                self._refreshing_status = True
                try:
                    return await super().get_status(
                        provider_task_id, request_fingerprint
                    )
                finally:
                    self._refreshing_status = False

        self.service.image_provider = OrderedImageProvider()
        self.service.video_provider = OrderedVideoProvider()
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        self.assertEqual(job.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(job.script_review.input_hash, job.script_hash)

        with self.assertRaises(RevisionConflict):
            await self.service.approve_script(
                job.id, job.revision, "0" * 64, self.actor
            )
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        self.assertEqual(job.approval("script").actor, "editor")

        seed_widths: list[int] = []
        with patch(
            "qijia_video.service.secrets.randbits",
            side_effect=lambda bits: seed_widths.append(bits) or len(seed_widths),
        ):
            job = await self.service.produce(job.id, self.actor)
        self.assertEqual(seed_widths, [31] * 5)
        self.assertEqual(
            call_order.count("image"),
            len(job.storyboard_plan.shots),
        )
        self.assertEqual(
            call_order.count("video_submit"),
            len(job.visual_requests),
        )
        self.assertEqual(job.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(len(job.review_bundle_hash), 64)
        self.assertTrue(job.render_manifest.subtitle_cues)
        self.assertEqual(
            [cue.text for cue in job.render_manifest.screen_text_cues],
            ["先看依据，再下结论"],
        )
        self.assertNotIn(
            "先看依据，再下结论",
            "".join(item.text for item in job.narration_manifest.segments),
        )
        self.assertEqual(
            (job.render_manifest.width, job.render_manifest.height),
            (1080, 1920),
        )
        self.assertTrue(all(
            len(cue.text) <= 20 for cue in job.render_manifest.subtitle_cues
        ))
        self.assertTrue(all(
            cue.text == cue.text.strip() for cue in job.render_manifest.subtitle_cues
        ))
        self.assertEqual(job.generation_settings.image_count, 0)
        self.assertEqual(job.generation_settings.shot_count, 0)
        self.assertEqual(
            len(job.visual_requests),
            sum(
                shot.visual_type == "video"
                for shot in job.storyboard_plan.shots
            ),
        )
        self.assertGreaterEqual(len(job.visual_requests), 1)
        self.assertLessEqual(len(job.visual_requests), 3)
        self.assertTrue(all(
            request.resolution == "1080p"
            and 8 <= request.duration_seconds <= 10
            for request in job.visual_requests
        ))
        video_shot_ids = [
            shot.shot_id
            for shot in job.storyboard_plan.shots
            if shot.visual_type == "video"
        ]
        self.assertEqual(
            [request.request_id for request in job.visual_requests],
            video_shot_ids,
        )
        first_video_shot = next(
            shot for shot in job.storyboard_plan.shots
            if shot.visual_type == "video"
        )
        self.assertIn("最高且唯一视觉基准", job.visual_requests[0].prompt)
        self.assertIn("【VisualBible】", job.visual_requests[0].prompt)
        self.assertIn("【ShotContextIR】", job.visual_requests[0].prompt)
        self.assertIn(
            first_video_shot.context.camera_intent,
            job.visual_requests[0].prompt,
        )
        self.assertNotIn("视频提示词结构", job.visual_requests[0].prompt)
        self.assertEqual(len(job.video_tasks), len(job.visual_requests))
        self.assertEqual(len(job.visual_versions), len(job.visual_requests))
        self.assertTrue(all(
            version.version == 1
            and version.asset is not None
            and version.task.state == ProviderTaskState.SUCCEEDED
            for version in job.visual_versions
        ))
        self.assertEqual(
            [task.request_id for task in job.video_tasks],
            [request.request_id for request in job.visual_requests],
        )
        self.assertTrue(all(
            task.state == ProviderTaskState.SUCCEEDED for task in job.video_tasks
        ))
        self.assertIsNotNone(job.storyboard_plan)
        self.assertEqual(len(job.storyboard_plan.shots), len(job.script.beats))
        planned_beat_ids = [
            beat_id
            for shot in job.storyboard_plan.shots
            for beat_id in shot.beat_ids
        ]
        self.assertEqual(
            [
                beat_id
                for index, beat_id in enumerate(planned_beat_ids)
                if index == 0 or beat_id != planned_beat_ids[index - 1]
            ],
            [beat.id for beat in job.script.beats],
        )
        self.assertEqual(
            [shot.visual_type for shot in job.storyboard_plan.shots].count("video"),
            len(job.visual_requests),
        )
        self.assertEqual(
            [shot.visual_type for shot in job.storyboard_plan.shots].count("image"),
            len(job.storyboard_plan.shots) - len(job.visual_requests),
        )
        timing_by_id = {
            item.segment_id: item.duration_seconds
            for item in job.narration_manifest.segments
        }
        self.assertTrue(all(
            sum(timing_by_id[beat_id] for beat_id in shot.beat_ids) <= 10
            for shot in job.storyboard_plan.shots
            if shot.visual_type == "video"
        ))
        self.assertEqual(
            len(job.first_frame_candidates), len(job.storyboard_plan.shots)
        )
        self.assertTrue(all(
            0 <= candidate.seed <= SEEDREAM_MAX_SEED
            for candidate in job.first_frame_candidates
        ))
        self.assertEqual(job.frame_selections, [])
        self.assertTrue(all(
            candidate.asset is not None
            for candidate in job.first_frame_candidates
        ))
        self.assertTrue(all(
            shot.selected_candidate_id == f"frame_{shot.shot_id}_01"
            for shot in job.storyboard_plan.shots
        ))
        self.assertEqual(
            len(job.render_manifest.visual_blocks), len(job.storyboard_plan.shots)
        )
        self.assertEqual(
            sum(
                block.duration_in_frames
                for block in job.render_manifest.visual_blocks
            ),
            job.render_manifest.duration_in_frames,
        )
        public_payload = qijia_api.public_job_payload(job, {
            "id": 7,
            "username": "editor",
            "role": "member",
            "permissions": ["qijia_video"],
        })
        self.assertEqual(public_payload["script"]["schema_version"], "3.0")
        self.assertIn("beats", public_payload["script"])
        self.assertNotIn("narration_segments", public_payload["script"])
        self.assertTrue(all(
            "source_url" not in candidate
            for candidate in public_payload["first_frame_candidates"]
        ))
        selected_assets = {
            candidate.asset.asset_id
            for candidate in job.first_frame_candidates
            if candidate.asset
            and any(
                shot.selected_candidate_id == candidate.candidate_id
                for shot in job.storyboard_plan.shots
            )
        }
        self.assertEqual(len(selected_assets), len(job.storyboard_plan.shots))
        self.assertEqual(
            {request.first_frame_asset_id for request in job.visual_requests},
            {
                candidate.asset.asset_id
                for candidate in job.first_frame_candidates
                if candidate.asset
                and candidate.shot_id in set(video_shot_ids)
                and any(
                    shot.selected_candidate_id == candidate.candidate_id
                    for shot in job.storyboard_plan.shots
                )
            },
        )
        self.assertTrue(all(
            request.prompt.startswith(
                "【Provider Adapter】seedream-seedance@1.3.0"
            )
            and "输入首帧就是视频第一帧" in request.prompt
            and "只生成无声画面" in request.prompt
            and "可读文字" in request.prompt
            and "【首帧驱动】" not in request.prompt
            and "【动作与运镜】" not in request.prompt
            for request in job.visual_requests
        ))
        self.assertTrue(all(
            segment.text not in request.prompt
            for segment in job.script.narration_segments
            for request in job.visual_requests
        ))
        blocks = job.render_manifest.visual_blocks
        self.assertEqual(len(blocks), len(job.storyboard_plan.shots))
        self.assertEqual(
            [block.type for block in blocks],
            [
                "generated_video"
                if shot.visual_type == "video"
                else "generated_image"
                for shot in job.storyboard_plan.shots
            ],
        )
        self.assertTrue(all(
            block.playback_rate == 1.0
            for block in blocks
            if block.type == "generated_video"
        ))
        self.assertTrue(all(
            block.asset_id
            and not block.headline
            and not block.body
            for block in blocks
        ))
        self.assertEqual(
            [block.shot_id for block in blocks],
            [f"shot_{index:02d}" for index in range(1, len(blocks) + 1)],
        )
        self.assertEqual(job.render_manifest.video_title, job.script.video_title)
        self.assertEqual(job.render_manifest.cover_text, job.script.cover_text)
        self.assertIn(job.render_manifest.cover_asset_id, selected_assets)
        self.assertEqual(blocks[0].start_frame, 0)
        self.assertEqual(
            blocks[-1].start_frame + blocks[-1].duration_in_frames,
            job.render_manifest.duration_in_frames,
        )
        self.assertEqual(
            sum(block.duration_in_frames for block in blocks),
            job.render_manifest.duration_in_frames,
        )
        self.assertTrue(all(
            left.start_frame + left.duration_in_frames == right.start_frame
            for left, right in zip(blocks, blocks[1:])
        ))
        with self.assertRaises(RevisionConflict):
            await self.service.approve_final(
                job.id, job.revision, "0" * 64, self.actor
            )
        job = await self.service.approve_final(
            job.id, job.revision, job.review_bundle_hash, self.actor
        )
        self.assertEqual(job.state, JobState.PACKAGED)
        self.assertEqual(
            [item.kind for item in job.approvals], ["script", "final"]
        )
        names = {item.name for item in job.artifacts}
        self.assertTrue(REQUIRED_PACKAGE_NAMES.issubset(names))
        draft = next(item for item in job.artifacts if item.name == "draft.mp4")
        final = next(item for item in job.artifacts if item.name == "final.mp4")
        self.assertEqual(draft.asset.sha256, final.asset.sha256)
        self.assertEqual(draft.asset.asset_id, final.asset.asset_id)
        self.assertEqual(draft.asset.object_key, final.asset.object_key)
        archive = self.root / "release.zip"
        await self.service.build_release_archive(job.id, self.actor, archive)
        expected_final = self.root / "expected-final.mp4"
        await self.storage.materialize(final.asset, expected_final)
        with zipfile.ZipFile(archive) as release:
            self.assertEqual(set(release.namelist()), REQUIRED_PACKAGE_NAMES)
            self.assertEqual(release.read("final.mp4"), expected_final.read_bytes())

    async def test_one_shot_can_regenerate_and_switch_versions_without_rerunning_others(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)

        shot_id = job.visual_requests[0].request_id
        original_requests = {
            item.request_id: item for item in job.visual_requests
        }
        original = original_requests[shot_id]
        original_version = next(
            item for item in job.visual_versions if item.shot_id == shot_id
        )
        frame_candidates = [
            item for item in job.first_frame_candidates if item.shot_id == shot_id
        ]
        self.assertEqual(len(frame_candidates), 1)
        frame_candidate = frame_candidates[0]
        storyboard_shot = next(
            item for item in job.storyboard_plan.shots if item.shot_id == shot_id
        )
        revision_intent = "一扇门缓慢打开，暖色光线进入房间。"
        events = []
        job = await self.service.regenerate_shot(
            job.id,
            shot_id,
            revision_intent,
            original.fingerprint(),
            self.actor,
            progress=events.append,
            seedance_model=SEEDANCE_FLAGSHIP_MODEL,
        )

        self.assertEqual(job.state, JobState.FINAL_REVIEW_REQUIRED)
        versions = sorted(
            (item for item in job.visual_versions if item.shot_id == shot_id),
            key=lambda item: item.version,
        )
        self.assertEqual([item.version for item in versions], [1, 2])
        self.assertEqual(versions[0].version_id, original_version.version_id)
        self.assertIsNotNone(versions[0].asset)
        self.assertIsNotNone(versions[1].asset)
        self.assertEqual(versions[0].request.revision_intent, "")
        self.assertEqual(versions[1].request.revision_intent, revision_intent)
        self.assertNotEqual(versions[1].request.prompt, revision_intent)
        self.assertIn(
            storyboard_shot.context.camera_intent,
            versions[1].request.prompt,
        )
        self.assertIn(revision_intent, versions[1].request.prompt)
        self.assertIn("最高且唯一视觉基准", versions[1].request.prompt)
        self.assertIn("generate_audio=false", versions[1].request.prompt)
        self.assertIsNotNone(versions[1].request.seed)
        self.assertEqual(versions[0].request.model_id, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(versions[1].request.model_id, SEEDANCE_FLAGSHIP_MODEL)
        selected = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )
        self.assertEqual(selected.fingerprint(), versions[1].request.fingerprint())
        self.assertEqual(
            selected.first_frame_asset_id, frame_candidate.asset.asset_id
        )
        self.assertEqual(
            storyboard_shot.selected_candidate_id,
            frame_candidate.candidate_id,
        )
        self.assertEqual(
            len(self.service._all_video_tasks(job)),
            len(original_requests) + 1,
        )
        self.assertEqual(
            {
                item.request_id: item.fingerprint()
                for item in job.visual_requests
                if item.request_id != shot_id
            },
            {
                item.request_id: item.fingerprint()
                for item in original_requests.values()
                if item.request_id != shot_id
            },
        )
        self.assertTrue(any(
            event.get("workflow") == "shot_edit" for event in events
        ))
        self.assertEqual(
            [item.kind for item in job.approvals], ["script"]
        )

        job = await self.service.select_shot_version(
            job.id,
            shot_id,
            versions[0].version_id,
            selected.fingerprint(),
            self.actor,
        )
        restored = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )
        self.assertEqual(restored.fingerprint(), original.fingerprint())
        self.assertEqual(restored.revision_intent, "")
        restored_storyboard_shot = next(
            item for item in job.storyboard_plan.shots if item.shot_id == shot_id
        )
        original_frame = next(
            item
            for item in frame_candidates
            if item.asset.asset_id == original.first_frame_asset_id
        )
        self.assertEqual(
            restored_storyboard_shot.selected_candidate_id,
            original_frame.candidate_id,
        )
        self.assertEqual(len([
            item for item in job.visual_versions if item.shot_id == shot_id
        ]), 2)
        self.assertEqual(
            len(self.service._all_video_tasks(job)),
            len(original_requests) + 1,
        )

        legacy_prompt = "历史后台任务已经冻结的完整镜头提示词。"
        job = await self.service.regenerate_shot(
            job.id,
            shot_id,
            "",
            restored.fingerprint(),
            self.actor,
            _legacy_compiled_prompt=legacy_prompt,
        )
        legacy_selected = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )
        self.assertEqual(legacy_selected.prompt, legacy_prompt)
        self.assertEqual(legacy_selected.revision_intent, "")
        self.assertEqual(len([
            item for item in job.visual_versions if item.shot_id == shot_id
        ]), 3)
        self.assertEqual(
            len(self.service._all_video_tasks(job)),
            len(original_requests) + 2,
        )

    async def test_editor_media_can_mix_images_and_videos_and_restore_ai(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)

        video_shot_id = job.visual_requests[0].request_id
        image_shot_id = next(
            item.shot_id
            for item in job.storyboard_plan.shots
            if item.visual_type == "image"
        )
        original_video_asset = self.service._generated_visual_asset_for_shot(
            job, video_shot_id
        )
        original_image_candidate = next(
            item
            for item in job.first_frame_candidates
            if item.shot_id == image_shot_id
            and item.candidate_id == next(
                shot.selected_candidate_id
                for shot in job.storyboard_plan.shots
                if shot.shot_id == image_shot_id
            )
        )

        image_source = self.root / "editor-image.png"
        image_source.write_bytes(
            bytes.fromhex("89504e470d0a1a0a") + b"editor-image"
        )
        raw_image = await self.storage.put_file(
            object_key="tests/raw-editor-image.png",
            path=image_source,
            asset_id="raw_editor_image",
            media_type="image/png",
        )
        events = []
        job = await self.service.replace_shot_media(
            job.id,
            video_shot_id,
            raw_image,
            "image",
            "upload_image_01",
            "产品界面.png",
            "",
            self.actor,
            progress=events.append,
        )

        selected_video_shot = next(
            item
            for item in job.storyboard_plan.shots
            if item.shot_id == video_shot_id
        )
        image_override = next(
            item
            for item in job.shot_media_versions
            if item.media_id == "upload_image_01"
        )
        video_block = next(
            item
            for item in job.render_manifest.visual_blocks
            if item.shot_id == video_shot_id
        )
        self.assertEqual(selected_video_shot.selected_media_id, "upload_image_01")
        self.assertEqual(image_override.original_filename, "产品界面.png")
        self.assertEqual(video_block.type, "generated_image")
        self.assertEqual(video_block.asset_id, image_override.asset.asset_id)
        self.assertEqual(
            self.service.visual_asset_for_shot(job, video_shot_id),
            image_override.asset,
        )
        self.assertTrue(any(
            event.get("stage") == "media_prepare" for event in events
        ))

        video_source = self.root / "editor-video.mov"
        video_source.write_bytes(
            bytes.fromhex("000000186674797071742020") + b"editor-video"
        )
        raw_video = await self.storage.put_file(
            object_key="tests/raw-editor-video.mov",
            path=video_source,
            asset_id="raw_editor_video",
            media_type="video/quicktime",
        )
        job = await self.service.replace_shot_media(
            job.id,
            image_shot_id,
            raw_video,
            "video",
            "upload_video_01",
            "采访片段.mov",
            "",
            self.actor,
        )

        selected_image_shot = next(
            item
            for item in job.storyboard_plan.shots
            if item.shot_id == image_shot_id
        )
        video_override = next(
            item
            for item in job.shot_media_versions
            if item.media_id == "upload_video_01"
        )
        image_block = next(
            item
            for item in job.render_manifest.visual_blocks
            if item.shot_id == image_shot_id
        )
        self.assertEqual(selected_image_shot.selected_media_id, "upload_video_01")
        self.assertEqual(video_override.asset.media_type, "video/mp4")
        self.assertEqual(image_block.type, "generated_video")
        self.assertEqual(image_block.asset_id, video_override.asset.asset_id)
        self.assertEqual(len(job.shot_media_versions), 2)

        job = await self.service.select_shot_media(
            job.id,
            video_shot_id,
            "",
            "upload_image_01",
            self.actor,
        )
        restored_video_shot = next(
            item
            for item in job.storyboard_plan.shots
            if item.shot_id == video_shot_id
        )
        restored_video_block = next(
            item
            for item in job.render_manifest.visual_blocks
            if item.shot_id == video_shot_id
        )
        self.assertEqual(restored_video_shot.selected_media_id, "")
        self.assertEqual(restored_video_block.type, "generated_video")
        self.assertEqual(restored_video_block.asset_id, original_video_asset.asset_id)

        job = await self.service.select_shot_media(
            job.id,
            image_shot_id,
            "",
            "upload_video_01",
            self.actor,
        )
        restored_image_shot = next(
            item
            for item in job.storyboard_plan.shots
            if item.shot_id == image_shot_id
        )
        restored_image_block = next(
            item
            for item in job.render_manifest.visual_blocks
            if item.shot_id == image_shot_id
        )
        self.assertEqual(restored_image_shot.selected_media_id, "")
        self.assertEqual(restored_image_block.type, "generated_image")
        self.assertEqual(
            restored_image_block.asset_id,
            original_image_candidate.asset.asset_id,
        )

        job = await self.service.select_shot_media(
            job.id,
            video_shot_id,
            "upload_image_01",
            "",
            self.actor,
        )
        self.assertEqual(
            next(
                item.selected_media_id
                for item in job.storyboard_plan.shots
                if item.shot_id == video_shot_id
            ),
            "upload_image_01",
        )
        self.assertEqual(len(job.shot_media_versions), 2)
        self.assertEqual([item.kind for item in job.approvals], ["script"])

    async def test_pre_generation_uploads_skip_matching_ai_and_render_once(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id,
            job.revision,
            job.script_hash,
            self.actor,
            prepare_media_first=True,
        )
        self.assertEqual(
            job.pre_generation_media_mode,
            PreGenerationMediaMode.REVIEW_BEFORE_GENERATION,
        )

        job = await self.service.produce(job.id, self.actor)
        self.assertEqual(job.state, JobState.MEDIA_REVIEW_REQUIRED)
        self.assertIsNotNone(job.narration_manifest)
        self.assertIsNotNone(job.storyboard_plan)
        self.assertFalse(job.first_frame_candidates)
        self.assertFalse(job.visual_requests)
        self.assertFalse(job.video_tasks)
        self.assertFalse(job.artifacts)
        self.assertEqual(self.service.renderer.render_calls, 0)
        self.assertEqual(self.service.renderer.cover_calls, 0)

        paused_revision = job.revision
        paused_again = await self.service.produce(job.id, self.actor)
        self.assertEqual(paused_again.revision, paused_revision)
        self.assertEqual(self.service.renderer.render_calls, 0)

        video_shot = next(
            shot
            for shot in job.storyboard_plan.shots
            if shot.visual_type == "video"
        )
        image_shot = next(
            shot
            for shot in job.storyboard_plan.shots
            if shot.visual_type == "image"
        )
        uploads = (
            (
                video_shot,
                "video",
                "video/mp4",
                ".mp4",
                bytes.fromhex("000000186674797069736f6d")
                + b"pre-generation-video",
                "upload_early_video",
            ),
            (
                image_shot,
                "image",
                "image/png",
                ".png",
                MockImageProvider._PNG,
                "upload_early_image",
            ),
        )
        expected_media: dict[str, str] = {}
        for shot, media_kind, media_type, extension, payload, media_id in uploads:
            source = self.root / f"{media_id}{extension}"
            source.write_bytes(payload)
            raw_asset = await self.storage.put_file(
                object_key=f"tests/raw-{media_id}{extension}",
                path=source,
                asset_id=f"raw_{media_id}",
                media_type=media_type,
            )
            job = await self.service.prepare_shot_media(
                job.id,
                shot.shot_id,
                raw_asset,
                media_kind,
                media_id,
                source.name,
                "",
                self.actor,
            )
            expected_media[shot.shot_id] = media_id
            selected_shot = next(
                item
                for item in job.storyboard_plan.shots
                if item.shot_id == shot.shot_id
            )
            self.assertEqual(selected_shot.selected_media_id, media_id)
            self.assertFalse(job.pending_shot_media_edits)
            self.assertEqual(self.service.renderer.render_calls, 0)

        job = await self.service.stage_shot_media_selection(
            job.id,
            image_shot.shot_id,
            "",
            job.revision,
            self.actor,
        )
        self.assertEqual(
            next(
                shot.selected_media_id
                for shot in job.storyboard_plan.shots
                if shot.shot_id == image_shot.shot_id
            ),
            "",
        )
        job = await self.service.stage_shot_media_selection(
            job.id,
            image_shot.shot_id,
            expected_media[image_shot.shot_id],
            job.revision,
            self.actor,
        )
        self.assertEqual(self.service.renderer.render_calls, 0)

        job = await self.service.confirm_pre_generation_media(
            job.id,
            job.revision,
            self.actor,
        )
        self.assertEqual(job.state, JobState.SCRIPT_APPROVED)
        self.assertEqual(
            job.pre_generation_media_mode,
            PreGenerationMediaMode.CONFIRMED,
        )
        produced = await self.service.produce(job.id, self.actor)

        self.assertEqual(produced.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(self.service.renderer.render_calls, 1)
        self.assertEqual(self.service.renderer.cover_calls, 1)
        self.assertFalse(produced.pending_shot_media_edits)
        self.assertEqual(
            len(produced.first_frame_candidates),
            len(produced.storyboard_plan.shots) - len(expected_media),
        )
        self.assertFalse(any(
            candidate.shot_id in expected_media
            for candidate in produced.first_frame_candidates
        ))
        expected_ai_video_shots = {
            shot.shot_id
            for shot in produced.storyboard_plan.shots
            if shot.visual_type == "video"
            and shot.shot_id not in expected_media
        }
        self.assertEqual(
            {request.request_id for request in produced.visual_requests},
            expected_ai_video_shots,
        )
        self.assertEqual(len(produced.video_tasks), len(expected_ai_video_shots))
        self.assertEqual(
            {
                shot.shot_id: shot.selected_media_id
                for shot in produced.storyboard_plan.shots
                if shot.shot_id in expected_media
            },
            expected_media,
        )
        uploaded_assets = {
            item.shot_id: item.asset.asset_id
            for item in produced.shot_media_versions
            if item.media_id in expected_media.values()
        }
        blocks = {
            block.shot_id: block
            for block in produced.render_manifest.visual_blocks
        }
        self.assertEqual(
            {
                shot_id: blocks[shot_id].asset_id
                for shot_id in expected_media
            },
            uploaded_assets,
        )

    async def test_all_pre_generation_uploads_bypass_visual_providers(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id,
            job.revision,
            job.script_hash,
            self.actor,
            prepare_media_first=True,
        )
        job = await self.service.produce(job.id, self.actor)

        for index, shot in enumerate(job.storyboard_plan.shots, 1):
            source = self.root / f"all-owned-{index}.png"
            source.write_bytes(
                bytes.fromhex("89504e470d0a1a0a")
                + f"all-owned-{index}".encode()
            )
            raw_asset = await self.storage.put_file(
                object_key=f"tests/raw-all-owned-{index}.png",
                path=source,
                asset_id=f"raw_all_owned_{index}",
                media_type="image/png",
            )
            job = await self.service.prepare_shot_media(
                job.id,
                shot.shot_id,
                raw_asset,
                "image",
                f"upload_all_owned_{index}",
                source.name,
                "",
                self.actor,
            )

        self.assertEqual(self.service.renderer.render_calls, 0)
        job = await self.service.confirm_pre_generation_media(
            job.id,
            job.revision,
            self.actor,
        )
        produced = await self.service.produce(job.id, self.actor)

        self.assertEqual(produced.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertFalse(produced.first_frame_candidates)
        self.assertFalse(produced.visual_requests)
        self.assertFalse(produced.video_tasks)
        self.assertFalse(produced.visual_versions)
        self.assertEqual(self.service.renderer.render_calls, 1)
        self.assertEqual(self.service.renderer.cover_calls, 1)
        self.assertEqual(
            len(produced.render_manifest.visual_blocks),
            len(produced.storyboard_plan.shots),
        )
        self.assertTrue(all(
            block.asset_id
            for block in produced.render_manifest.visual_blocks
        ))

    async def test_three_prepared_media_edits_render_the_film_only_once(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        renderer = self.service.renderer
        baseline_render_calls = renderer.render_calls
        baseline_cover_calls = renderer.cover_calls
        original_bundle = job.review_bundle_hash
        original_artifacts = [
            (item.name, item.asset.asset_id) for item in job.artifacts
        ]
        shots = job.storyboard_plan.shots[:3]
        uploads = [
            ("image", "image/png", ".png", bytes.fromhex("89504e470d0a1a0a") + b"one"),
            ("video", "video/mp4", ".mp4", bytes.fromhex("000000186674797069736f6d") + b"two"),
            ("image", "image/jpeg", ".jpg", bytes.fromhex("ffd8ff") + b"three"),
        ]
        expected_selections = {}

        for index, (shot, upload) in enumerate(zip(shots, uploads), 1):
            media_kind, media_type, extension, payload = upload
            source = self.root / f"batch-source-{index}{extension}"
            source.write_bytes(payload)
            media_id = f"upload_batch_{index:02d}"
            raw_asset = await self.storage.put_file(
                object_key=f"tests/raw-batch-{index}{extension}",
                path=source,
                asset_id=f"raw_batch_{index}",
                media_type=media_type,
            )
            job = await self.service.prepare_shot_media(
                job.id,
                shot.shot_id,
                raw_asset,
                media_kind,
                media_id,
                source.name,
                "",
                self.actor,
            )
            expected_selections[shot.shot_id] = media_id
            self.assertEqual(renderer.render_calls, baseline_render_calls)
            self.assertEqual(renderer.cover_calls, baseline_cover_calls)
            self.assertEqual(job.review_bundle_hash, original_bundle)
            self.assertEqual(
                [(item.name, item.asset.asset_id) for item in job.artifacts],
                original_artifacts,
            )
            self.assertEqual(
                next(
                    item.selected_media_id
                    for item in job.storyboard_plan.shots
                    if item.shot_id == shot.shot_id
                ),
                "",
            )

        self.assertEqual(
            {
                item.shot_id: item.media_id
                for item in job.pending_shot_media_edits
            },
            expected_selections,
        )
        with self.assertRaisesRegex(InvalidTransition, "先应用或撤销"):
            await self.service.approve_final(
                job.id,
                job.revision,
                job.review_bundle_hash,
                self.actor,
                package_immediately=False,
            )

        fingerprint = await self.service.validate_pending_shot_media_action(
            job.id,
            job.revision,
            self.actor,
        )
        job = await self.service.apply_pending_shot_media(
            job.id,
            fingerprint,
            "batch_test_three",
            self.actor,
        )

        self.assertEqual(renderer.render_calls, baseline_render_calls + 1)
        self.assertEqual(renderer.cover_calls, baseline_cover_calls + 1)
        self.assertEqual(job.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertFalse(job.pending_shot_media_edits)
        self.assertEqual(
            {
                item.shot_id: item.selected_media_id
                for item in job.storyboard_plan.shots
                if item.shot_id in expected_selections
            },
            expected_selections,
        )
        self.assertTrue(any(
            "-batch_test_three/" in item.asset.object_key
            for item in job.artifacts
        ))
        job = await self.service.stage_shot_media_selection(
            job.id,
            shots[0].shot_id,
            "",
            job.revision,
            self.actor,
        )
        recovered = await self.service.apply_pending_shot_media(
            job.id,
            fingerprint,
            "batch_test_three",
            self.actor,
        )
        self.assertEqual(renderer.render_calls, baseline_render_calls + 1)
        self.assertEqual(len(recovered.pending_shot_media_edits), 1)
        self.assertEqual(recovered.pending_shot_media_edits[0].media_id, "")

    async def test_pending_media_can_be_replaced_and_discarded_without_render(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        baseline_render_calls = self.service.renderer.render_calls
        shot_id = job.storyboard_plan.shots[0].shot_id

        for index in (1, 2):
            source = self.root / f"pending-replacement-{index}.png"
            source.write_bytes(
                bytes.fromhex("89504e470d0a1a0a") + f"image-{index}".encode()
            )
            raw_asset = await self.storage.put_file(
                object_key=f"tests/raw-pending-{index}.png",
                path=source,
                asset_id=f"raw_pending_{index}",
                media_type="image/png",
            )
            job = await self.service.prepare_shot_media(
                job.id,
                shot_id,
                raw_asset,
                "image",
                f"upload_pending_{index}",
                source.name,
                "",
                self.actor,
            )

        self.assertEqual(self.service.renderer.render_calls, baseline_render_calls)
        self.assertEqual(len(job.shot_media_versions), 2)
        self.assertEqual(len(job.pending_shot_media_edits), 1)
        self.assertEqual(
            job.pending_shot_media_edits[0].media_id,
            "upload_pending_2",
        )
        job = await self.service.discard_pending_shot_media_edits(
            job.id,
            job.revision,
            self.actor,
            shot_id=shot_id,
        )
        self.assertFalse(job.pending_shot_media_edits)
        self.assertEqual(len(job.shot_media_versions), 2)
        self.assertEqual(self.service.renderer.render_calls, baseline_render_calls)

    async def test_failed_batch_render_keeps_draft_and_pending_edits(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        original_bundle = job.review_bundle_hash
        original_artifacts = [
            (item.name, item.asset.asset_id) for item in job.artifacts
        ]
        shot_id = job.storyboard_plan.shots[0].shot_id
        source = self.root / "batch-failure.png"
        source.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"failure")
        raw_asset = await self.storage.put_file(
            object_key="tests/raw-batch-failure.png",
            path=source,
            asset_id="raw_batch_failure",
            media_type="image/png",
        )
        job = await self.service.prepare_shot_media(
            job.id,
            shot_id,
            raw_asset,
            "image",
            "upload_batch_failure",
            source.name,
            "",
            self.actor,
        )
        fingerprint = await self.service.validate_pending_shot_media_action(
            job.id,
            job.revision,
            self.actor,
        )

        class FailingBatchRenderer(FakeRenderer):
            async def render(self, manifest, storage, workspace):
                del manifest, storage, workspace
                self.render_calls += 1
                raise ProviderUnavailable("测试中的批量渲染失败")

        self.service.renderer = FailingBatchRenderer()
        with self.assertRaisesRegex(ProviderUnavailable, "批量渲染失败"):
            await self.service.apply_pending_shot_media(
                job.id,
                fingerprint,
                "batch_failure",
                self.actor,
            )

        latest = await self.service.get_job(job.id, self.actor)
        self.assertEqual(latest.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(latest.review_bundle_hash, original_bundle)
        self.assertEqual(
            [(item.name, item.asset.asset_id) for item in latest.artifacts],
            original_artifacts,
        )
        self.assertEqual(len(latest.pending_shot_media_edits), 1)
        self.assertEqual(
            latest.pending_shot_media_edits[0].media_id,
            "upload_batch_failure",
        )
        self.assertEqual(
            next(
                item.selected_media_id
                for item in latest.storyboard_plan.shots
                if item.shot_id == shot_id
            ),
            "",
        )
        self.assertIn("原成片和待应用修改均已保留", latest.error)

    async def test_failed_uploaded_video_keeps_the_reviewable_draft(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        original_bundle = job.review_bundle_hash
        original_artifacts = [
            (item.name, item.asset.asset_id) for item in job.artifacts
        ]
        shot_id = job.visual_requests[0].request_id

        video_source = self.root / "broken-editor-video.mov"
        video_source.write_bytes(
            bytes.fromhex("000000186674797071742020") + b"broken-editor-video"
        )
        raw_video = await self.storage.put_file(
            object_key="tests/broken-editor-video.mov",
            path=video_source,
            asset_id="raw_broken_editor_video",
            media_type="video/quicktime",
        )

        class FailingUploadPackager(FakeMediaPackager):
            async def prepare_uploaded_video_for_timeline(
                self,
                source,
                destination,
                *,
                chapter_duration_seconds,
            ):
                del source, destination, chapter_duration_seconds
                raise ProviderUnavailable("测试中的上传视频处理失败")

        self.service.media_packager = FailingUploadPackager()
        with self.assertRaisesRegex(ProviderUnavailable, "上传视频处理失败"):
            await self.service.replace_shot_media(
                job.id,
                shot_id,
                raw_video,
                "video",
                "upload_broken_01",
                "损坏视频.mov",
                "",
                self.actor,
            )

        latest = await self.service.get_job(job.id, self.actor)
        current_shot = next(
            item
            for item in latest.storyboard_plan.shots
            if item.shot_id == shot_id
        )
        self.assertEqual(latest.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(latest.review_bundle_hash, original_bundle)
        self.assertEqual(
            [(item.name, item.asset.asset_id) for item in latest.artifacts],
            original_artifacts,
        )
        self.assertEqual(current_shot.selected_media_id, "")
        self.assertFalse(any(
            item.media_id == "upload_broken_01"
            for item in latest.shot_media_versions
        ))
        self.assertIn("原成片仍然保留", latest.error)

    async def test_failed_shot_regeneration_keeps_the_reviewable_draft(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        original_bundle = job.review_bundle_hash
        shot_id = job.visual_requests[0].request_id
        original = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )

        class FailingVideoProvider:
            name = "failing-video"

            async def submit(self, request, *, first_frame_url=""):
                del request, first_frame_url
                raise ProviderUnavailable("测试中的单镜头失败")

        self.service.video_provider = FailingVideoProvider()
        with self.assertRaisesRegex(ProviderUnavailable, "单镜头失败"):
            await self.service.regenerate_shot(
                job.id,
                shot_id,
                "新的镜头提示词",
                original.fingerprint(),
                self.actor,
            )
        latest = await self.service.get_job(job.id, self.actor)
        self.assertEqual(latest.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(latest.review_bundle_hash, original_bundle)
        current = next(
            item for item in latest.visual_requests
            if item.request_id == shot_id
        )
        self.assertEqual(current.fingerprint(), original.fingerprint())
        self.assertIn("原成片仍然保留", latest.error)

    async def test_frontend_supported_settings_flow_into_providers(self):
        class RecordingScriptProvider(TemplateScriptProvider):
            prompt = None

            async def generate(self, card, prompt=None):
                self.prompt = prompt
                return await super().generate(card, prompt)

        provider = RecordingScriptProvider()
        self.service.script_provider = provider
        class RecordingStoryboardProvider(TemplateStoryboardProvider):
            base_style = None

            async def generate_with_direction(
                self,
                script,
                director_instruction,
                beat_groups,
                visual_types,
                **kwargs,
            ):
                self.base_style = director_instruction
                return await super().generate_with_direction(
                    script,
                    director_instruction,
                    beat_groups,
                    visual_types,
                    **kwargs,
                )

        storyboard_provider = RecordingStoryboardProvider()
        self.service.storyboard_provider = storyboard_provider
        class RecordingTtsProvider(SilentTtsProvider):
            voice_id = None
            speed_ratio = None

            async def synthesize(
                self,
                script,
                workspace,
                *,
                voice_id=None,
                speed_ratio=1.0,
            ):
                self.voice_id = voice_id
                self.speed_ratio = speed_ratio
                return await super().synthesize(
                    script,
                    workspace,
                    voice_id=voice_id,
                    speed_ratio=speed_ratio,
                )

        tts_provider = RecordingTtsProvider()
        self.service.tts_provider = tts_provider
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        settings = GenerationSettings(
            visual_style_id="papercraft-stop-motion",
            video_resolution="1080p",
            tts_voice_id="zh_male_ruyayichen_saturn_bigtts",
            tts_speed_ratio=1.1,
        )
        job = await self.service.create_job(
            card.id, self.actor, settings
        )
        job = await self.service.generate_script(job.id, self.actor)
        self.assertTrue(provider.prompt.startswith("你是任务冻结的唯一 Script Skill"))
        self.assertIn("【不可变原始输入】", provider.prompt)
        self.assertIn("【唯一 ContextPack（用户提供材料）】", provider.prompt)
        self.assertIn("【Script Skill】evidence-led-explainer@1.1.0", provider.prompt)
        self.assertIn('"external_retrieval": false', provider.prompt)
        self.assertIn("禁止联网搜索", provider.prompt)
        self.assertIn("【冻结的事实、安全与质量政策】", provider.prompt)
        self.assertNotIn("H3", provider.prompt)
        self.assertIn("245-325 个汉字", provider.prompt)
        self.assertIsNotNone(job.editorial_plan)
        self.assertIsNone(job.creative_brief)

        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        self.assertEqual(
            tts_provider.voice_id,
            "zh_male_ruyayichen_saturn_bigtts",
        )
        self.assertEqual(tts_provider.speed_ratio, 1.1)
        self.assertEqual(job.narration_manifest.speed_ratio, 1.1)
        self.assertIn("【唯一视觉负责人】", storyboard_provider.base_style)
        self.assertIn("精致手工纸艺定格", storyboard_provider.base_style)
        self.assertIn("VisualBible", storyboard_provider.base_style)
        self.assertIn("ShotContextIR", storyboard_provider.base_style)
        self.assertNotIn("H3", storyboard_provider.base_style)
        self.assertNotIn(job.editorial_plan.core_thesis, storyboard_provider.base_style)
        requests = job.visual_requests
        self.assertGreaterEqual(len(requests), 1)
        self.assertLessEqual(len(requests), 3)
        self.assertTrue(all(
            request.resolution == "1080p"
            and request.model_id == SEEDANCE_EFFICIENT_MODEL
            and 8 <= request.duration_seconds <= 10
            for request in requests
        ))
        self.assertEqual(
            (job.render_manifest.width, job.render_manifest.height),
            (1080, 1920),
        )
        self.assertTrue(all(
            request.prompt.startswith("【Provider Adapter】seedream-seedance@1.3.0")
            and "【VisualBible】" in request.prompt
            and "【ShotContextIR】" in request.prompt
            and "视频提示词结构" not in request.prompt
            for request in requests
        ))
        legacy = requests[0].model_copy(update={
            "resolution": "720p",
            "duration_seconds": 5,
        })
        job.visual_requests = [legacy]
        self.assertEqual(self.service._build_visual_requests(job), [legacy])

    async def test_progress_events_and_background_package_are_resumable(self):
        events = []
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(
            job.id, self.actor, progress=events.append
        )
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(
            job.id, self.actor, progress=events.append
        )
        job = await self.service.approve_final(
            job.id,
            job.revision,
            job.review_bundle_hash,
            self.actor,
            package_immediately=False,
        )
        self.assertEqual(job.state, JobState.FINAL_APPROVED)
        job = await self.service.package(
            job.id, self.actor, progress=events.append
        )
        self.assertEqual(job.state, JobState.PACKAGED)

        stages = {event["stage"] for event in events}
        self.assertTrue({
            "material_confirmed",
            "script_generation",
            "confirm_script",
            "tts",
            "storyboard",
            "first_frames",
            "seedance_shot_1",
            "seedance_parallel",
            "visual_assets",
            "remotion_render",
            "remotion_normalize",
            "artifact_upload",
            "quality",
            "confirm_final",
            "package",
        }.issubset(stages))
        self.assertNotIn("seedance_shot_2", stages)
        self.assertNotIn("seedance_shot_3", stages)
        self.assertNotIn("frame_selection", stages)

    async def test_legacy_frozen_video_requests_resume_without_seedream_cost(self):
        class ForbiddenImageProvider:
            name = "forbidden-image"

            async def generate(self, prompt, *, seed):
                del prompt, seed
                raise AssertionError("legacy task must not submit Seedream")

            async def download(self, source_url, destination):
                del source_url, destination
                raise AssertionError("legacy task has no Seedream asset")

        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        legacy_requests = [
            item.model_copy(update={
                "resolution": "720p",
                "duration_seconds": 5,
            })
            for item in self.service._build_visual_requests(job)
        ]
        job.visual_requests = legacy_requests
        job = await self.service._save_job(job, self.actor)
        self.service.image_provider = ForbiddenImageProvider()
        job = await self.service.produce(job.id, self.actor)
        self.assertEqual(job.visual_requests, legacy_requests)
        self.assertIsNone(job.storyboard_plan)
        self.assertEqual(job.first_frame_candidates, [])

    async def test_high_risk_card_fails_closed(self):
        card = await self.service.create_source_card(
            valid_card(risk_level="high"), self.actor
        )
        with self.assertRaises(QualityGateFailed):
            await self.service.verify_source_card(
                card.id, card.revision, self.actor
            )

    async def test_team_can_read_but_only_owner_can_mutate_resources(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        stranger = Actor(user_id=8, username="stranger", role="member")
        with self.assertRaises(AccessDenied):
            await self.service.get_source_card(card.id, stranger)
        with self.assertRaises(AccessDenied):
            await self.service.update_source_card(
                card.id, valid_card(), card.revision, stranger
            )
        shared = await self.service.view_source_card(card.id, stranger)
        self.assertEqual(shared.id, card.id)
        self.assertEqual(
            [item.id for item in await self.service.list_source_cards(stranger)],
            [card.id],
        )


class QijiaVideoPermissionTests(unittest.TestCase):
    @staticmethod
    def client_for(user: dict) -> TestClient:
        app = FastAPI()
        app.include_router(qijia_api.api_router)
        app.include_router(qijia_api.page_router)
        app.dependency_overrides[auth_service.get_current_user] = lambda: user
        return TestClient(app)

    def test_create_job_public_settings_expose_only_v2_selections(self):
        properties = set(
            qijia_api.CreateGenerationSettings.model_json_schema()["properties"]
        )
        self.assertEqual(
            properties,
            {
                "skill_id",
                "skill_version",
                "script_skill_id",
                "script_skill_version",
                "director_skill_id",
                "director_skill_version",
                "visual_style_id",
                "visual_style_version",
                "provider_adapter_id",
                "provider_adapter_version",
                "video_resolution",
                "tts_voice_id",
                "tts_speed_ratio",
                "seedance_model",
            },
        )
        for legacy_field in (
            "script_prompt",
            "seedance_prompt",
            "prompt_writing_profile_id",
            "image_count",
            "shot_count",
        ):
            with self.subTest(legacy_field=legacy_field):
                with self.assertRaises(ValidationError):
                    qijia_api.CreateGenerationSettings.model_validate({
                        legacy_field: "legacy",
                    })
        migrated = qijia_api.CreateGenerationSettings.model_validate({
            "director_skill_id": "paper-collage-explainer",
            "director_skill_version": "1.1.0",
        }).to_internal()
        self.assertEqual(migrated.director_skill_id, "animated-explainer")
        self.assertEqual(migrated.director_skill_version, "")
        self.assertEqual(migrated.visual_style_id, "paper-collage-explainer")
        self.assertEqual(migrated.visual_style_version, "1.1.0")

    def test_api_and_page_require_independent_permission(self):
        denied = self.client_for({
            "id": 2,
            "username": "member",
            "role": "member",
            "permissions": ["video"],
        })
        self.assertEqual(denied.get("/api/qijia-video/capabilities").status_code, 403)
        self.assertEqual(denied.get("/qijia-video").status_code, 403)

        allowed = self.client_for({
            "id": 3,
            "username": "editor",
            "role": "member",
            "permissions": ["qijia_video"],
        })
        response = allowed.get("/api/qijia-video/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["module"], "qijia_video")
        self.assertEqual(response.json()["data"]["pipeline_version"], "v2")
        self.assertNotIn("mock", response.json()["data"]["script_provider"])
        self.assertNotIn("mock", response.json()["data"]["tts_provider"])
        self.assertNotIn("mock", response.json()["data"]["video_provider"])
        defaults = response.json()["data"]["generation_defaults"]
        self.assertNotIn("image_count", defaults)
        self.assertNotIn("shot_count", defaults)
        self.assertEqual(
            response.json()["data"]["generation_defaults"]["video_resolution"],
            "1080p",
        )
        self.assertEqual(
            response.json()["data"]["generation_defaults"]["seedance_model"],
            SEEDANCE_EFFICIENT_MODEL,
        )
        self.assertNotIn(
            "seedance_prompt",
            response.json()["data"]["generation_defaults"],
        )
        seedream_pricing = response.json()["data"]["seedream_pricing"]
        self.assertEqual(seedream_pricing["chapter_policy"], "semantic_adaptive")
        self.assertEqual(seedream_pricing["max_video_chapters"], 3)
        skills = response.json()["data"]["content_skills"]
        self.assertEqual(
            {item["skill_id"] for item in skills},
            {"explain-expert-view"},
        )
        self.assertTrue(all(item["external_retrieval"] is False for item in skills))
        skill_response = allowed.get("/api/qijia-video/skills")
        self.assertEqual(skill_response.status_code, 200)
        self.assertEqual(
            {item["skill_id"] for item in skill_response.json()["data"]},
            {"explain-expert-view"},
        )
        self.assertIn("visual_styles", response.json()["data"])
        self.assertNotIn("prompt_writing_profile", response.json()["data"])
        self.assertEqual(
            {item["skill_id"] for item in response.json()["data"]["script_skills"]},
            {"evidence-led-explainer"},
        )
        self.assertEqual(
            {item["skill_id"] for item in response.json()["data"]["director_skills"]},
            {"animated-explainer"},
        )
        self.assertTrue(all(
            item["role"] == "director_owner"
            for item in response.json()["data"]["director_skills"]
        ))
        self.assertTrue(all(
            item["role"] == "visual_language"
            for item in response.json()["data"]["visual_styles"]
        ))
        self.assertEqual(
            response.json()["data"]["provider_adapter"]["adapter_id"],
            "seedream-seedance",
        )
        self.assertEqual(
            allowed.get("/api/qijia-video/script-skills").status_code,
            200,
        )
        self.assertEqual(
            allowed.get("/api/qijia-video/director-skills").status_code,
            200,
        )
        self.assertEqual(
            allowed.get("/api/qijia-video/provider-adapter").status_code,
            200,
        )
        style_response = allowed.get("/api/qijia-video/visual-styles")
        self.assertEqual(style_response.status_code, 200)
        self.assertEqual(
            {item["style_id"] for item in style_response.json()["data"]},
            {
                "content-skill-default",
                "paper-collage-explainer",
                "papercraft-stop-motion",
            },
        )
        tts_pricing = response.json()["data"]["tts_pricing"]
        self.assertEqual(
            [item["id"] for item in tts_pricing["voices"]],
            [
                "zh_female_vv_uranus_bigtts",
                "zh_female_santongyongns_saturn_bigtts",
                "zh_male_ruyayichen_saturn_bigtts",
            ],
        )
        self.assertEqual(
            [item["ratio"] for item in tts_pricing["speed_ratios"]],
            [1.0, 1.1, 1.2],
        )
        self.assertEqual(
            [item["speech_rate"] for item in tts_pricing["speed_ratios"]],
            [0, 10, 20],
        )
        self.assertEqual(tts_pricing["default_speed_ratio"], 1.2)
        self.assertEqual(tts_pricing["preview_max_characters"], 60)
        self.assertEqual(tts_pricing["preview_max_estimated_cost_cny"], 0.03)
        self.assertTrue(tts_pricing["preview_cost_confirmation_required"])
        self.assertEqual(
            response.json()["data"]["seedance_pricing"]["yuan_per_million_tokens"],
            4.2,
        )
        self.assertEqual(
            [
                item["yuan_per_million_tokens"]
                for item in response.json()["data"]["seedance_pricing"]["models"]
            ],
            [4.2, 46.0],
        )
        self.assertEqual(
            response.json()["data"]["seedream_pricing"]["candidates_per_shot"],
            1,
        )
        self.assertEqual(
            response.json()["data"]["douyin_performance"][
                "estimated_cny_per_success"
            ],
            0.0134,
        )
        self.assertEqual(
            response.json()["data"]["douyin_performance"][
                "short_link_estimated_cny"
            ],
            0.0201,
        )
        self.assertEqual(
            response.json()["data"]["douyin_performance"][
                "requests_per_refresh"
            ],
            1,
        )
        self.assertNotIn("frame_evaluator", response.json()["data"])
        self.assertEqual(allowed.get("/qijia-video").status_code, 200)


if __name__ == "__main__":
    unittest.main()
