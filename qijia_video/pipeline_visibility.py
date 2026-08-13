"""Backend-owned production map and per-job execution evidence."""
from __future__ import annotations

from qijia_video.cost_analysis import USD_TO_CNY_RATE
from qijia_video.model_registry import model_display_name


def _model(model_id: str, provider: str, role: str) -> dict:
    value = str(model_id or "")
    return {
        "model_id": value,
        "display_name": model_display_name(value),
        "provider": str(provider or ""),
        "role": role,
    }


def _provider_model_id(provider, field: str = "model") -> str:
    return str(
        getattr(provider, field, "")
        or getattr(provider, "name", "")
    )


def _capability(kind: str, payload: dict, id_key: str) -> dict:
    return {
        "kind": kind,
        "id": str(payload.get(id_key) or ""),
        "version": str(payload.get("version") or ""),
        "display_name": str(payload.get("display_name") or ""),
        "description": str(payload.get("description") or ""),
        "source": "versioned_registry",
    }


def _node(
    node_id: str,
    order: int,
    category: str,
    category_label: str,
    name: str,
    owner: str,
    detail: str,
    input_label: str,
    output_label: str,
    *,
    capabilities: list[dict] | None = None,
    models: list[dict] | None = None,
    tools: list[dict] | None = None,
    calls: str = "",
    operations: list[str] | None = None,
    progress_stages: list[str] | None = None,
    human_gate: bool = False,
) -> dict:
    return {
        "id": node_id,
        "order": order,
        "category": category,
        "category_label": category_label,
        "name": name,
        "owner": owner,
        "detail": detail,
        "input": input_label,
        "output": output_label,
        "capabilities": list(capabilities or []),
        "models": list(models or []),
        "tools": list(tools or []),
        "planned_calls": calls,
        "operations": list(operations or []),
        "progress_stages": list(progress_stages or []),
        "human_gate": human_gate,
    }


