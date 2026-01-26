# RTSP Timelapse Generator - Project Context

## Overview

Python application that captures frames from RTSP camera streams on configurable schedules and generates timelapse videos. Supports both headless operation (config-file driven) and interactive use via optional Flask web UI.

## Tech Stack

- **Python 3.13** - Core runtime
- **Flask 3.0+** - Web UI and REST API
- **OpenCV (headless)** - RTSP frame capture
- **APScheduler** - Cron-like job scheduling
- **FFmpeg** - Timelapse video generation (external binary)
- **PyYAML** - Configuration files
- **Watchdog** - Config hot-reload

## Project Structure

```
/root/rtspse/
├── config/                 # YAML configuration files
│   ├── app.yaml           # Web UI, storage paths, logging
│   ├── cameras.yaml       # Camera definitions and schedules
│   └── exports.yaml       # Export presets and history
├── src/
│   ├── main.py            # Entry point, lifecycle, signal handling
│   ├── models.py          # Dataclasses: CameraConfig, Schedule, ExportPreset, etc.
│   ├── config.py          # ConfigManager - loads/saves YAML configs
│   ├── capture.py         # CaptureManager - RTSP frame capture via OpenCV
│   ├── scheduler.py       # ScheduleManager - APScheduler integration
│   ├── exporter.py        # Exporter - FFmpeg timelapse generation
│   └── web/
│       ├── app.py         # Flask app factory, API routes, page routes
│       ├── templates/     # Jinja2 HTML templates
│       └── static/        # JS/CSS assets
├── captures/              # Stored images: {camera}/{YYYY-MM}/{camera}_{date}.jpg
├── exports/               # Generated MP4 timelapse videos
├── logs/                  # Rotating log files
└── requirements.txt       # Python dependencies
```

## Key Files Reference

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `src/main.py` | App entry point | `main()`, `ConfigFileHandler`, `handle_config_reload()` |
| `src/config.py` | Config loading | `ConfigManager`, `get_config()`, `reload_config()` |
| `src/models.py` | Data models | `CameraConfig`, `Schedule`, `FrequencyType`, `ExportPreset` |
| `src/capture.py` | Frame capture | `CaptureManager.capture_frame()`, `test_connection()` |
| `src/scheduler.py` | Job scheduling | `ScheduleManager`, supports hourly/interval/x_per_day |
| `src/exporter.py` | Video export | `Exporter.generate_timelapse()`, `_run_ffmpeg()` |
| `src/web/app.py` | REST API | 27 endpoints under `/api/*`, pages under `/` |

## Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Run with web UI (default)
python -m src.main

# Run headless (no web UI)
python -m src.main --no-web

# Custom config directory
python -m src.main --config-dir /path/to/config
```

Web UI available at `http://0.0.0.0:5050` (port configured in app.yaml)

## Configuration

### Schedule Frequency Types
- `hourly` - Capture once per hour (at minute 0) within time window
- `interval` - Capture every N hours (value = hours between captures)
- `x_per_day` - Distribute N captures evenly across time window

### Time Windows
Optional start/end times (HH:MM format). Captures only occur within window.

### Capture Settings
- `jpeg_quality`: 1-100 (default 90)
- `timeout_seconds`: RTSP connection timeout (default 10)
- `retry_count`: Retries with exponential backoff (default 3)
- `resolution_scale`: Optional scaling factor (e.g., 0.5 for half size)

## API Endpoints (Key Routes)

```
GET    /api/cameras              - List all cameras with status
POST   /api/cameras              - Add new camera
PUT    /api/cameras/<name>       - Update camera
DELETE /api/cameras/<name>       - Delete camera
POST   /api/cameras/<name>/capture - Manual trigger
GET    /api/cameras/<name>/test  - Test connection

GET    /api/captures             - List captures (paginated)
GET    /api/captures/<path>      - Serve capture image

POST   /api/exports              - Generate timelapse
POST   /api/exports/calculate    - Preview export info
GET    /api/exports/<filename>   - Download video (attachment)
GET    /api/exports/<filename>/stream - Stream video (in-browser playback)
DELETE /api/exports/<filename>   - Delete export

GET    /api/schedules            - List schedules and next runs
GET    /api/storage              - Storage statistics
GET    /api/logs                 - Recent log entries
```

## Known Issues & Technical Debt

### Security (High Priority)
1. **Plaintext credentials** - `models.py:124-125` stores username/password in plain YAML
2. **Timing attack vulnerability** - `web/app.py:62-64` uses `!=` for password comparison instead of `secrets.compare_digest()`
3. **RTSP credentials in URLs** - Displayed in UI/logs without masking

### Thread Safety (Medium Priority)
4. **Global mutable state** - `web/app.py:38-41` and `main.py:23-27` use globals accessed by multiple threads
5. **Config hot-reload race condition** - `ConfigManager.cameras` modified without locks while Flask may be reading

### Production Readiness (Medium Priority)
6. **Flask dev server** - `main.py:209-216` uses `app.run()` instead of WSGI server (Gunicorn/uWSGI)
7. **Blocking exports** - `web/app.py:403-408` blocks request thread during FFmpeg execution
8. **No async task queue** - Long exports should use Celery/RQ for background processing

### Code Quality (Low Priority)
9. **Duplicate date parsing** - `{camera}_{YYYY-MM-DD_HH-MM-SS}.jpg` parsed in 4+ places
10. **Missing file encoding** - `config.py:156` opens files without `encoding='utf-8'`
11. **OpenCV timeout unreliable** - `CAP_PROP_*_TIMEOUT_MSEC` not supported by all backends

### Missing for Production
- No unit tests
- No Docker support
- No health check endpoint
- No rate limiting
- No HTTPS (expects reverse proxy)
- No metrics/monitoring

## File Naming Conventions

### Captures
```
captures/{camera_name}/{YYYY-MM}/{camera_name}_{YYYY-MM-DD_HH-MM-SS}.jpg
```

### Exports
```
exports/{camera_name}_{startYYYYMMDD}_{endYYYYMMDD}_{export_id}.mp4
```

## Hot Reload

- **File watcher**: Monitors `config/` directory for `.yaml` changes
- **Debounce**: 1 second delay before reload triggers
- **SIGHUP**: Manual reload via `kill -HUP <pid>`
- **Behavior**: Adds/removes/updates camera schedules without restart

## Signal Handling

- `SIGTERM` / `SIGINT` - Graceful shutdown
- `SIGHUP` - Trigger config reload

## Dependencies on External Tools

- **FFmpeg** - Must be installed and available in PATH for export functionality

## Current Deployment

- 7 cameras configured (residential/farm property)
- Hourly captures 6 AM - 8 PM
- Web UI on port 5050
- No auth enabled (internal network)

## Development Notes

### Adding a New Schedule Type
1. Add enum value to `FrequencyType` in `models.py`
2. Implement `_create_{type}_job()` in `scheduler.py`
3. Handle in `_create_schedule_jobs()` switch
4. Update UI forms if needed

### Adding a New API Endpoint
1. Add route in `web/app.py` under appropriate section
2. Apply `@require_auth` decorator
3. Use `_config_manager`, `_capture_manager`, etc. globals
4. Return JSON with `jsonify()`

### Testing Camera Connection
```python
from src.capture import CaptureManager
cm = CaptureManager(Path("captures"))
result = cm.test_connection("rtsp://user:pass@host/stream")
print(result)  # {'success': True, 'width': 1920, 'height': 1080, 'fps': 25.0}
```
