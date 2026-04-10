from onvif import ONVIFDiscovery

# Create discovery instance
discovery = ONVIFDiscovery(timeout=5)

# Discover devices
devices = discovery.discover()

# Or with
# Discover with search filter by types or scopes (case-insensitive substring match)
devices = discovery.discover(search="Profile/Streaming")

# Display discovered devices
for device in devices:
    print(f"Found device at {device['host']}:{device['port']}")
    print(f"  Scopes: {device.get('scopes', [])}")
    print(f"  XAddrs: {device['xaddrs']}")
