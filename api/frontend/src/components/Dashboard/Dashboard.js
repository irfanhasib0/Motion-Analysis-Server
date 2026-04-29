import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Camera, Video, HardDrive, Clock } from 'lucide-react';
import MotionActivityChart from './MotionActivityChart';
import StreamLagChart from './StreamLagChart';
import ResourceUsageChart from './ResourceUsageChart';
import zonesApi from '../../zonesApi';
import './Dashboard.css';

const Dashboard = ({ cameras, recordings, systemInfo }) => {
  const navigate = useNavigate();
  const [allZones, setAllZones] = useState([]);

  useEffect(() => {
    zonesApi.getZonesSummary()
      .then((res) => {
        const flat = [];
        Object.entries(res.data || {}).forEach(([cameraId, data]) => {
          (data.zones || []).forEach((z) => flat.push({ ...z, camera_id: cameraId, camera_name: data.camera_name }));
        });
        setAllZones(flat);
      })
      .catch(() => {});
  }, []);
  const onlineCameras = cameras.filter(c => c.status === 'online').length;
  const recordingCameras = cameras.filter(c => c.status === 'recording').length;
  const totalRecordings = recordings.length;
  const recentRecordings = recordings.slice(0, 5);

  const normalizedProcessUsage = systemInfo.process_usage || systemInfo.process || {};
  const normalizedAverages = systemInfo.averages_5m || {};
  const normalizedAiUsage = systemInfo.ai_process_usage || {};
  const normalizedDiskSize = systemInfo.disk_size || {};
  const processPidLabel = normalizedProcessUsage.pid ?? 'N/A';

  const formatBytes = (bytes) => {
    if (!bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const formatDuration = (seconds) => {
    if (!seconds) return '0s';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    } else if (minutes > 0) {
      return `${minutes}m ${secs}s`;
    } else {
      return `${secs}s`;
    }
  };

  const formatDate = (dateString) => {
    return new Date(dateString).toLocaleString();
  };

  const formatPercent3 = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '0.000%';
    return `${numeric.toFixed(3)}%`;
  };

  const formatMB = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '0.0 MB';
    return `${numeric.toFixed(1)} MB`;
  };

  const formatIO = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '0.0 MB';
    if (numeric >= 1024) {
      return `${(numeric / 1024).toFixed(2)} GB`;
    }
    return `${numeric.toFixed(1)} MB`;
  };

  const formatRate = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '0.00 MB/s';
    return `${numeric.toFixed(2)} MB/s`;
  };

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">Dashboard</h1>
        <p className="page-subtitle">System overview and statistics</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-value">{cameras.length}</div>
          <div className="stat-label">Total Cameras</div>
          <div className="stat-change positive">
            {onlineCameras} online • {recordingCameras} recording
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-value">{totalRecordings}</div>
          <div className="stat-label">Total Recordings</div>
          <div className="stat-change">
            {recordings.filter(r => r.status === 'recording').length} active
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-value">
            {systemInfo.disk_usage ? 
              Math.round(systemInfo.disk_usage.percent_used) + '%' : 
              'N/A'
            }
          </div>
          <div className="stat-label">Disk Usage</div>
          <div className="stat-change">
            {systemInfo.disk_usage ? 
              `${formatBytes(systemInfo.disk_usage.used_gb * 1024**3)} used` :
              'Loading...'
            }
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-value">
            {systemInfo.uptime ? 
              `${systemInfo.uptime.days}d ${systemInfo.uptime.hours}h` : 
              'N/A'
            }
          </div>
          <div className="stat-label">System Uptime</div>
          <div className="stat-change">
            {systemInfo.processing_active ? 
              `${Object.keys(systemInfo.processing_active).length} processing` :
              'No active processing'
            }
          </div>
        </div>
      </div>

      {/* Motion Activity Chart */}
      <div className="content-section" style={{ marginBottom: 16 }}>
        <div className="section-header">
          <h2 className="section-title">24h Motion Activity Pattern</h2>
        </div>
        <MotionActivityChart recordings={recordings} cameras={cameras} allZones={allZones} onDayClick={(date, recordingId, cameraId) => navigate('/live', { state: { date, recordingId, cameraId } })} />
      </div>

      {/* Stream Lag History Chart */}
      <div className="content-section" style={{ marginBottom: 16 }}>
        <div className="section-header">
          <h2 className="section-title">24h Stream Lag History</h2>
        </div>
        <StreamLagChart cameras={cameras} />
      </div>

      {/* CPU & Memory Usage Chart */}
      <div className="content-section" style={{ marginBottom: 16 }}>
        <div className="section-header">
          <h2 className="section-title">24h CPU &amp; Memory Usage</h2>
        </div>
        <ResourceUsageChart />
      </div>

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title">
            System Metrics <span className="section-title-subtle">(PID: {processPidLabel})</span>
          </h2>
        </div>

        <div className="camera-info-grid">
          <div className="camera-info-card">
            <div className="camera-info-header">
              <div className="camera-details">
                <div className="camera-name">Overall System</div>
              </div>
            </div>
            <div className="camera-metadata">
              <div className="metadata-item">
                <span className="metadata-label">CPU Usage:</span>
                <span className="metadata-value">{formatPercent3(normalizedAverages.cpu_usage)}</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">RAM Usage:</span>
                <span className="metadata-value">{formatPercent3(normalizedAverages.memory_usage)}</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">Disk IO:</span>
                <span className="metadata-value">R {formatRate(normalizedAverages.disk_io_read_mb_s)} / W {formatRate(normalizedAverages.disk_io_write_mb_s)}</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">Disk Size:</span>
                <span className="metadata-value">{formatIO(normalizedDiskSize.overall_used_gb * 1024)} / {formatIO(normalizedDiskSize.overall_total_gb * 1024)}</span>
              </div>
            </div>
          </div>

          <div className="camera-info-card">
            <div className="camera-info-header">
              <div className="camera-details">
                <div className="camera-name">Server ({processPidLabel})</div>
              </div>
            </div>
            <div className="camera-metadata">
              <div className="metadata-item">
                <span className="metadata-label">CPU Usage:</span>
                <span className="metadata-value">{formatPercent3(normalizedAverages.process_cpu_percent)}</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">RAM Usage:</span>
                <span className="metadata-value">{formatPercent3(normalizedAverages.process_memory_percent)} ({formatMB(normalizedProcessUsage.memory_mb)})</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">Disk IO:</span>
                <span className="metadata-value">R {formatRate(normalizedAverages.process_disk_io_read_mb_s)} / W {formatRate(normalizedAverages.process_disk_io_write_mb_s)}</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">Disk Size (Rec Dir):</span>
                <span className="metadata-value">{formatIO((normalizedDiskSize.recording_dir_size_gb) * 1024)}</span>
              </div>
            </div>
          </div>

          <div className="camera-info-card">
            <div className="camera-info-header">
              <div className="camera-details">
                <div className="camera-name">AI Tracker ({normalizedAiUsage.mode ?? 'N/A'})</div>
              </div>
            </div>
            <div className="camera-metadata">
              <div className="metadata-item">
                <span className="metadata-label">CPU Usage:</span>
                <span className="metadata-value">{formatPercent3(normalizedAverages.ai_cpu_percent)}</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">RAM Usage:</span>
                <span className="metadata-value">{formatPercent3(normalizedAverages.ai_memory_percent)} ({formatMB(normalizedAiUsage.memory_mb ?? 0)})</span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">PIDs:</span>
                <span className="metadata-value">
                  {(normalizedAiUsage.tracker_count ?? 0) > 0
                    ? normalizedAiUsage.tracker_pids.join(', ')
                    : 'N/A'}
                </span>
              </div>
              <div className="metadata-item">
                <span className="metadata-label">Trackers:</span>
                <span className="metadata-value">{normalizedAiUsage.tracker_count ?? 0}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title">Camera Status</h2>
        </div>

        <div className="camera-info-grid">
          {cameras.map(camera => (
            <div key={camera.id} className="camera-info-card">
              <div className="camera-info-header">
                <div className="camera-icon-wrapper">
                  <Camera size={32} />
                </div>
                <div className="camera-details">
                  <div className="camera-name">{camera.name}</div>
                  <span className={`status status-${camera.status}`}>
                    {camera.status}
                  </span>
                </div>
              </div>

              <div className="camera-metadata">
                <div className="metadata-item">
                  <span className="metadata-label">Type:</span>
                  <span className="metadata-value">{camera.camera_type}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Resolution:</span>
                  <span className="metadata-value">{camera.resolution || 'Unknown'}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">FPS:</span>
                  <span className="metadata-value">{camera.fps || 'N/A'}</span>
                </div>
                <div className="metadata-item">
                  <span className="metadata-label">Location:</span>
                  <span className="metadata-value">{camera.location || 'Not set'}</span>
                </div>
                {camera.last_seen && (
                  <div className="metadata-item">
                    <span className="metadata-label">Last Seen:</span>
                    <span className="metadata-value">{formatDate(camera.last_seen)}</span>
                  </div>
                )}
                {camera.processing_active && (
                  <div className="metadata-item">
                    <span className="metadata-label">Processing:</span>
                    <span className="metadata-value processing-active">
                      {camera.processing_type}
                    </span>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title">Recent Recordings</h2>
        </div>
        
        <div className="recording-list">
          {recentRecordings.length === 0 ? (
            <div className="recording-item">
              <div className="recording-info">
                <div className="recording-name">No recordings found</div>
                <div className="recording-details">
                  <span>Start recording from cameras to see them here</span>
                </div>
              </div>
            </div>
          ) : (
            recentRecordings.map(recording => {
              const camera = cameras.find(c => c.id === recording.camera_id);
              return (
                <div key={recording.id} className="recording-item">
                  <div className="recording-info">
                    <div className="recording-name">
                      {camera?.name || 'Unknown Camera'} - {recording.filename}
                    </div>
                    <div className="recording-details">
                      <span><Clock size={12} /> {formatDate(recording.created_at)}</span>
                      <span><Video size={12} /> {formatDuration(recording.duration)}</span>
                      <span><HardDrive size={12} /> {formatBytes(recording.file_size)}</span>
                      <span className={`status status-${recording.status}`}>
                        {recording.status}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

export default Dashboard;