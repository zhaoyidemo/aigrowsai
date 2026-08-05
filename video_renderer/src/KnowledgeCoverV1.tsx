import React from 'react';
import {AbsoluteFill, Img} from 'remotion';
import type {KnowledgeVideoProps} from './types';

const DESIGN_WIDTH = 1080;
const DESIGN_HEIGHT = 1920;

export const KnowledgeCoverV1: React.FC<KnowledgeVideoProps> = (props) => {
  const first = props.visual_blocks[0];
  const headline = props.cover_text || props.video_title || first?.headline || '家庭教育与心理学';
  const body = props.video_title && props.video_title !== headline
    ? props.video_title
    : first?.body || '';
  const coverUrl = props.cover_asset_id
    ? props.resolved_assets[props.cover_asset_id]
    : '';
  const headlineSize = headline.length > 30 ? 68 : headline.length > 20 ? 76 : 84;
  const scaleX = props.width / DESIGN_WIDTH;
  const scaleY = props.height / DESIGN_HEIGHT;
  return (
    <AbsoluteFill style={{background: '#0f172a', overflow: 'hidden'}}>
      <div
        style={{
          position: 'absolute',
          width: DESIGN_WIDTH,
          height: DESIGN_HEIGHT,
          transform: `scale(${scaleX}, ${scaleY})`,
          transformOrigin: 'top left',
        }}
      >
      <AbsoluteFill
        style={{
          background: '#0f172a',
          color: '#f8fafc',
          fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, sans-serif',
          padding: '150px 88px 170px',
          justifyContent: 'space-between',
        }}
      >
      {coverUrl ? (
        <AbsoluteFill>
          <Img src={coverUrl} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
          <AbsoluteFill style={{background: 'linear-gradient(180deg, rgba(2,6,23,.28) 0%, rgba(2,6,23,.48) 42%, rgba(2,6,23,.92) 100%)'}} />
        </AbsoluteFill>
      ) : (
        <AbsoluteFill style={{background: 'linear-gradient(150deg, #0f172a 0%, #1e293b 100%)'}} />
      )}
      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', position: 'relative', zIndex: 1}}>
        <div style={{fontSize: 28, color: '#cbd5e1', letterSpacing: 4}}>家庭教育与心理学</div>
        <div style={{background: 'rgba(2,6,23,.78)', color: '#e2e8f0', borderRadius: 999, padding: '12px 20px', fontSize: 24}}>
          AI 生成内容
        </div>
      </div>
      <div style={{position: 'relative', zIndex: 1}}>
        <div style={{width: 76, height: 8, borderRadius: 999, background: '#38bdf8', marginBottom: 42}} />
        <div style={{fontSize: headlineSize, fontWeight: 760, lineHeight: 1.2, letterSpacing: -2}}>{headline}</div>
        {body ? (
          <div style={{fontSize: 36, color: '#cbd5e1', lineHeight: 1.6, marginTop: 52, maxHeight: 310, overflow: 'hidden'}}>
            {body}
          </div>
        ) : null}
      </div>
      <div style={{fontSize: 26, color: '#94a3b8', position: 'relative', zIndex: 1}}>知识内容 · 请结合完整来源理解</div>
      </AbsoluteFill>
      </div>
    </AbsoluteFill>
  );
};