def production_pipeline(runtime) -> dict:
    """Return the current v4 map from runtime providers and registries."""

    script_skill = runtime.script_skill_registry.resolve().public_payload()
    director_skill = runtime.director_skill_registry.resolve().public_payload()
    adapter = runtime.provider_adapter_registry.public_default()
    styles = runtime.visual_style_registry.public_catalog()
    default_style = next(
        (item for item in styles if item.get("default")),
        styles[0] if styles else {},
    )
    nodes = [
        _node(
            "creative_input", 1, "human", "你", "输入与知识边界", "创作者",
            "冻结原始请求、用户材料与可选参考图；视频主链不联网。",
            "自然语言请求、已核对材料、可选参考图",
            "CreativeInputSnapshot",
            calls="不调用模型",
            progress_stages=["material_confirmed"],
        ),
        _node(
            "script", 2, "ai_colleague", "AI 同事", "脚本创作",
            script_skill["display_name"],
            "主编初稿、非阻断编辑建议、主编终稿；是否采用由主编决定。",
            "原始请求与用户材料",
            "最终 ScriptDraft 与结构完整性记录",
            capabilities=[_capability("script_skill", script_skill, "skill_id")],
            models=[_model(
                _provider_model_id(runtime.script_provider),
                runtime.script_provider.name,
                "初稿、编辑建议、终稿",
            )],
            calls="固定 3 次",
            operations=[
                "script_draft_generation", "script_critique",
                # Keep retired operation names so historical job costs remain
                # attached to the script node without advertising another gate.
                "script_revision", "script_final_review", "script_generation",
            ],
            progress_stages=["script_generation"],
        ),
        _node(
            "script_review", 3, "human", "你", "脚本确认", "创作者",
            "人工修改并锁定内容唯一真相，确认后才产生配音和画面费用。",
            "最终 ScriptDraft", "script approval",
            calls="人工质量门", progress_stages=["confirm_script"],
            human_gate=True,
        ),
        _node(
            "voice", 4, "production_model", "生成模型", "旁白生成",
            model_display_name(_provider_model_id(
                runtime.tts_provider, "resource_id"
            )),
            "按 ScriptBeat 分别合成、测量真实时长，再零间隔拼成完整旁白。",
            "已确认口播与音色、语速",
            "NarrationManifest 与完整旁白音轨",
            models=[_model(
                _provider_model_id(runtime.tts_provider, "resource_id"),
                runtime.tts_provider.name,
                "逐段 TTS",
            )],
            calls="每个 ScriptBeat 1 次",
            operations=["tts_synthesis"], progress_stages=["tts"],
        ),
        _node(
            "director", 5, "ai_colleague", "AI 同事", "视觉导演",
            director_skill["display_name"],
            "先建立视觉命题与资产圣经，再按真实 TTS 时长导演分镜并独立审片。",
            "确认脚本、逐段真实时长、视觉风格与参考图",
            "DirectorTreatment、VisualBible、AssetBible、StoryboardPlan",
            capabilities=[_capability(
                "director_skill", director_skill, "skill_id"
            )],
            models=[_model(
                _provider_model_id(runtime.storyboard_provider),
                runtime.storyboard_provider.name,
                "视觉开发、分镜、审片与必要修订",
            )],
            calls="正常 3 次，最多 5 次",
            operations=[
                "director_treatment", "storyboard_generation",
                "director_critique", "storyboard_revision",
            ],
            progress_stages=["storyboard"],
        ),
        _node(
            "prompt_method", 6, "creative_method", "创作方法",
            "视觉语言与提示词编译", adapter["display_name"],
            "Visual Style 只管美术语言；H3 Provider Adapter 把导演语义、参考职责和真实时长编译为媒体提示词。",
            "导演产物、Visual Style、参考图职责",
            "Seedream / Seedance 只读生产提示词",
            capabilities=[
                _capability("visual_style", default_style, "style_id"),
                _capability("provider_adapter", adapter, "adapter_id"),
            ],
            calls="本地确定性编译，无模型调用",
        ),
        _node(
            "style_development", 7, "production_model", "生成模型",
            "视觉样片", "Seedream 视觉开发",
            "用同一代表性事件和同一 seed 生成 3 张样片，只比较视觉处理。",
            "H3 图片提示词与可选原始参考图", "3 张视觉开发样片",
            models=[_model(
                _provider_model_id(runtime.image_provider),
                runtime.image_provider.name,
                "视觉样片",
            )],
            calls="固定 3 张", operations=["seedream_style_frame"],
            progress_stages=["style_development"],
        ),
        _node(
            "media_review", 8, "human", "你", "样片与素材安排", "创作者",
            "锁定一张样片并连续安排自有图片或视频，一次确认后才批量生产。",
            "3 张样片与自有素材", "已确认视觉基线与镜头素材计划",
            calls="人工质量门",
            progress_stages=["media_prepare", "media_staged", "confirm_media"],
            human_gate=True,
        ),
        _node(
            "media_generation", 9, "production_model", "生成模型",
            "正式画面生产", "Seedream + Seedance",
            "只为未上传素材的章节生成首帧；仅在连续动作不可替代时生成无声视频。",
            "正式提示词、原图、已选样片与镜头计划",
            "正式首帧、必要视频与自有素材汇合结果",
            models=[
                _model(
                    _provider_model_id(runtime.image_provider),
                    runtime.image_provider.name,
                    "正式首帧",
                ),
                _model(
                    _provider_model_id(runtime.video_provider),
                    runtime.video_provider.name,
                    "必要的连续动作视频",
                ),
            ],
            calls="按实际章节与自有素材动态决定",
            operations=["seedream_image", "seedance_video"],
            progress_stages=[
                "first_frames", "seedance_parallel", "seedance_shot_1",
                "seedance_shot_2", "seedance_shot_3", "visual_assets",
            ],
        ),
        _node(
            "render", 10, "production_tool", "生产工具",
            "合成与自动质检", "Remotion + FFmpeg",
            "把旁白、字幕和已选镜头合成为竖屏成片，并执行自动媒体检查。",
            "完整旁白、字幕时间轴与已选镜头",
            "draft.mp4、封面与 QualityReport",
            tools=[
                {"name": runtime.renderer.name, "role": "视频合成"},
                {"name": runtime.media_packager.name, "role": "媒体标准化"},
                {"name": runtime.quality_checker.name, "role": "自动质检"},
            ],
            calls="本地生产工具",
            progress_stages=[
                "remotion", "remotion_render", "remotion_normalize",
                "quality", "artifact_upload",
            ],
        ),
        _node(
            "final_review", 11, "human", "你", "成片确认与发布包", "创作者",
            "人工确认成片后生成最终视频与发布包；系统不会自动发布。",
            "可预览成片与自动质检结果", "final.mp4 与发布包",
            calls="人工质量门", progress_stages=["confirm_final", "package"],
            human_gate=True,
        ),
    ]
    return {
        "schema_version": "1.0",
        "pipeline_version": "v4",
        "source": "runtime_models_and_versioned_registries",
        "nodes": nodes,
    }


