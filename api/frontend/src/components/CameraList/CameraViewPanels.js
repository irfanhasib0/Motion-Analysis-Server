import React, { useEffect, useRef, useState } from 'react';
import { api } from '../../api';

export const AUDIO_TOGGLE_STYLE = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: '32px',
  height: '32px',
  borderRadius: '8px',
  border: '1px solid #2f3743',
  cursor: 'pointer',
};

const AUDIO_PANEL_STYLE = {
  display: 'inline-flex',
  flexDirection: 'column',
  alignItems: 'stretch',
  width: '140px',
  flex: '0 0 auto',
  padding: '4px 6px',
  border: '1px solid rgba(148,163,184,0.25)',
  borderRadius: '10px',
  background: 'rgba(10,14,20,0.35)',
};

const AUDIO_PLAYER_STYLE = { width: '100%', maxWidth: '100%', height: '22px' };

// Backend always streams raw PCM wrapped in a WAV header — format is fixed.
const AUDIO_STREAM_FORMAT = 'wav';

export const SENSITIVITY_LEVEL = 5;
export const DEFAULT_SENSITIVITY = 2;

const getCameraAspectRatio = (resolution) => {
  const match = /^([0-9]+)x([0-9]+)$/i.exec(resolution || '');
  if (!match) {
    return '16 / 9';
  }
  const width = parseInt(match[1], 10) || 16;
  const height = parseInt(match[2], 10) || 9;
  return `${width} / ${height}`;
};

export const CameraAudioPanel = ({ camera }) => {
  const audioRef = useRef(null);
  const isOnline = camera.status === 'online' || camera.status === 'recording';
  const shouldRenderAudio = isOnline && Boolean(camera.audio_enabled);
  const [audioPlaybackFormat] = useState(AUDIO_STREAM_FORMAT);
  const [activeAudioUrl, setActiveAudioUrl] = useState('');

  // Reset audio URL when camera changes
  useEffect(() => {
    setActiveAudioUrl('');
  }, [camera.id]);

  // Start audio when camera comes online
  useEffect(() => {
    if (!shouldRenderAudio) {
      // Stop audio if camera goes offline
      setActiveAudioUrl('');
      const audioEl = audioRef.current;
      if (audioEl) {
        audioEl.pause();
        audioEl.src = '';
      }
      return;
    }

    // Camera is online and audio enabled - start audio stream
    const streamUrl = api.getCameraAudioStreamUrl(camera.id, audioPlaybackFormat);
    setActiveAudioUrl(streamUrl);
    
    // Auto-play when audio source is set
    setTimeout(() => {
      const audioEl = audioRef.current;
      if (audioEl && streamUrl) {
        audioEl.play().catch(() => {
          console.log('Audio autoplay blocked - user must click play');
        });
      }
    }, 500);
  }, [shouldRenderAudio, camera.id, audioPlaybackFormat]);

  if (!shouldRenderAudio) {
    return null;
  }

  return (
    <div style={AUDIO_PANEL_STYLE}>
      <audio
        ref={audioRef}
        src={activeAudioUrl}
        controls={true}
        autoPlay={true}
        muted={false}
        preload="metadata"
        style={AUDIO_PLAYER_STYLE}
      />
    </div>
  );
};

export const CameraVideoPanel = ({ camera, variant = 'primary' }) => {
  const isOnline = camera.status === 'online' || camera.status === 'recording';

  const mediaStyle = {
    width: '100%',
    height: 'auto',
    aspectRatio: getCameraAspectRatio(camera.resolution),
    objectFit: 'contain',
    background: '#000',
  };

  if (!isOnline) {
    return (
      <img
        src={`${api.getBlankStreamUrl(camera.id)}`}
        alt={`Camera ${camera.name}`}
        className="camera-stream"
        style={mediaStyle}
      />
    );
  }

  const streamUrl = variant === 'support'
    ? api.appendQueryParams(api.getProcessingStreamUrl(camera.id), { view: 'support' })
    : api.getCameraVideoStreamUrl(camera.id, 'mjpeg');

  return (
    <img
      key={`${camera.id}:${variant}:mjpeg`}
      src={streamUrl}
      alt={variant === 'support' ? `Support view for ${camera.name}` : `Camera ${camera.name}`}
      className="camera-stream"
      style={mediaStyle}
    />
  );
};
