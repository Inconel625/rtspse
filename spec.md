# RTSP Timelapse Generator - Project Specification for Claude Code

## Project Overview
Android app that captures images from RTSP camera streams on configurable schedules and generates timelapse videos. Designed for headless operation with optional web UI.

## Core Architecture

### Technology Stack
- **Backend**: Python with Flask (lightweight, good RTSP library support)
- **RTSP Handling**: `opencv-python` or `ffmpeg-python`
- **Scheduling**: `APScheduler` for cron-like job management
- **Configuration**: YAML files (human-readable, easy to edit)
- **Web UI**: Flask + simple HTML/CSS/JS (optional, can be disabled)
- **Video Export**: `ffmpeg` for timelapse generation

### File Structure
```
timelapse-app/
├── config/
│   ├── app.yaml          # Global settings (UI enabled/disabled, paths, etc)
│   ├── cameras.yaml      # Camera definitions and schedules
│   └── exports.yaml      # Export presets and history
├── captures/
│   └── [camera_name]/
│       └── YYYY-MM/
│           └── [camera]_YYYY-MM-DD_HH-MM-SS.jpg
├── exports/
│   └── [export_name]_YYYY-MM-DD.mp4
├── src/
│   ├── main.py           # Application entry point
│   ├── capture.py        # RTSP capture logic
│   ├── scheduler.py      # Schedule management
│   ├── exporter.py       # Timelapse generation
│   └── web/
│       ├── app.py        # Flask routes
│       └── templates/    # HTML templates
└── logs/
```

## Feature Specifications

### 1. Camera Configuration (cameras.yaml)
```yaml
cameras:
  - name: "front_door"
    rtsp_url: "rtsp://username:password@192.168.1.100:554/stream1"
    enabled: true
    schedules:
      - name: "hourly_daytime"
        enabled: true
        frequency: "hourly"  # hourly, x_per_day, interval
        interval_hours: 1    # Used when frequency = "interval"
        times_per_day: null  # Used when frequency = "x_per_day"
        time_window:
          start: "06:00"
          end: "22:00"

      - name: "every_4_hours"
        enabled: true
        frequency: "interval"
        interval_hours: 4
        time_window: null    # null = 24/7

    capture_settings:
      resolution: [1920, 1080]  # null = stream native
      quality: 85              # JPEG quality 1-100
      timeout_seconds: 10
      retry_attempts: 3
```

### 2. Global Settings (app.yaml)
```yaml
app:
  web_ui:
    enabled: true
    host: "0.0.0.0"
    port: 5000

  storage:
    base_path: "./captures"
    organize_by_month: true
    filename_format: "{camera}_{date}_{time}.jpg"  # Uses strftime formatting

  logging:
    level: "INFO"
    file: "./logs/app.log"
    max_size_mb: 100
```

### 3. Schedule Types Detail

**Frequency Options:**
- `hourly`: Capture once per hour during time window
- `x_per_day`: Evenly distribute X captures across time window (or 24h if no window)
- `interval`: Capture every X hours

**Implementation Logic:**
```python
# x_per_day example: 12 times per day between 6am-10pm (16 hours)
# = every 1.33 hours = captures at: 6:00, 7:20, 8:40, 10:00, 11:20, 12:40, 14:00, 15:20, 16:40, 18:00, 19:20, 20:40
```

### 4. File Naming & Organization

**Directory Structure:**
```
captures/
├── front_door/
│   ├── 2025-01/
│   │   ├── front_door_2025-01-15_06-00-00.jpg
│   │   ├── front_door_2025-01-15_07-00-00.jpg
│   │   └── ...
│   └── 2025-02/
│       └── ...
└── backyard/
    └── ...
```

**Filename Components:**
- Camera name
- ISO date: YYYY-MM-DD
- Time: HH-MM-SS (24-hour)
- Extension: .jpg

### 5. Export Configuration

**Web UI Export Interface:**
- Camera selector (dropdown)
- Date range picker (start/end dates with time)
- Speed selector:
  - Frames per second (FPS): 1, 5, 10, 15, 24, 30, 60
  - Custom FPS input
- Preview calculations:
  - Total images found: X
  - Real time span: Y days, Z hours
  - Output duration at selected FPS: A minutes, B seconds
  - Estimated file size

**Export Process:**
```python
# User selects:
# - Camera: "front_door"
# - Range: 2025-01-01 00:00 to 2025-01-31 23:59
# - FPS: 24

# App calculates:
# - Images found: 744 (31 days × 24 hours)
# - Real time: 31 days (744 hours)
# - Output duration: 31 seconds (744 frames ÷ 24 fps)
# - File size estimate: ~15 MB (depends on resolution/quality)
```