def _usage_cost_cny(record) -> float | None:
    if record.reported_cost is not None:
        amount, currency = float(record.reported_cost), record.reported_currency
    elif record.estimated_cost is not None:
        amount, currency = float(record.estimated_cost), record.estimated_currency
    else:
        return None
    if currency == "CNY":
        return amount
    if currency == "USD":
        return amount * USD_TO_CNY_RATE
    return None


def _job_facts(job) -> dict:
    state = str(getattr(job.state, "value", job.state))
    pipeline_version = str(
        getattr(job.pipeline_version, "value", job.pipeline_version)
    )
    media_mode = str(
        getattr(
            job.pre_generation_media_mode,
            "value",
            job.pre_generation_media_mode,
        )
    )
    approvals = {item.kind: item for item in job.approvals}
    artifact_names = {item.name for item in job.artifacts}
    style_assets = [
        item for item in job.style_frame_candidates if item.asset is not None
    ]
    first_frames = [
        item for item in job.first_frame_candidates if item.asset is not None
    ]
    all_video_tasks = [
        item
        for item in [
            *job.video_tasks,
            *(version.task for version in job.visual_versions),
        ]
        if item is not None
    ]
    video_tasks = {
        (item.provider, item.provider_task_id or item.request_fingerprint): item
        for item in all_video_tasks
    }
    succeeded_videos = [
        item
        for item in video_tasks.values()
        if str(getattr(item.state, "value", item.state)) == "succeeded"
    ]
    storyboard_ready = bool(
        job.storyboard_plan and job.visual_bible and job.asset_bible
    )
    prompt_compiled = bool(
        job.style_frame_candidates
        or job.first_frame_candidates
        or job.visual_requests
    )
    draft_ready = bool(
        job.quality_report
        or "draft.mp4" in artifact_names
        or state in {"final_review_required", "final_approved", "packaged"}
    )
    media_confirmed = (
        media_mode == "confirmed"
        or state
        in {
            "quality_checking",
            "final_review_required",
            "final_approved",
            "packaged",
        }
    )
    return {
        "state": state,
        "pipeline_version": pipeline_version,
        "quality_first": pipeline_version == "v4",
        "media_mode": media_mode,
        "approvals": approvals,
        "style_assets": style_assets,
        "first_frames": first_frames,
        "video_tasks": video_tasks,
        "succeeded_videos": succeeded_videos,
        "storyboard_ready": storyboard_ready,
        "prompt_compiled": prompt_compiled,
        "draft_ready": draft_ready,
        "media_confirmed": media_confirmed,
    }


