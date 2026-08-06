import React from 'react';
import {Composition} from 'remotion';
import {KnowledgeVideoV1} from './KnowledgeVideoV1';
import {KnowledgeCoverV1} from './KnowledgeCoverV1';
import type {KnowledgeVideoProps} from './types';

const DEFAULT_WIDTH = 480;
const DEFAULT_HEIGHT = 854;

const defaultProps: KnowledgeVideoProps = {
  schema_version: '1.0',
  job_id: 'preview',
  renderer: 'remotion',
  composition_id: 'KnowledgeVideoV1',
  template_version: 'neutral_knowledge_v2',
  width: DEFAULT_WIDTH,
  height: DEFAULT_HEIGHT,
  fps: 30,
  duration_in_frames: 1800,
  video_title: '',
  cover_text: '',
  assets: [],
  cover_asset_id: '',
  resolved_assets: {},
  audio_asset_id: '',
  visual_blocks: [],
  subtitle_cues: [],
  screen_text_cues: [],
  brand_overlay: null,
};

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="KnowledgeVideoV1"
      component={KnowledgeVideoV1}
      width={DEFAULT_WIDTH}
      height={DEFAULT_HEIGHT}
      fps={30}
      durationInFrames={1800}
      defaultProps={defaultProps}
      calculateMetadata={({props}) => ({
        durationInFrames: Math.max(1, Math.floor(props.duration_in_frames)),
        fps: props.fps,
        width: props.width,
        height: props.height,
      })}
    />
    <Composition
      id="KnowledgeCoverV1"
      component={KnowledgeCoverV1}
      width={DEFAULT_WIDTH}
      height={DEFAULT_HEIGHT}
      fps={30}
      durationInFrames={1}
      defaultProps={defaultProps}
      calculateMetadata={({props}) => ({
        durationInFrames: 1,
        fps: props.fps,
        width: props.width,
        height: props.height,
      })}
    />
  </>
);
