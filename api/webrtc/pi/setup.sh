#!/bin/bash
# Quick setup script for Raspberry Pi

set -e

echo "========================================="
echo "WebRTC Pi Setup Script"
echo "========================================="
echo ""

# Check if running on Pi
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
    echo "Warning: This doesn't appear to be a Raspberry Pi"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Update system
echo "Updating system packages..."
sudo apt update

# Install GStreamer and dependencies
echo "Installing GStreamer and dependencies..."
sudo apt install -y \
    python3 python3-pip \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gstreamer-1.0 \
    ffmpeg

# Install Python packages
echo "Installing Python packages..."
pip3 install -r requirements.txt

# Create .env template if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env template..."
    cat > .env << 'EOF'
# WebRTC Configuration
SIGNAL_WSS=wss://signal.yourdomain.com/ws/device
DEVICE_ID=dev1
DEVICE_TOKEN=CHANGE_ME_GENERATE_RANDOM_TOKEN

# Camera Configuration
RTSP_URL=rtsp://USER:PASS@CAMERA_IP:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif

# Optional: Use subtype=1 for lighter substream
# RTSP_URL=rtsp://USER:PASS@CAMERA_IP:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif
EOF
    echo ""
    echo "⚠️  Created .env template - YOU MUST EDIT IT with your actual values!"
    echo ""
fi

# Test GStreamer
echo "Testing GStreamer installation..."
if gst-launch-1.0 --version > /dev/null 2>&1; then
    echo "✓ GStreamer installed successfully"
else
    echo "✗ GStreamer installation failed"
    exit 1
fi

# Test Python GStreamer bindings
echo "Testing Python GStreamer bindings..."
if python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst" 2>/dev/null; then
    echo "✓ Python GStreamer bindings working"
else
    echo "✗ Python GStreamer bindings not working"
    exit 1
fi

echo ""
echo "========================================="
echo "Setup Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "1. Edit .env file with your configuration"
echo "2. Test RTSP connection: ffprobe -rtsp_transport tcp \"\$RTSP_URL\""
echo "3. Test raw publisher: python3 publisher_raw.py"
echo "4. Test AI publisher: python3 publisher_ai.py"
echo "5. Run device agent: python3 device_agent.py"
echo ""
echo "Optional: Install as systemd service (see README.md)"
echo ""
