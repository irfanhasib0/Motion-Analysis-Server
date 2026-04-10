/**
 * Zone Control API — isolated from api.js.
 * Import this module only in zone-related components.
 */

import axios from 'axios';

const API_BASE_URL =
  process.env.NODE_ENV === 'production'
    ? window.location.origin
    : 'http://localhost:9001';

const TOKEN_KEY = 'nvr_access_token';
const getToken = () => localStorage.getItem(TOKEN_KEY);

const zoneClient = axios.create({ baseURL: `${API_BASE_URL}/api`, timeout: 10000 });

zoneClient.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ─── Zone CRUD ────────────────────────────────────────────────────────────────

export const zonesApi = {
  /**
   * Get all zones (and config metadata) for a single camera.
   * @returns {Promise<{camera_id, zones, updated_at}>}
   */
  getCameraZones: (cameraId) => zoneClient.get(`/cameras/${cameraId}/zones`),

  /**
   * Create a new zone for a camera.
   * @param {string}  cameraId
   * @param {object}  zone  – { name, color, zone_type, polygons, hit_mode?,
   *                           min_dwell_frames?, enabled?, include_background? }
   */
  createZone: (cameraId, zone) => zoneClient.post(`/cameras/${cameraId}/zones`, zone),

  /** Update an existing zone (PATCH-style — only send changed fields). */
  updateZone: (cameraId, zoneId, partial) =>
    zoneClient.put(`/cameras/${cameraId}/zones/${zoneId}`, partial),

  /** Delete a zone. */
  deleteZone: (cameraId, zoneId) =>
    zoneClient.delete(`/cameras/${cameraId}/zones/${zoneId}`),

  /** Enable or disable a zone without modifying anything else. */
  toggleZone: (cameraId, zoneId, enabled) =>
    zoneClient.patch(`/cameras/${cameraId}/zones/${zoneId}/enabled`, { enabled }),

  /**
   * Summary of all zones across every camera — used by filter dropdowns.
   * @returns {Promise<{ [camera_id]: CameraZoneConfig }>}
   */
  getZonesSummary: () => zoneClient.get('/zones/summary'),
};

export default zonesApi;
