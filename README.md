# RTSP Timelapse Generator

A Python application that captures frames from RTSP camera streams on configurable schedules and generates timelapse videos using FFmpeg.

## Features

- **RTSP Frame Capture** - Captures frames from any RTSP-compatible camera using OpenCV
- **Flexible Scheduling** - Three scheduling modes:
  - `hourly` - Capture once per hour with optional time windows
  - `interval` - Capture every N hours
  - `x_per_day` - Distribute N captures evenly across the day
- **Time Windows** - Restrict captures to specific hours (e.g., 6 AM - 8 PM)
- **Timelapse Generation** - Create MP4 videos from captured frames using FFmpeg
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
```

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Inconel625/rtspse.git
   cd rtspse
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   # or
   venv\Scripts\activate     # Windows
   ```

3. Install Python dependencies:
   ```bash
   pip install flask>=3.0.0 opencv-python-headless>=4.8.0 apscheduler>=3.10.0 pyyaml>=6.0 watchdog>=3.0.0
   ```

4. Install FFmpeg:
   ```bash
   # Ubuntu/Debian
   sudo apt install ffmpeg

   # macOS
   brew install ffmpeg

   # Windows - download from https://ffmpeg.org/download.html
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
  captures_path: captures    # Where to store captured images
  exports_path: exports      # Where to store generated videos
  logs_path: logs           # Log file location
  max_log_size_mb: 100      # Max log file size before rotation

log_level: INFO             # DEBUG, INFO, WARNING, ERROR
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
        time_window:
          start: "06:00"
          end: "20:00"
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
| `interval` | Hours between captures | `value: 2` = every 2 hours |
| `x_per_day` | Total captures per day | `value: 12` = 12 times daily |

### exports.yaml - Export Presets

Three built-in presets are available:

| Preset | FPS | Resolution | FFmpeg Preset | Use Case |
|--------|-----|------------|---------------|----------|
| `standard` | 9 | Original | medium | General purpose |
| `fast_preview` | 15 | 854x480 | ultrafast | Quick previews |
| `high_quality` | 60 | Original | slow | Final production |

## Usage

### Running with Web UI

```bash
python -m src.main
```

Access the web interface at `http://localhost:5050`

### Running Headless (No Web UI)

```bash
python -m src.main --no-web
```

### Custom Config Directory

```bash
python -m src.main --config-dir /path/to/config
```

### Signal Handling

- `SIGTERM` / `SIGINT` (Ctrl+C) - Graceful shutdown
- `SIGHUP` - Reload configuration

## Web UI Features

- **Dashboard** - Overview of cameras, schedules, recent captures, and storage
- **Camera Management** - Add, edit, delete, and test camera connections
- **Schedule Editor** - Visual schedule configuration
- **Export Generator** - Create timelapses with date range selection and progress tracking
- **Settings** - Configure application settings

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
| `/api/exports/presets` | GET | List export presets |
| `/api/storage` | GET | Storage statistics |

## File Organization

```
rtspse/
├── config/
│   ├── app.yaml              # Application settings
│   ├── cameras.yaml          # Camera definitions
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
