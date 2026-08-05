import React from 'react';
import {
  AbsoluteFill,
  Audio,
  Img,
  interpolate,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import type {KnowledgeVideoProps, ScreenTextCue, VisualBlock} from './types';

const DESIGN_WIDTH = 1080;
const DESIGN_HEIGHT = 1920;

const palette = [
  ['#0f172a', '#1e293b'],
  ['#172554', '#1e3a8a'],
  ['#292524', '#44403c'],
  ['#1c1917', '#3f3f46'],
];

const Card: React.FC<{block: VisualBlock; index: number}> = ({block, index}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const colors = palette[index % palette.length];
  const opacity = interpolate(
    frame,
    [0, Math.min(12, block.duration_in_frames / 4), Math.max(13, block.duration_in_frames - 12), block.duration_in_frames],
    [0, 1, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const translateY = interpolate(frame, [0, Math.min(fps / 2, block.duration_in_frames / 3)], [24, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(145deg, ${colors[0]} 0%, ${colors[1]} 100%)`,
        color: '#f8fafc',
        padding: '170px 96px 320px',
        justifyContent: 'center',
        opacity,
      }}
    >
      <div style={{transform: `translateY(${translateY}px)`}}>
        <div style={{fontSize: 28, color: '#cbd5e1', letterSpacing: 4, marginBottom: 34}}>
          {block.type === 'quote_card' ? '观点' : block.type === 'source_card' ? '来源与边界' : '家庭教育与心理学'}
        </div>
        {block.headline ? (
          <div style={{fontSize: 68, fontWeight: 750, lineHeight: 1.18, marginBottom: 44}}>
            {block.headline}
          </div>
        ) : null}
        <div style={{fontSize: block.headline ? 42 : 54, fontWeight: 580, lineHeight: 1.55, whiteSpace: 'pre-wrap'}}>
          {block.body}
        </div>
      </div>
    </AbsoluteFill>
  );
};
const Visual: React.FC<{
  block: VisualBlock;
  index: number;
  assetUrl?: string;
  assetDurationSeconds?: number | null;
}> = ({block, index, assetUrl, assetDurationSeconds}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  if (block.type === 'generated_video' && assetUrl) {
    const sourceFrames = Math.max(1, Math.round((assetDurationSeconds || 4) * fps));
    // New manifests explicitly request natural motion. Old persisted manifests
    // omit this field and retain their historical fit-to-chapter behavior.
    const playbackRate = block.playback_rate ?? sourceFrames / Math.max(1, block.duration_in_frames);
    const lastFrame = Math.max(1, block.duration_in_frames - 1);
    const direction = index % 2 === 0 ? 1 : -1;
    const scale = interpolate(frame, [0, lastFrame], [1.015, 1.04], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
    const translateX = interpolate(frame, [0, lastFrame], [-6 * direction, 6 * direction], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
    const translateY = interpolate(frame, [0, lastFrame], [4, -4], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
    return (
      <AbsoluteFill style={{backgroundColor: '#020617', overflow: 'hidden'}}>
        <OffthreadVideo
          src={assetUrl}
          muted
          playbackRate={playbackRate}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            transform: `translate3d(${translateX}px, ${translateY}px, 0) scale(${scale})`,
          }}
        />
        <AbsoluteFill style={{background: 'linear-gradient(180deg, rgba(2,6,23,.05), rgba(2,6,23,.38))'}} />
      </AbsoluteFill>
    );
  }
  if (block.type === 'generated_image' && assetUrl) {
    const lastFrame = Math.max(1, block.duration_in_frames - 1);
    const direction = index % 2 === 0 ? 1 : -1;
    const pullBack = index % 3 === 2;
    const scale = interpolate(
      frame,
      [0, lastFrame],
      pullBack ? [1.12, 1.045] : [1.035, 1.115],
      {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
    );
    const translateX = interpolate(frame, [0, lastFrame], [-18 * direction, 18 * direction], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
    const translateY = interpolate(frame, [0, lastFrame], [10, -10], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
    return (
      <AbsoluteFill style={{backgroundColor: '#020617', overflow: 'hidden'}}>
        <Img
          src={assetUrl}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            transform: `translate3d(${translateX}px, ${translateY}px, 0) scale(${scale})`,
          }}
        />
        <AbsoluteFill style={{background: 'linear-gradient(180deg, rgba(2,6,23,.03), rgba(2,6,23,.34))'}} />
      </AbsoluteFill>
    );
  }
  if (block.type === 'generated_video' || block.type === 'generated_image') {
    return <AbsoluteFill style={{background: 'linear-gradient(145deg, #172554 0%, #292524 100%)'}} />;
  }
  return <Card block={block} index={index} />;
};

const EditorialText: React.FC<{cue: ScreenTextCue}> = ({cue}) => {
  const frame = useCurrentFrame();
  const fadeFrames = Math.min(10, Math.max(3, Math.floor(cue.duration_in_frames / 4)));
  const opacity = interpolate(
    frame,
    [0, fadeFrames, Math.max(fadeFrames + 1, cue.duration_in_frames - fadeFrames), cue.duration_in_frames],
    [0, 1, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const translateY = interpolate(frame, [0, fadeFrames], [18, 0], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
  });
  const isClosing = cue.kind === 'closing';
  const isHeadline = cue.kind === 'headline';
  return (
    <AbsoluteFill
      style={{
        alignItems: 'center',
        justifyContent: 'flex-start',
        padding: `${isClosing ? 420 : isHeadline ? 310 : 350}px 82px 0`,
        opacity,
      }}
    >
      <div
        style={{
          maxWidth: 900,
          transform: `translateY(${translateY}px)`,
          borderLeft: '9px solid #d59b55',
          borderRadius: 20,
          padding: '25px 34px 27px',
          background: 'rgba(17, 24, 39, .70)',
          color: '#fffaf0',
          fontSize: cue.text.length <= 16 ? 58 : 46,
          fontWeight: 760,
          lineHeight: 1.34,
          letterSpacing: 1,
          textAlign: 'left',
          textShadow: '0 3px 18px rgba(0,0,0,.35)',
          boxShadow: '0 16px 46px rgba(0,0,0,.20)',
        }}
      >
        {cue.text}
      </div>
    </AbsoluteFill>
  );
};

export const KnowledgeVideoV1: React.FC<KnowledgeVideoProps> = (props) => {
  const audioUrl = props.resolved_assets[props.audio_asset_id];
  const scaleX = props.width / DESIGN_WIDTH;
  const scaleY = props.height / DESIGN_HEIGHT;
  return (
    <AbsoluteFill style={{backgroundColor: '#0f172a', overflow: 'hidden'}}>
      <div
        style={{
          position: 'absolute',
          width: DESIGN_WIDTH,
          height: DESIGN_HEIGHT,
          transform: `scale(${scaleX}, ${scaleY})`,
          transformOrigin: 'top left',
        }}
      >
      <AbsoluteFill style={{backgroundColor: '#0f172a', fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, sans-serif'}}>
      {props.visual_blocks.map((block, index) => (
        <Sequence
          key={block.id}
          from={block.start_frame}
          durationInFrames={block.duration_in_frames}
          premountFor={30}
        >
          <Visual
            block={block}
            index={index}
            assetUrl={block.asset_id ? props.resolved_assets[block.asset_id] : undefined}
            assetDurationSeconds={block.asset_id ? props.assets.find((asset) => asset.asset_id === block.asset_id)?.duration_seconds : undefined}
          />
        </Sequence>
      ))}

      {audioUrl ? <Audio src={audioUrl} /> : null}

      {props.screen_text_cues.map((cue) => (
        <Sequence key={cue.id} from={cue.start_frame} durationInFrames={cue.duration_in_frames}>
          <EditorialText cue={cue} />
        </Sequence>
      ))}

      {props.subtitle_cues.map((cue) => (
        <Sequence key={cue.id} from={cue.start_frame} durationInFrames={cue.duration_in_frames}>
          <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', padding: '0 74px 178px'}}>
            <div
              style={{
                maxWidth: 930,
                background: 'rgba(2, 6, 23, .82)',
                color: '#fff',
                borderRadius: 22,
                padding: '22px 30px',
                fontSize: 38,
                lineHeight: 1.45,
                fontWeight: 650,
                textAlign: 'center',
                boxShadow: '0 12px 36px rgba(0,0,0,.22)',
              }}
            >
              {cue.text}
            </div>
          </AbsoluteFill>
        </Sequence>
      ))}

      {props.ai_content_label.enabled ? (
        <Sequence
          from={props.ai_content_label.start_frame}
          durationInFrames={props.ai_content_label.duration_in_frames}
        >
          <AbsoluteFill style={{alignItems: 'flex-start', justifyContent: 'flex-start', padding: '76px 64px'}}>
            <div style={{background: 'rgba(2,6,23,.78)', color: '#e2e8f0', borderRadius: 999, padding: '12px 20px', fontSize: 24}}>
              AI 生成内容
            </div>
          </AbsoluteFill>
        </Sequence>
      ) : null}
      </AbsoluteFill>
      </div>
    </AbsoluteFill>
  );
};
