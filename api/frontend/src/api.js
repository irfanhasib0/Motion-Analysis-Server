import axios from 'axios';

const API_BASE_URL = process.env.NODE_ENV === 'production' 
  ? window.location.origin 
  : 'http://localhost:9001';

const TOKEN_STORAGE_KEY = 'nvr_access_token';
const RECORDING_PLAYBACK_MODE_KEY = 'nvr_recording_playback_mode';

const getStoredToken = () => localStorage.getItem(TOKEN_STORAGE_KEY);

const getStoredRecordingPlaybackMode = () => {
  const mode = localStorage.getItem(RECORDING_PLAYBACK_MODE_KEY);
  return mode === 'stream' ? 'stream' : 'play';
};

const buildUrlWithToken = (path) => {
  const token = getStoredToken();
  const base = `${API_BASE_URL}${path}`;
  if (!token) {
    return base;
  }
  const separator = base.includes('?') ? '&' : '?';
  return `${base}${separator}access_token=${encodeURIComponent(token)}`;
};

const appendQueryParams = (url, params = {}) => {
  const entries = Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '');
  if (entries.length === 0) {
    return url;
  }
  const separator = url.includes('?') ? '&' : '?';
  const query = entries
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join('&');
  return `${url}${separator}${query}`;
};

const apiClient = axios.create({
  baseURL: `${API_BASE_URL}/api`,
  timeout: 10000,
});

// Request interceptor
apiClient.interceptors.request.use(
  (config) => {
    const token = getStoredToken();
    if (token) {
      config.headers = config.headers || {};
      config.headers.Authorization = `Bearer ${token}`;
    }
    console.log(`API Request: ${config.method?.toUpperCase()} ${config.url}`);
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response interceptor
apiClient.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    console.error('API Error:', error);
    return Promise.reject(error);
  }
);