def _job_statuses(job, facts: dict) -> dict[str, str]:
    state = facts["state"]
    approvals = facts["approvals"]
    quality_first = facts["quality_first"]
    media_mode = facts["media_mode"]
    statuses = {
        "creative_input": "completed",
        "script": (
            "completed"
            if job.script
            else "running" if state == "script_generating" else "pending"
        ),
        "script_review": (
            "completed"
            if "script" in approvals
            else "waiting" if state == "script_review_required" else "pending"
        ),
        "voice": (
            "completed"
            if job.narration_manifest
            else (
                "running"
                if "script" in approvals
                and state in {"script_approved", "producing"}
                else "pending"
            )
        ),
        "director": (
            "completed"
            if facts["storyboard_ready"]
            else (
                "running"
                if job.narration_manifest and state == "producing"
                else "pending"
            )
        ),
        "prompt_method": (
            "completed"
            if facts["prompt_compiled"]
            else "running" if facts["storyboard_ready"] else "pending"
        ),
        "style_development": (
            "skipped"
            if not quality_first
            else (
                "completed"
                if len(facts["style_assets"]) >= 3
                else (
                    "running"
                    if facts["storyboard_ready"] and state == "producing"
                    else "pending"
                )
            )
        ),
        "media_review": (
            "skipped"
            if not quality_first and media_mode == "automatic"
            else (
                "completed"
                if facts["media_confirmed"]
                else "waiting" if state == "media_review_required" else "pending"
            )
        ),
        "media_generation": (
            "completed"
            if facts["draft_ready"]
            else (
                "running"
                if facts["media_confirmed"]
                and state in {"producing", "quality_checking"}
                else "pending"
            )
        ),
        "render": (
            "completed"
            if facts["draft_ready"]
            else "running" if state == "quality_checking" else "pending"
        ),
        "final_review": (
            "completed"
            if state == "packaged"
            else (
                "running"
                if state == "final_approved"
                else "waiting" if state == "final_review_required" else "pending"
            )
        ),
    }
    return statuses


def _output_summaries(job, facts: dict) -> dict[str, str]:
    approvals = facts["approvals"]
    reference_assets = (
        job.input_snapshot.reference_assets
        if job.input_snapshot
        else ((job.source_card_snapshot or {}).get("reference_assets") or [])
    )
    return {
        "creative_input": (
            "原始请求已冻结"
            + (" · 含 1 张参考图" if reference_assets else "")
        ),
        "script": (
            f"{len(job.script.beats)} 个 ScriptBeat · "
            f"{'主编终稿已交付' if job.script_review else '等待交付'}"
            if job.script else "尚未生成 ScriptDraft"
        ),
        "script_review": (
            f"{approvals['script'].actor} 已确认"
            if "script" in approvals else "等待人工确认"
        ),
        "voice": (
            f"{len(job.narration_manifest.segments)} 段实测音频 · "
            f"{job.narration_manifest.total_duration_seconds:.1f} 秒"
            if job.narration_manifest else "尚未生成完整旁白"
        ),
        "director": (
            f"{len(job.storyboard_plan.shots)} 个视觉章节 · "
            f"独立审片{'通过' if job.storyboard_plan.director_review else '待确认'}"
            if job.storyboard_plan else "尚未交付导演方案"
        ),
        "prompt_method": (
            f"样片提示词 {len(job.style_frame_candidates)} 份 · "
            f"正式首帧 {len(job.first_frame_candidates)} 份 · "
            f"视频提示词 {len(job.visual_requests)} 份"
            if facts["prompt_compiled"] else "尚未执行媒体提示词编译"
        ),
        "style_development": (
            (
                f"已生成 {len(facts['style_assets'])}/3 张"
                + (
                    f" · 已选 {job.selected_style_frame_id}"
                    if job.selected_style_frame_id else ""
                )
            )
            if facts["quality_first"]
            else "该历史任务没有视觉样片质量门"
        ),
        "media_review": (
            f"{'已选样片' if job.selected_style_frame_id else '样片待选'}"
            f" · 自有素材 {len(job.shot_media_versions)} 个"
        ),
        "media_generation": (
            f"正式首帧 {len(facts['first_frames'])} 张 · "
            f"成功视频 {len(facts['succeeded_videos'])} 段 · "
            f"自有素材 {len(job.shot_media_versions)} 个"
        ),
        "render": (
            (
                f"自动质检 {job.quality_report.automatic_status}"
                if job.quality_report else "成片草稿已生成"
            )
            if facts["draft_ready"] else "尚未生成成片草稿"
        ),
        "final_review": (
            f"发布资产 {len(job.artifacts)} 个"
            if facts["state"] == "packaged"
            else (
                f"{approvals['final'].actor} 已确认，正在打包"
                if "final" in approvals else "等待人工确认成片"
            )
        ),
    }


