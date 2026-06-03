# RTSP Timelapse Generator

A Python application that captures frames from RTSP camera streams on configurable schedules and generates timelapse videos using FFmpeg.

## Features

- **RTSP Frame Capture** - Captures frames from any RTSP-compatible camera using OpenCV
- **Flexible Scheduling** - Three scheduling modes:
  - `hourly` - Capture once per hour with optional time windows
  - `interval` - Capture every N minutes, hours, or days
  - `x_per_day` - Distribute N captures evenly across the day
- **Time Windows** - Restrict captures to specific hours (e.g., 6 AM - 8 PM)
- **Dawn/Dusk Capture Windows** - Optionally use computed sunrise/sunset times instead of static time windows, adjusting automatically with seasons via the [astral](https://github.com/sffjunkie/astral) library
- **Automatic Timezone Detection** - Timezone is auto-detected from configured latitude/longitude and used for accurate dawn/dusk calculations and consistent time display across the UI
- **Timelapse Generation** - Create MP4 videos from captured frames using FFmpeg
- **Export Time-of-Day Filter** - Filter images by time range (custom hours or dawn-to-dusk) when generating timelapses
- **Export Presets** - Pre-configured encoding settings (standard, fast_preview, high_quality)
- **Web UI** - Optional browser-based management interface
- **Hot-Reload Configuration** - Changes to YAML config files are detected and applied automatically
- **Headless Operation** - Run without the web UI for server deployments

## Requirements

- Python 3.10+
- FFmpeg (must be installed and available in PATH)

### Python Dependencies

```
flask>=3.0.0
opencv-python-headless>=4.8.0
apscheduler>=3.10.0
pyyaml>=6.0
watchdog>=3.0.0
astral>=3.2
timezonefinder>=6.0
```

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Inconel625/rtspse.git
   cd rtspse
   ```

2. Install FFmpeg:
   ```bash
   # Ubuntu/Debian
   sudo apt install ffmpeg

   # macOS
   brew install ffmpeg

   # Windows - download from https://ffmpeg.org/download.html
   ```

3. Create and activate a virtual environment:

   > **Ubuntu/Debian note:** The `python3-venv` package must be installed before this step:
   > ```bash
   > sudo apt install python3-venv
   > ```

   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   # or
   venv\Scripts\activate     # Windows
   ```

4. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

5. Copy and configure the example config files:
   ```bash
   cp config/cameras.yaml.example config/cameras.yaml
   # Edit config/cameras.yaml with your camera details
   ```

## Configuration

All configuration is done through YAML files in the `config/` directory.

### app.yaml - Application Settings

```yaml
web_ui:
  enabled: true          # Enable/disable web interface
  host: 0.0.0.0         # Bind address
  port: 5050            # Web UI port
  auth_enabled: false   # Enable basic authentication
  username: admin       # Auth username (if enabled)
  password: admin       # Auth password (if enabled)

storage:
  captures_path: captures    # Where to store captured images (relative to working directory)
  exports_path: exports      # Where to store generated videos (relative to working directory)
  logs_path: logs           # Log file location (relative to working directory)
  max_log_size_mb: 100      # Max log file size before rotation

log_level: INFO             # DEBUG, INFO, WARNING, ERROR

# Optional: location for dawn/dusk and timezone calculations
location:
  latitude: 51.5074
  longitude: -0.1278
  timezone: Europe/London       # Auto-detected from lat/lng if omitted
```

### cameras.yaml - Camera Configuration

```yaml
cameras:
  My-Camera:
    url: rtsp://username:password@192.168.1.100:554/stream
    enabled: true
    schedules:
      - name: daytime_hourly
        frequency: hourly       # hourly, interval, or x_per_day
        enabled: true
        value: 1                # Depends on frequency type
        interval_unit: hours    # minutes, hours, or days (for interval frequency)
        time_window:
          start: "06:00"
          end: "20:00"
          use_dawn_dusk: false  # Use computed dawn/dusk instead of static times
    capture_settings:
      jpeg_quality: 90          # 1-100
      timeout_seconds: 10       # Connection timeout
      retry_count: 3            # Retries on failure
      retry_delay_seconds: 1.0  # Delay between retries
```

### Schedule Frequency Types

| Type | Value Meaning | Example |
|------|--------------|---------|
| `hourly` | Captures per hour (always 1) | Capture at minute 0 each hour |
| `interval` | Interval between captures | `value: 30, interval_unit: minutes` = every 30 min |
| `x_per_day` | Total captures per day | `value: 12` = 12 times daily |

### exports.yaml - Export Presets

Three built-in presets are available:

| Preset | FPS | Resolution | FFmpeg Preset | Use Case |
|--------|-----|------------|---------------|----------|
| `standard` | 15 | Original | veryfast | General purpose |
| `fast_preview` | 15 | 854x480 | ultrafast | Quick previews |
| `high_quality` | 60 | Original | fast (1.6× bitrate) | Final production |

Encoding uses a capped target bitrate scaled to the output resolution (roughly
7-8 Mbps at 1080p), which keeps file sizes reasonable and makes in-browser playback
smooth. A preset's `bitrate_factor` multiplies that target for higher-quality output.

### Hardware-accelerated encoding (optional)

By default encoding runs on the CPU (libx264). On machines with an Intel/AMD iGPU you
can offload encoding to the GPU via VAAPI by setting `export.hwaccel: auto` in
`app.yaml`. The app uses `/dev/dri/renderD128` when present and automatically falls
back to software if it is missing. On Proxmox, pass the host iGPU into the container/VM
first (for an LXC, add `dev0: /dev/dri/renderD128` to its config).

## Usage

### Running with Web UI

```bash
python -m src.main
```

Access the web interface at `http://localhost:5050`

> **Note:** On first run, Flask will print a warning about running a development server. This is expected for home and local network use. For internet-facing deployments, consider running behind a reverse proxy such as nginx.

### Running Headless (No Web UI)

```bash
python -m src.main --no-web
```

### Custom Config Directory

```bash
python -m src.main --config-dir /path/to/config
```

> **Note:** Storage paths in `app.yaml` (`captures_path`, `exports_path`, `logs_path`) are relative to the working directory from which you run the application, not the config directory. When using `--config-dir`, either run the application from the desired base directory or use absolute paths in `app.yaml`.

### Running as a systemd Service (Linux)

To run the application automatically on boot, create a systemd unit file:

```bash
sudo nano /etc/systemd/system/rtspse.service
```

```ini
[Unit]
Description=RTSP Timelapse Generator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/rtspse
ExecStart=/opt/rtspse/venv/bin/python -m src.main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

> **Note:** Adjust `WorkingDirectory` and `ExecStart` to match your actual install path and virtual environment location.

Then enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rtspse
sudo systemctl start rtspse
```

Check status and logs:

```bash
sudo systemctl status rtspse
sudo journalctl -u rtspse -f
```

To reload configuration without restarting:

```bash
sudo systemctl kill -s HUP rtspse
```

### Signal Handling

- `SIGTERM` / `SIGINT` (Ctrl+C) - Graceful shutdown
- `SIGHUP` - Reload configuration

## Web UI Features

- **Dashboard** - Overview of cameras, schedules, recent captures, and storage
- **Camera Management** - Add, edit, delete, and test camera connections
- **Schedule Editor** - Visual schedule configuration with optional dawn/dusk toggle
- **Export Generator** - Create timelapses with date range selection, time-of-day filtering (all day, custom hours, or dawn-to-dusk), and progress tracking
- **Settings** - Configure application settings and location (for dawn/dusk calculations and timezone)

## API Endpoints

The web UI exposes a REST API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cameras` | GET/POST | List or add cameras |
| `/api/cameras/<name>` | GET/PUT/DELETE | Manage specific camera |
| `/api/cameras/<name>/test` | POST | Test camera connection |
| `/api/cameras/<name>/capture` | POST | Trigger manual capture |
| `/api/schedules` | GET | List all schedules with next run times |
| `/api/captures` | GET | List captured images |
| `/api/exports` | GET/POST | List or create exports |
| `/api/exports/calculate` | POST | Preview export stats without generating |
| `/api/settings/location` | GET/PUT | Get or update location for dawn/dusk |
| `/api/sun` | GET | Get today's computed dawn/dusk times |
| `/api/storage` | GET | Storage statistics |

## File Organization

```
rtspse/
├── config/
│   ├── app.yaml              # Application settings
│   ├── cameras.yaml          # Camera definitions (copy from cameras.yaml.example)
│   └── exports.yaml          # Export presets and history
├── captures/                 # Stored images
│   └── {camera-name}/
│       └── {YYYY-MM}/
│           └── {camera}_{timestamp}.jpg
├── exports/                  # Generated videos
├── logs/                     # Application logs
└── src/                      # Source code
```

## Troubleshooting

### Camera Connection Issues

1. Test the RTSP URL with VLC or ffplay first
2. Check firewall settings on both the camera and server
3. Verify credentials in the RTSP URL
4. Try different stream paths (cameras vary by manufacturer)

### FFmpeg Errors

1. Ensure FFmpeg is installed: `ffmpeg -version`
2. Check that FFmpeg is in your PATH
3. Verify captured images exist in the date range

### Permission Issues

Ensure the application has write access to:
- `captures/` directory
- `exports/` directory
- `logs/` directory

## License

MIT License