export const api = {
  // Auth endpoints
  login: (password) => apiClient.post('/auth/login', { password }),
  setAccessToken: (token) => {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    }
  },
  getAccessToken: () => getStoredToken(),
  clearAccessToken: () => localStorage.removeItem(TOKEN_STORAGE_KEY),
  getRecordingPlaybackMode: () => getStoredRecordingPlaybackMode(),
  setRecordingPlaybackMode: (mode) => {
    const normalized = mode === 'stream' ? 'stream' : 'play';
    localStorage.setItem(RECORDING_PLAYBACK_MODE_KEY, normalized);
  },
  getLiveStreamMode: async () => {
    const response = await apiClient.get('/system/live-stream-mode');
    const mode = response?.data?.live_stream_mode;
    return mode === 'hls' ? 'hls' : 'mjpeg';
  },
  setLiveStreamMode: async (mode) => {
    const normalized = mode === 'hls' ? 'hls' : 'mjpeg';
    const response = await apiClient.post('/system/live-stream-mode', { mode: normalized });
    const updatedMode = response?.data?.live_stream_mode;
    return updatedMode === 'hls' ? 'hls' : 'mjpeg';
  },
  appendQueryParams,

  // Camera endpoints
  getCameras: () => apiClient.get('/cameras'),
  createCamera: (camera) => apiClient.post('/cameras', camera),
  updateCamera: (cameraId, updates) => apiClient.put(`/cameras/${cameraId}`, updates),
  deleteCamera: (cameraId) => apiClient.delete(`/cameras/${cameraId}`),
  startCamera: (cameraId) => apiClient.post(`/cameras/${cameraId}/start`),
  stopCamera: (cameraId) => apiClient.post(`/cameras/${cameraId}/stop`),
  
  // Recording endpoints
  startRecording: (cameraId) => {
    console.log('API startRecording called for camera:', cameraId);
    return apiClient.post(`/cameras/${cameraId}/start-recording`);
  },
  stopRecording: (cameraId) => apiClient.post(`/cameras/${cameraId}/stop-recording`),
  getRecordings: (cameraId = null) => {
    const params = cameraId ? { camera_id: cameraId } : {};
    return apiClient.get('/recordings', { params });
  },
  getRecordingStorageInfo: () => apiClient.get('/recordings/storage'),
  deleteRecording: (recordingId) => apiClient.delete(`/recordings/${recordingId}`),
  updateRecordingMeta: (recordingId, data) => apiClient.patch(`/recordings/${recordingId}/meta`, data),
  downloadRecording: (recordingId) => {
    return buildUrlWithToken(`/api/recordings/${recordingId}/download`);
  },
  
  // Streaming endpoints
  getCameraMjpegStreamUrl: (cameraId) =>
    appendQueryParams(buildUrlWithToken(`/api/cameras/${cameraId}/stream`), { mode: 'mjpeg' }),
  getCameraHlsManifestUrl: (cameraId) => buildUrlWithToken(`/api/cameras/${cameraId}/hls/index.m3u8`),
  getCameraStreamModeInfo: (cameraId, mode = 'mjpeg') =>
    apiClient.get(`/cameras/${cameraId}/stream`, { params: { mode } }),
  getCameraVideoStreamUrl: (cameraId, mode = 'mjpeg') => {
    return mode === 'hls'
      ? buildUrlWithToken(`/api/cameras/${cameraId}/hls/index.m3u8`)
      : appendQueryParams(buildUrlWithToken(`/api/cameras/${cameraId}/stream`), { mode: 'mjpeg' });
  },
  getCameraAudioStreamUrl: (cameraId, fmt = 'wav', nonce = null) => {
    const base = buildUrlWithToken(`/api/cameras/${cameraId}/audio_stream?fmt=${encodeURIComponent(fmt)}`);
    return nonce ? appendQueryParams(base, { nonce }) : base;
  },
  startCameraAudioStream: (cameraId, fmt = 'wav') =>
    apiClient.post(`/cameras/${cameraId}/audio_stream/start`, null, { params: { fmt } }),
  getCameraAudioAnalysis: (cameraId) =>
    apiClient.get(`/cameras/${cameraId}/audio_stream/analysis`),
  getCameraSensitivity: (cameraId) =>
    apiClient.get(`/cameras/${cameraId}/sensitivity`),
  setCameraSensitivity: (cameraId, sensitivity) =>
    apiClient.put(`/cameras/${cameraId}/sensitivity`, { sensitivity }),
  stopCameraAudioStream: (cameraId) => apiClient.post(`/cameras/${cameraId}/audio_stream/stop`),
  stopCameraHlsStream: (cameraId) => apiClient.post(`/cameras/${cameraId}/hls/stop`),
  
  getBlankStreamUrl: (cameraId) => buildUrlWithToken(`/api/cameras/${cameraId}/stream/blank`),
  getRecordingStreamUrl: (recordingId, mode = getStoredRecordingPlaybackMode()) => {
    const endpoint = mode === 'stream'
      ? `/api/recordings/${recordingId}/stream`
      : `/api/recordings/${recordingId}/play`;
    return buildUrlWithToken(endpoint);
  },
  getProcessingStreamUrl: (cameraId) => buildUrlWithToken(`/api/cameras/${cameraId}/processing_stream`),
  getResultStreamUrl: (cameraId) => buildUrlWithToken(`/api/cameras/${cameraId}/result_stream`),
  
  // System endpoints
  getSystemInfo: () => apiClient.get('/system/info'),
  getSystemSettings: () => apiClient.get('/system/settings'),
  updateSystemSettings: (settings) => apiClient.put('/system/settings', settings),
  
  // Preset management endpoints
  getSystemPresets: () => apiClient.get('/system/presets'),
  updatePerformanceProfile: (payload) => apiClient.put('/system/performance-profile', payload),

  // Stream health monitoring endpoints
  getCameraStreamHealth: (cameraId) => apiClient.get(`/cameras/${cameraId}/stream-health`),
  getAllCamerasStreamHealth: () => apiClient.get('/system/stream-health'),

  // Archive endpoints
  exportArchive: (filters = {}) => apiClient.post('/recordings/archive/export', filters),
  listArchives: () => apiClient.get('/recordings/archive/list'),
  loadArchive: (archivePath) => apiClient.post('/recordings/archive/load', { archive_path: archivePath }),
  unloadArchive: (archivePath) => apiClient.post('/recordings/archive/unload', { archive_path: archivePath }),
};

export default api;