def _frozen_capability(kind: str, snapshot, id_field: str) -> dict:
    return {
        "kind": kind,
        "id": str(getattr(snapshot, id_field, "") or ""),
        "version": str(snapshot.version or ""),
        "display_name": str(snapshot.display_name or ""),
        "description": str(snapshot.description or ""),
        "source": "job_frozen_snapshot",
    }


def _freeze_capabilities(node: dict, job) -> None:
    if node["id"] == "script":
        capabilities = []
        if job.script_skill_snapshot:
            snapshot = job.script_skill_snapshot
            node["owner"] = snapshot.display_name
            capabilities.append(_frozen_capability(
                "script_skill", snapshot, "skill_id"
            ))
        if job.pipeline_version != "v4" and job.skill_snapshot:
            capabilities.append(_frozen_capability(
                "legacy_content_skill", job.skill_snapshot, "skill_id"
            ))
        if job.pipeline_version != "v4" and job.prompt_adapter_snapshot:
            capabilities.append(_frozen_capability(
                "legacy_script_prompt_adapter",
                job.prompt_adapter_snapshot,
                "adapter_id",
            ))
        if capabilities:
            node["capabilities"] = capabilities
    elif node["id"] == "director" and job.director_skill_snapshot:
        snapshot = job.director_skill_snapshot
        node["owner"] = snapshot.display_name
        node["capabilities"] = [_frozen_capability(
            "director_skill", snapshot, "skill_id"
        )]
    elif node["id"] == "prompt_method":
        capabilities = []
        if job.visual_style_snapshot:
            capabilities.append(_frozen_capability(
                "visual_style", job.visual_style_snapshot, "style_id"
            ))
        if job.provider_adapter_snapshot:
            snapshot = job.provider_adapter_snapshot
            capabilities.append(_frozen_capability(
                "provider_adapter", snapshot, "adapter_id"
            ))
            node["owner"] = snapshot.display_name
        if job.pipeline_version != "v4" and job.prompt_writing_profile_snapshot:
            snapshot = job.prompt_writing_profile_snapshot
            capabilities.append(_frozen_capability(
                "legacy_prompt_writing_profile", snapshot, "profile_id"
            ))
            if not job.provider_adapter_snapshot:
                node["owner"] = snapshot.display_name
        if capabilities:
            node["capabilities"] = capabilities


def _artifact_model_ids(node_id: str, job, facts: dict) -> list[str]:
    candidates: list[str] = []
    if node_id == "script" and job.script_review:
        candidates.append(job.script_review.model_id)
    elif node_id == "director" and job.storyboard_plan:
        candidates.append(job.storyboard_plan.model_id)
        if job.storyboard_plan.director_review:
            candidates.append(job.storyboard_plan.director_review.model_id)
    elif node_id == "style_development":
        candidates.extend(
            item.model_id for item in job.style_frame_candidates
        )
    elif node_id == "media_generation":
        candidates.extend(
            item.model_id for item in job.first_frame_candidates
        )
        candidates.extend(
            item.model_id for item in facts["video_tasks"].values()
        )
    return list(dict.fromkeys(item for item in candidates if item))


