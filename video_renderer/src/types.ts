export type AssetRef = {
  asset_id: string;
  object_key: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
  duration_seconds?: number | null;
};

export type VisualBlock = {
  id: string;
  type: 'generated_video' | 'generated_image' | 'title_card' | 'quote_card' | 'source_card';
  shot_id?: string;
  start_frame: number;
  duration_in_frames: number;
  asset_id?: string | null;
  playback_rate?: number | null;
  headline?: string;
  body?: string;
  source_refs?: string[];
};

export type SubtitleCue = {
  id: string;
  start_frame: number;
  duration_in_frames: number;
  text: string;
};

export type ScreenTextCue = {
  id: string;
  start_frame: number;
  duration_in_frames: number;
  text: string;
  kind: 'headline' | 'emphasis' | 'closing';
};

export type KnowledgeVideoProps = {
  schema_version: string;
  job_id: string;
  renderer: 'remotion';
  composition_id: 'KnowledgeVideoV1';
  template_version: string;
  width: 480 | 720 | 1080;
  height: 854 | 1280 | 1920;
  fps: 30;
  duration_in_frames: number;
  video_title: string;
  cover_text: string;
  assets: AssetRef[];
  cover_asset_id: string;
  resolved_assets: Record<string, string>;
  audio_asset_id: string;
  visual_blocks: VisualBlock[];
  subtitle_cues: SubtitleCue[];
  screen_text_cues: ScreenTextCue[];
  brand_overlay: null;
};