**Export Settings (exports.yaml):**
```yaml
export_presets:
  - name: "standard"
    fps: 24
    resolution: [1920, 1080]
    codec: "libx264"
    quality: "high"  # high, medium, low

  - name: "fast_preview"
    fps: 60
    resolution: [1280, 720]
    codec: "libx264"
    quality: "medium"

export_history:
  - camera: "front_door"
    start_date: "2025-01-01T00:00:00"
    end_date: "2025-01-31T23:59:59"
    fps: 24
    output_file: "exports/front_door_january_2025-02-01.mp4"
    created_at: "2025-02-01T10:30:00"
    image_count: 744
    duration_seconds: 31
```

### 6. Headless Operation Requirements

**All operations must be performable via config files:**

1. **Add camera**: Edit `cameras.yaml`, restart app or send SIGHUP
2. **Modify schedule**: Edit camera's schedules in YAML
3. **Generate export**: Add to exports.yaml with `auto_generate: true`:
```yaml
pending_exports:
  - camera: "front_door"
    start_date: "2025-01-01T00:00:00"
    end_date: "2025-01-31T23:59:59"
    fps: 24
    output_name: "january_timelapse"
    auto_generate: true
```
4. **Disable UI**: Set `app.web_ui.enabled: false` in app.yaml

**File watcher should monitor config changes and reload without restart when possible.**

### 7. Web UI Features

**Dashboard:**
- Camera status cards (online/offline, last capture time, image count)
- Active schedules display
- Recent captures preview (thumbnail grid)
- Storage usage stats

**Camera Management:**
- Add/edit/delete cameras
- Test RTSP connection
- Enable/disable cameras and schedules
- Manual capture trigger

**Schedule Editor:**
- Visual schedule builder
- Conflict detection (overlapping schedules)
- Next capture time preview

**Export Generator:**
- Date range calendar picker
- FPS slider with preview
- Progress bar during generation
- Export history/download links

**Settings:**
- Storage path configuration
- Log viewer
- UI enable/disable toggle
- Backup/restore configuration

## Implementation Phases

### Phase 1: Core Capture System
- RTSP connection and image capture
- Configuration file parsing
- Basic scheduling (single schedule type)
- File organization and naming

### Phase 2: Advanced Scheduling
- Multiple schedule support per camera
- All frequency types (hourly, x_per_day, interval)
- Time window enforcement
- Schedule conflict detection

### Phase 3: Export System
- Timelapse generation with ffmpeg
- Date range selection
- FPS configuration
- Duration/size calculations

### Phase 4: Web UI
- Flask app setup
- Dashboard and monitoring
- Camera/schedule management
- Export interface

### Phase 5: Polish
- Configuration file hot-reload
- Error handling and retry logic
- Logging and monitoring
- Documentation

## Technical Considerations

**RTSP Capture:**
```python
# Use opencv for frame capture
import cv2

def capture_frame(rtsp_url, output_path, timeout=10):
    cap = cv2.VideoCapture(rtsp_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Minimize latency

    if cap.isOpened():
        ret, frame = cap.read()
        if ret:
            cv2.imwrite(output_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    cap.release()
```

**Scheduling:**
```python
# APScheduler example
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

# Hourly capture
scheduler.add_job(
    capture_frame,
    'cron',
    hour='6-22',  # Time window
    args=[camera_url, output_path]
)
```

**Export Generation:**
```bash
# ffmpeg command structure
ffmpeg -framerate 24 -pattern_type glob -i 'captures/front_door/2025-01/*.jpg' \
       -c:v libx264 -pix_fmt yuv420p -preset medium \
       exports/front_door_january.mp4
```

## Error Handling

- **Connection failures**: Retry with exponential backoff, log failure
- **Storage full**: Stop captures, send alert (if UI disabled, log critically)
- **Invalid config**: Validate on load, reject with detailed error messages
- **Missing images in range**: Warn user, generate with available images
- **Schedule conflicts**: Warn but allow (capture same image twice if needed)

## Security Considerations

- Store RTSP credentials in separate file (gitignored)
- Web UI should have optional authentication
- Validate all file paths to prevent directory traversal
- Rate limit capture attempts to prevent abuse

## Configuration Validation

Implement schema validation for all YAML files:
- Required fields check
- Type validation (URLs, dates, numbers)
- Range validation (FPS 1-120, quality 1-100)
- RTSP URL format verification

---

**This specification provides a complete roadmap for Claude Code to implement a production-ready RTSP timelapse system with both UI and headless operation modes.**