def job_execution_trace(runtime, job) -> dict:
    """Overlay frozen capabilities, artifacts and usage on the current map."""

    pipeline = production_pipeline(runtime)
    nodes = pipeline["nodes"]
    facts = _job_facts(job)
    statuses = _job_statuses(job, facts)
    summaries = _output_summaries(job, facts)
    records = list(job.usage_records)
    matches_current_pipeline = (
        facts["pipeline_version"] == pipeline["pipeline_version"]
    )
    if not matches_current_pipeline:
        for node in nodes:
            node["models"] = []
            node["tools"] = []
            node["planned_calls"] = "历史任务按当时冻结版本执行"
            if node["id"] in {"script", "director", "prompt_method"}:
                node["capabilities"] = []
        next(
            item for item in nodes if item["id"] == "script"
        )["detail"] = (
            "该历史任务按当时冻结的脚本能力执行；这些能力不属于当前 v4 "
            "默认链路。"
        )
        next(
            item for item in nodes if item["id"] == "director"
        )["detail"] = (
            "该历史任务按当时冻结的导演能力与流程生成视觉方案。"
        )
        next(
            item for item in nodes if item["id"] == "prompt_method"
        )["detail"] = (
            "该历史任务仅展示当时冻结的提示词与视觉组件，不借用当前默认值。"
        )
    status_labels = {
        "pending": "尚未开始",
        "running": "进行中",
        "waiting": "等待你",
        "completed": "已完成",
        "failed": "失败",
        "skipped": "本任务跳过",
    }
    operation_to_node = {
        operation: node["id"]
        for node in nodes
        for operation in node.get("operations", [])
    }
    if facts["state"] == "failed":
        failed_record = next(
            (item for item in reversed(records) if not item.succeeded),
            None,
        )
        failed_node = (
            operation_to_node.get(failed_record.operation)
            if failed_record else None
        )
        if not failed_node:
            failed_node = {
                "script": "script",
                "quality": "render",
                "package": "final_review",
            }.get(job.failed_stage)
        if not failed_node:
            failed_node = next(
                (
                    node["id"]
                    for node in nodes
                    if statuses[node["id"]] == "running"
                    and node["id"] != "prompt_method"
                ),
                None,
            )
        if failed_node:
            statuses[failed_node] = "failed"

    for node in nodes:
        _freeze_capabilities(node, job)
        node_records = [
            item
            for item in records
            if item.operation in node.get("operations", [])
        ]
        actual_model_ids = list(dict.fromkeys(
            item.model_id for item in node_records if item.model_id
        ))
        for model_id in _artifact_model_ids(node["id"], job, facts):
            if model_id not in actual_model_ids:
                actual_model_ids.append(model_id)
        known_costs = [
            cost
            for cost in (_usage_cost_cny(item) for item in node_records)
            if cost is not None
        ]
        status = statuses[node["id"]]
        node["status"] = status
        node["status_label"] = status_labels[status]
        node["actual"] = {
            "request_count": sum(
                int(item.request_count) for item in node_records
            ),
            "successful_request_count": sum(
                int(item.request_count)
                for item in node_records if item.succeeded
            ),
            "failed_request_count": sum(
                int(item.request_count)
                for item in node_records if not item.succeeded
            ),
            "input_tokens": sum(item.input_tokens for item in node_records),
            "output_tokens": sum(item.output_tokens for item in node_records),
            "total_tokens": sum(item.total_tokens for item in node_records),
            "known_cost_cny": (
                round(sum(known_costs), 6) if known_costs else None
            ),
            "unpriced_request_count": sum(
                int(item.request_count)
                for item in node_records
                if _usage_cost_cny(item) is None
            ),
            "models": [{
                "model_id": model_id,
                "display_name": model_display_name(model_id),
                "source": (
                    "usage_record"
                    if any(item.model_id == model_id for item in node_records)
                    else "generated_artifact"
                ),
            } for model_id in actual_model_ids],
            "output_summary": summaries[node["id"]],
            "started_at": node_records[0].occurred_at if node_records else "",
            "last_event_at": (
                node_records[-1].occurred_at if node_records else ""
            ),
        }

    return {
        "schema_version": "1.0",
        "pipeline_version": facts["pipeline_version"],
        "current_pipeline_version": pipeline["pipeline_version"],
        "matches_current_pipeline": matches_current_pipeline,
        "source": "job_snapshots_artifacts_and_usage_ledger",
        "nodes": nodes,
    }
