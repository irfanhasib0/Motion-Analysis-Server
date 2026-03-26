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
  width: '280px',
  flex: '0 0 auto',
  padding: '4px 8px',
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

const WsStreamCanvas = ({ cameraId, style }) => {
  const canvasRef = useRef(null);

  useEffect(() => {
    const wsUrl = api.getWsStreamUrl(cameraId);
    if (!wsUrl) return undefined;

    let closed = false;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onmessage = (event) => {
      if (closed || !canvasRef.current) return;
      const blob = new Blob([event.data], { type: 'image/jpeg' });
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => {
        if (closed || !canvasRef.current) { URL.revokeObjectURL(url); return; }
        const canvas = canvasRef.current;
        canvas.width = img.width;
        canvas.height = img.height;
        canvas.getContext('2d').drawImage(img, 0, 0);
        URL.revokeObjectURL(url);
      };
      img.src = url;
    };

    ws.onerror = () => { if (!closed) ws.close(); };

    return () => { closed = true; ws.close(); };
  }, [cameraId]);

  return <canvas ref={canvasRef} className="camera-stream" style={style} />;
};

export const CameraAudioPanel = ({ camera, disabled = false }) => {
  const audioRef = useRef(null);
  const isOnline = camera.status === 'online' || camera.status === 'recording';
  const shouldRenderAudio = isOnline && Boolean(camera.audio_enabled) && !disabled;
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

export const CameraVideoPanel = ({ camera, variant = 'primary', liveStreamMode, hlsFailedByCamera, setHlsFailedByCamera }) => {
  const videoRef = useRef(null);
  const isOnline = camera.status === 'online' || camera.status === 'recording';
  const isWsStream = liveStreamMode === 'ws' && variant === 'primary';
  const isHlsStream = liveStreamMode === 'hls' && variant === 'primary' && !hlsFailedByCamera[camera.id];

  useEffect(() => {
    if (!isOnline || !isHlsStream || !videoRef.current) {
      return undefined;
    }

    let destroyed = false;
    let hlsInstance = null;
    const videoEl = videoRef.current;
    const sourceUrl = api.getCameraVideoStreamUrl(camera.id, 'hls');

    const attachStream = async () => {
      if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {
        videoEl.src = sourceUrl;
        try { await videoEl.play(); } catch {}
        return;
      }

      try {
        const hlsModule = await import('hls.js');
        const Hls = hlsModule.default;
        if (destroyed || !videoRef.current) {
          return;
        }

        if (Hls.isSupported()) {
          hlsInstance = new Hls({
            lowLatencyMode: true,
            liveSyncDurationCount: 3,
          });
          hlsInstance.on(Hls.Events.ERROR, (_event, data) => {
            if (data?.fatal) {
              setHlsFailedByCamera((prev) => ({ ...prev, [camera.id]: true }));
            }
          });
          hlsInstance.loadSource(sourceUrl);
          hlsInstance.attachMedia(videoRef.current);
          hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
            if (!destroyed && videoRef.current) {
              videoRef.current.play().catch(() => {});
            }
          });
        } else {
          videoRef.current.src = sourceUrl;
          videoRef.current.play().catch(() => {});
        }
      } catch (error) {
        console.error('Failed to initialize HLS stream:', error);
        setHlsFailedByCamera((prev) => ({ ...prev, [camera.id]: true }));
        if (!destroyed && videoRef.current) {
          videoRef.current.src = sourceUrl;
        }
      }
    };

    attachStream();

    return () => {
      destroyed = true;
      if (hlsInstance) {
        hlsInstance.destroy();
      }
      if (videoEl) {
        try {
          videoEl.pause();
          videoEl.removeAttribute('src');
          videoEl.load();
        } catch {}
      }
    };
  }, [camera.id, isHlsStream, isOnline]);

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

  if (isWsStream) {
    return <WsStreamCanvas cameraId={camera.id} style={mediaStyle} />;
  }

  if (isHlsStream) {
    return (
      <video
        ref={videoRef}
        key={`${camera.id}:${variant}:hls`}
        className="camera-stream"
        style={mediaStyle}
        autoPlay
        playsInline
        controls
        onError={() => setHlsFailedByCamera((prev) => ({ ...prev, [camera.id]: true }))}
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
