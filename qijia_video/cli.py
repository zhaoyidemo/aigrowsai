"""齐家短视频模块 CLI；与 Web 入口复用同一领域服务。"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from qijia_video.contracts import (
    Actor,
    SourceCardInput,
)
from qijia_video.infrastructure.media import FfmpegMediaPackager
from qijia_video.infrastructure.memory_repository import InMemoryAggregateRepository
from qijia_video.infrastructure.image_providers import MockImageProvider
from qijia_video.infrastructure.mock_providers import (
    SilentTtsProvider,
    TemplateScriptProvider,
    TemplateStoryboardProvider,
)
from qijia_video.infrastructure.quality import FfprobeQualityChecker
from qijia_video.infrastructure.remotion_renderer import RemotionRenderer
from qijia_video.infrastructure.storage import LocalArtifactStorage
from qijia_video.infrastructure.video_providers import MockVideoProvider
from qijia_video.service import QijiaVideoService
from qijia_video.settings import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _demo_card() -> SourceCardInput:
    return SourceCardInput.model_validate({
        "content_domain": "parent_education",
        "content_format": "concept_explainer",
        "subject": {"type": "concept", "name": "自主练习"},
        "title": "孩子学习独立完成任务，需要怎样的练习？",
        "core_idea": "能力的形成需要在安全边界内获得持续、可承担的自主练习。",
        "parent_question": "父母应该什么时候帮助，什么时候让孩子自己尝试？",
        "sources": [{
            "id": "source_01",
            "type": "other",
            "title": "齐家短视频 MVP 工作流测试资料",
            "author": "内容团队",
            "locator": "内部测试卡 v1",
            "rights_status": "verified_for_citation",
        }],
        "verified_facts": [{
            "id": "fact_01",
            "text": "本测试卡只用于验证来源、脚本、渲染、质检和两次人工确认能否形成完整闭环。",
            "source_refs": ["source_01"],
        }],
        "interpretation_boundary": [{
            "id": "boundary_01",
            "text": "不得把一般性练习建议表述为对具体儿童的诊断或治疗方案。",
        }],
    })


def _build_service(root: Path) -> tuple[QijiaVideoService, LocalArtifactStorage]:
    storage = LocalArtifactStorage(root / "storage")
    renderer = RemotionRenderer(
        PROJECT_ROOT / "video_renderer",
        timeout_seconds=settings.QIJIA_VIDEO_RENDER_TIMEOUT,
        node_binary=settings.QIJIA_VIDEO_NODE_BINARY,
        concurrency=settings.REMOTION_CONCURRENCY,
    )
    return QijiaVideoService(
        repository=InMemoryAggregateRepository(),
        script_provider=TemplateScriptProvider(),
        storyboard_provider=TemplateStoryboardProvider(),
        image_provider=MockImageProvider(),
        tts_provider=SilentTtsProvider(),
        video_provider=MockVideoProvider(),
        renderer=renderer,
        storage=storage,
        quality_checker=FfprobeQualityChecker(),
        media_packager=FfmpegMediaPackager(),
        work_root=root / "work",
    ), storage


async def _demo(output_dir: Path) -> dict:
    # 在发生任何渲染成本前先确认目标目录可写。
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="qijia-video-cli-"))
    try:
        service, storage = _build_service(temporary)
        actor = Actor(user_id=1, username="cli-demo", role="member")
        card = await service.create_source_card(_demo_card(), actor)
        card = await service.verify_source_card(card.id, card.revision, actor)
        job = await service.create_job(card.id, actor)
        job = await service.generate_script(job.id, actor)
        job = await service.approve_script(
            job.id, job.revision, job.script_hash, actor
        )
        job = await service.produce(job.id, actor)
        job = await service.approve_final(
            job.id, job.revision, job.review_bundle_hash, actor
        )
        for artifact in job.artifacts:
            await storage.materialize(artifact.asset, output_dir / artifact.name)
        return {
            "job_id": job.id,
            "state": job.state.value,
            "review_bundle_hash": job.review_bundle_hash,
            "output_dir": str(output_dir.resolve()),
            "artifacts": [item.name for item in job.artifacts],
        }
    finally:
        await asyncio.to_thread(shutil.rmtree, temporary, True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m qijia_video.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    schema = subparsers.add_parser("schema", help="输出 source_card JSON Schema")
    schema.add_argument("--output", type=Path)
    demo = subparsers.add_parser("demo", help="生成一条完整的本地占位发布包")
    demo.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "tmp" / "qijia_video_demo"
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "schema":
        value = json.dumps(
            SourceCardInput.model_json_schema(), ensure_ascii=False, indent=2
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(value + "\n", encoding="utf-8")
        else:
            print(value)
        return
    result = asyncio.run(_demo(args.output_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
