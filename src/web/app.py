"""Flask web application for RTSP Timelapse Generator."""

import copy
import functools
import hmac
import logging
import os
import threading
import uuid
from datetime import datetime, time
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    Blueprint,
    jsonify,
    request,
    render_template,
    send_file,
    abort,
    Response
)

from ..config import ConfigManager
from ..capture import CaptureManager
from ..scheduler import ScheduleManager
from ..exporter import Exporter, ExportError
from ..models import (
    CameraConfig,
    LocationConfig,
    Schedule,
    FrequencyType,
    TimeWindow,
    CaptureSettings,
    PendingExport,
)

logger = logging.getLogger(__name__)

# Global references (set by create_app)
_config_manager: Optional[ConfigManager] = None
_capture_manager: Optional[CaptureManager] = None
_schedule_manager: Optional[ScheduleManager] = None
_exporter: Optional[Exporter] = None

api = Blueprint('api', __name__, url_prefix='/api')
pages = Blueprint('pages', __name__)


# ============== Helpers ==============

def _is_safe_path(base: Path, target: Path) -> bool:
    """Return True if target resolves inside base (prevents path traversal)."""
    return str(target.resolve()).startswith(str(base.resolve()))


def _clamp(value, min_val, max_val):
    """Clamp a numeric value to [min_val, max_val]."""
    return max(min_val, min(max_val, value))


def _schedule_to_dict(s: Schedule) -> dict:
    """Serialise a Schedule to a JSON-friendly dict."""
    return {
        'name': s.name,
        'frequency': s.frequency.value,
        'enabled': s.enabled,
        'value': s.value,
        'interval_unit': s.interval_unit,
        'time_window': {
            'start': s.time_window.start.strftime('%H:%M'),
            'end': s.time_window.end.strftime('%H:%M'),
            'use_dawn_dusk': s.time_window.use_dawn_dusk,
        } if s.time_window else None
    }


def require_auth(f):
    """Decorator for basic auth if enabled."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if _config_manager and _config_manager.app_config.web_ui.auth_enabled:
            auth = request.authorization
            if not auth:
                return Response(
                    'Authentication required',
                    401,
                    {'WWW-Authenticate': 'Basic realm="RTSP Timelapse"'}
                )

            expected_user = _config_manager.app_config.web_ui.username
            expected_pass = _config_manager.app_config.web_ui.password

            # Use constant-time comparison to prevent timing attacks
            user_ok = hmac.compare_digest(auth.username, expected_user)
            pass_ok = hmac.compare_digest(auth.password, expected_pass)
            if not (user_ok and pass_ok):
                return Response('Invalid credentials', 401)

        return f(*args, **kwargs)
    return decorated


# ============== API Routes ==============

@api.route('/cameras', methods=['GET'])
@require_auth
def list_cameras():
    """List all cameras with status."""
    cameras = []

    for name, camera in _config_manager.cameras.items():
        captures = _capture_manager.get_captures_for_camera(name)
        last_capture = None
        last_capture_path = None
        last_capture_time = None
        if captures:
            last_capture = captures[-1].name
            last_capture_path = str(captures[-1].relative_to(_capture_manager.captures_path))
            # Parse timestamp from filename
            try:
                filename = captures[-1].stem
                date_str = "_".join(filename.split("_")[1:])
                capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")
                last_capture_time = capture_time.isoformat() + 'Z'
            except ValueError:
                pass

        cameras.append({
            'name': name,
            'url': camera.url,
            'enabled': camera.enabled,
            'schedule_count': len(camera.schedules),
            'capture_count': len(captures),
            'last_capture': last_capture,
            'last_capture_path': last_capture_path,
            'last_capture_time': last_capture_time,
            'schedules': [_schedule_to_dict(s) for s in camera.schedules]
        })

    return jsonify(cameras)


@api.route('/cameras/<name>/capture-dates', methods=['GET'])
@require_auth
def camera_capture_dates(name):
    """Per-day capture counts for a camera, for the export date picker."""
    if name not in _config_manager.cameras:
        return jsonify({'error': 'Camera not found'}), 404

    days = _capture_manager.get_capture_date_counts(name)
    sorted_days = sorted(days.keys())
    return jsonify({
        'camera': name,
        'days': days,
        'min_date': sorted_days[0] if sorted_days else None,
        'max_date': sorted_days[-1] if sorted_days else None,
        'total': sum(days.values()),
    })


@api.route('/cameras', methods=['POST'])
@require_auth
def add_camera():
    """Add a new camera."""
    data = request.json

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Camera name is required'}), 400

    if '_' in name:
        return jsonify({'error': 'Camera name may not contain underscores'}), 400

    if name in _config_manager.cameras:
        return jsonify({'error': 'Camera already exists'}), 400

    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Camera URL is required'}), 400

    if not _config_manager._validate_camera_url(url):
        return jsonify({'error': 'Invalid URL. Must start with rtsp://, rtsps://, http://, or https://'}), 400

    schedules = []
    for sched_data in data.get('schedules', []):
        tw = None
        if sched_data.get('time_window'):
            tw = TimeWindow(
                start=datetime.strptime(sched_data['time_window']['start'], '%H:%M').time(),
                end=datetime.strptime(sched_data['time_window']['end'], '%H:%M').time(),
                use_dawn_dusk=sched_data['time_window'].get('use_dawn_dusk', False)
            )

        schedules.append(Schedule(
            name=sched_data.get('name', 'default'),
            frequency=FrequencyType(sched_data.get('frequency', 'hourly')),
            enabled=sched_data.get('enabled', True),
            value=sched_data.get('value', 1),
            interval_unit=sched_data.get('interval_unit', 'hours'),
            time_window=tw
        ))

    camera = CameraConfig(
        name=name,
        url=url,
        enabled=data.get('enabled', True),
        schedules=schedules,
        capture_settings=CaptureSettings(
            jpeg_quality=_clamp(data.get('jpeg_quality', 90), 1, 100),
            timeout_seconds=_clamp(data.get('timeout_seconds', 10), 1, 120),
            retry_count=_clamp(data.get('retry_count', 3), 0, 10)
        )
    )

    _config_manager.cameras[name] = camera
    _config_manager.save_cameras_config()
    _schedule_manager.update_camera(camera)

    logger.info(f"Added camera: {name}")
    return jsonify({'success': True, 'name': name}), 201


@api.route('/cameras/<name>', methods=['PUT'])
@require_auth
def update_camera(name):
    """Update a camera."""
    if name not in _config_manager.cameras:
        return jsonify({'error': 'Camera not found'}), 404

    data = request.json
    camera = _config_manager.cameras[name]

    if 'url' in data:
        camera.url = data['url']
    if 'enabled' in data:
        camera.enabled = data['enabled']

    if 'schedules' in data:
        schedules = []
        for sched_data in data['schedules']:
            tw = None
            if sched_data.get('time_window'):
                tw = TimeWindow(
                    start=datetime.strptime(sched_data['time_window']['start'], '%H:%M').time(),
                    end=datetime.strptime(sched_data['time_window']['end'], '%H:%M').time(),
                    use_dawn_dusk=sched_data['time_window'].get('use_dawn_dusk', False)
                )

            schedules.append(Schedule(
                name=sched_data.get('name', 'default'),
                frequency=FrequencyType(sched_data.get('frequency', 'hourly')),
                enabled=sched_data.get('enabled', True),
                value=sched_data.get('value', 1),
                interval_unit=sched_data.get('interval_unit', 'hours'),
                time_window=tw
            ))
        camera.schedules = schedules

    if 'capture_settings' in data:
        cs = data['capture_settings']
        camera.capture_settings.jpeg_quality = _clamp(
            cs.get('jpeg_quality', camera.capture_settings.jpeg_quality), 1, 100)
        camera.capture_settings.timeout_seconds = _clamp(
            cs.get('timeout_seconds', camera.capture_settings.timeout_seconds), 1, 120)
        camera.capture_settings.retry_count = _clamp(
            cs.get('retry_count', camera.capture_settings.retry_count), 0, 10)

    _config_manager.save_cameras_config()
    _schedule_manager.update_camera(camera)

    logger.info(f"Updated camera: {name}")
    return jsonify({'success': True})


@api.route('/cameras/<name>', methods=['DELETE'])
@require_auth
def delete_camera(name):
    """Delete a camera."""
    if name not in _config_manager.cameras:
        return jsonify({'error': 'Camera not found'}), 404

    _schedule_manager.remove_camera(name)
    del _config_manager.cameras[name]
    _config_manager.save_cameras_config()

    logger.info(f"Deleted camera: {name}")
    return jsonify({'success': True})


@api.route('/cameras/<name>/capture', methods=['POST'])
@require_auth
def trigger_capture(name):
    """Manually trigger a capture."""
    if name not in _config_manager.cameras:
        return jsonify({'error': 'Camera not found'}), 404

    camera = _config_manager.cameras[name]
    result = _capture_manager.capture_frame(camera)

    if result:
        return jsonify({
            'success': True,
            'path': str(result.relative_to(_capture_manager.captures_path))
        })
    else:
        return jsonify({'success': False, 'error': 'Capture failed'}), 500


@api.route('/cameras/<name>/test', methods=['GET'])
@require_auth
def test_camera(name):
    """Test camera connection."""
    if name not in _config_manager.cameras:
        return jsonify({'error': 'Camera not found'}), 404

    camera = _config_manager.cameras[name]
    result = _capture_manager.test_connection(camera.url)

    return jsonify(result)


@api.route('/schedules', methods=['GET'])
@require_auth
def list_schedules():
    """List all schedules."""
    schedules = []

    for camera_name, camera in _config_manager.cameras.items():
        for schedule in camera.schedules:
            entry = _schedule_to_dict(schedule)
            entry['camera'] = camera_name
            schedules.append(entry)

    next_runs = _schedule_manager.get_next_run_times()

    return jsonify({
        'schedules': schedules,
        'next_runs': next_runs,
        'jobs': _schedule_manager.get_all_jobs()
    })


@api.route('/captures', methods=['GET'])
@require_auth
def list_captures():
    """List captures with pagination."""
    limit = request.args.get('limit', 50, type=int)
    camera = request.args.get('camera')

    if camera:
        captures = []
        for path in _capture_manager.get_captures_for_camera(camera):
            try:
                filename = path.stem
                date_str = "_".join(filename.split("_")[1:])
                capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")
                captures.append({
                    'camera': camera,
                    'path': str(path.relative_to(_capture_manager.captures_path)),
                    'timestamp': capture_time.isoformat() + 'Z',
                    'filename': path.name
                })
            except ValueError:
                continue
        captures.sort(key=lambda x: x['timestamp'], reverse=True)
        captures = captures[:limit]
    else:
        captures = _capture_manager.get_recent_captures(limit)

    return jsonify(captures)


@api.route('/captures/<path:capture_path>', methods=['GET'])
@require_auth
def serve_capture(capture_path):
    """Serve a capture image."""
    full_path = _capture_manager.captures_path / capture_path

    if not _is_safe_path(_capture_manager.captures_path, full_path):
        abort(403)

    if not full_path.exists():
        abort(404)

    return send_file(full_path, mimetype='image/jpeg')


@api.route('/exports', methods=['GET'])
@require_auth
def list_exports():
    """List export history with optional pagination."""
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    all_files = _exporter.list_exports()
    all_history = _config_manager.export_history

    paginated_files = all_files[offset:offset + limit]
    paginated_history = [
        {
            'id': h.id,
            'camera': h.camera,
            'start_date': h.start_date.isoformat(),
            'end_date': h.end_date.isoformat(),
            'preset': h.preset,
            'output_file': h.output_file,
            'created_at': h.created_at.isoformat() + 'Z',
            'image_count': h.image_count,
            'duration_seconds': h.duration_seconds,
            'file_size_bytes': h.file_size_bytes,
            'start_time': h.start_time,
            'end_time': h.end_time,
        }
        for h in all_history[offset:offset + limit]
    ]

    return jsonify({
        'exports': paginated_files,
        'history': paginated_history,
        'total_exports': len(all_files),
        'total_history': len(all_history),
        'limit': limit,
        'offset': offset,
        'presets': {
            name: {
                'fps': p.fps,
                'width': p.width,
                'height': p.height,
                'codec': p.codec
            }
            for name, p in _config_manager.export_presets.items()
        }
    })


@api.route('/exports', methods=['POST'])
@require_auth
def create_export():
    """Create a new export."""
    data = request.json

    camera = data.get('camera')
    if not camera or camera not in _config_manager.cameras:
        return jsonify({'error': 'Invalid camera'}), 400

    try:
        start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d')
        end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d')
        end_date = end_date.replace(hour=23, minute=59, second=59)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format'}), 400

    preset_name = data.get('preset', 'standard')
    if preset_name not in _config_manager.export_presets:
        return jsonify({'error': 'Invalid preset'}), 400

    # Work on a copy so the shared preset object is never mutated
    preset = copy.copy(_config_manager.export_presets[preset_name])

    fps = data.get('fps')
    if fps is not None:
        if not isinstance(fps, int) or fps <= 0:
            return jsonify({'error': 'fps must be a positive integer'}), 400
        preset.fps = fps

    # Parse optional time-of-day filter
    start_time = None
    end_time = None
    use_dawn_dusk = data.get('use_dawn_dusk', False)
    if not use_dawn_dusk:
        if data.get('start_time'):
            start_time = datetime.strptime(data['start_time'], '%H:%M').time()
        if data.get('end_time'):
            end_time = datetime.strptime(data['end_time'], '%H:%M').time()

    location = _config_manager.app_config.location
    export_id = str(uuid.uuid4())[:8]

    smoothing = data.get('smoothing', 'none')
    if smoothing not in ('none', 'blend'):
        return jsonify({'error': 'Invalid smoothing option'}), 400

    def run_export():
        try:
            history = _exporter.generate_timelapse(
                camera=camera,
                start_date=start_date,
                end_date=end_date,
                preset=preset,
                start_time=start_time,
                end_time=end_time,
                use_dawn_dusk=use_dawn_dusk,
                location=location,
                export_id=export_id,
                smoothing=smoothing
            )
            _config_manager.export_history.append(history)
            _config_manager.save_exports_config()
        except Exception as e:
            logger.error(f"Background export failed: {e}")

    thread = threading.Thread(target=run_export, daemon=True)
    thread.start()

    return jsonify({'success': True, 'message': 'Export started', 'export_id': export_id}), 202


@api.route('/exports/calculate', methods=['POST'])
@require_auth
def calculate_export():
    """Calculate export info without generating."""
    data = request.json

    camera = data.get('camera')
    if not camera or camera not in _config_manager.cameras:
        return jsonify({'error': 'Invalid camera'}), 400

    try:
        start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d')
        end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d')
        end_date = end_date.replace(hour=23, minute=59, second=59)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format'}), 400

    fps = data.get('fps', 30)

    # Parse optional time-of-day filter
    start_time = None
    end_time = None
    use_dawn_dusk = data.get('use_dawn_dusk', False)
    if not use_dawn_dusk:
        if data.get('start_time'):
            start_time = datetime.strptime(data['start_time'], '%H:%M').time()
        if data.get('end_time'):
            end_time = datetime.strptime(data['end_time'], '%H:%M').time()

    location = _config_manager.app_config.location

    # Resolve output resolution from the selected preset so the size estimate uses
    # the same bitrate model the encoder will.
    preset = _config_manager.export_presets.get(data.get('preset', 'standard'))
    width = preset.width if preset else None
    height = preset.height if preset else None
    bitrate_factor = preset.bitrate_factor if preset else 1.0

    info = _exporter.calculate_export_info(camera, start_date, end_date, fps, start_time, end_time,
                                           use_dawn_dusk=use_dawn_dusk, location=location,
                                           width=width, height=height, bitrate_factor=bitrate_factor)
    return jsonify(info)


@api.route('/exports/<export_id>/progress', methods=['GET'])
@require_auth
def export_progress(export_id):
    """Get progress of an active export."""
    progress = _exporter.get_export_progress(export_id)
    if progress is None:
        return jsonify({'error': 'Export not found'}), 404
    return jsonify(progress)


@api.route('/exports/<export_id>/cancel', methods=['POST'])
@require_auth
def cancel_export_route(export_id):
    """Cancel an active export."""
    if _exporter.cancel_export(export_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Export not found or not cancellable'}), 404


@api.route('/exports/<filename>', methods=['GET'])
@require_auth
def download_export(filename):
    """Download an export file."""
    export_path = _exporter.exports_path / filename

    if not _is_safe_path(_exporter.exports_path, export_path):
        abort(403)

    if not export_path.exists():
        abort(404)

    return send_file(export_path, as_attachment=True)


@api.route('/exports/<filename>/stream', methods=['GET'])
@require_auth
def stream_export(filename):
    """Stream an export file for in-browser playback."""
    export_path = _exporter.exports_path / filename

    if not _is_safe_path(_exporter.exports_path, export_path):
        abort(403)

    if not export_path.exists():
        abort(404)

    response = send_file(export_path, mimetype='video/mp4', conditional=True)
    response.cache_control.max_age = 3600
    response.cache_control.public = True
    return response


@api.route('/exports/<filename>', methods=['DELETE'])
@require_auth
def delete_export(filename):
    """Delete an export file and its history entry."""
    try:
        found = _exporter.delete_export(filename)
    except ExportError:
        abort(403)

    if not found:
        return jsonify({'error': 'Export not found'}), 404

    # Remove matching history entry so the UI doesn't show a broken link
    _config_manager.export_history = [
        h for h in _config_manager.export_history
        if h.output_file != filename
    ]
    _config_manager.save_exports_config()

    return jsonify({'success': True})


@api.route('/storage', methods=['GET'])
@require_auth
def storage_stats():
    """Get storage statistics."""
    captures_stats = _capture_manager.get_storage_stats()
    exports_stats = _exporter.get_exports_storage_stats()

    return jsonify({
        'captures': captures_stats,
        'exports': exports_stats,
        'total_size_bytes': captures_stats['total_size_bytes'] + exports_stats['total_size_bytes']
    })


@api.route('/logs', methods=['GET'])
@require_auth
def get_logs():
    """Get recent log entries."""
    limit = request.args.get('limit', 100, type=int)
    logs_path = _config_manager.get_logs_path() / 'rtspse.log'

    if not logs_path.exists():
        return jsonify({'lines': []})

    try:
        file_size = logs_path.stat().st_size
        # Read at most 1MB from end of file
        read_size = min(file_size, 1024 * 1024)
        with open(logs_path, 'rb') as f:
            f.seek(max(0, file_size - read_size))
            data = f.read().decode('utf-8', errors='replace')
        lines = data.splitlines()[-limit:]
        return jsonify({'lines': lines})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api.route('/settings/location', methods=['GET'])
@require_auth
def get_location():
    """Get configured location."""
    loc = _config_manager.app_config.location
    if loc:
        return jsonify({
            'latitude': loc.latitude,
            'longitude': loc.longitude,
            'elevation': loc.elevation,
            'timezone': loc.timezone,
            'configured': True
        })
    return jsonify({'configured': False})


@api.route('/settings/location', methods=['PUT'])
@require_auth
def update_location():
    """Update location configuration."""
    data = request.json

    latitude = data.get('latitude')
    longitude = data.get('longitude')
    elevation = data.get('elevation', 0.0)

    if latitude is None or longitude is None:
        return jsonify({'error': 'Latitude and longitude are required'}), 400

    try:
        lat = float(latitude)
        lon = float(longitude)
    except (ValueError, TypeError):
        return jsonify({'error': 'Latitude and longitude must be numbers'}), 400

    if not (-90 <= lat <= 90):
        return jsonify({'error': 'Latitude must be between -90 and 90'}), 400
    if not (-180 <= lon <= 180):
        return jsonify({'error': 'Longitude must be between -180 and 180'}), 400

    _config_manager.app_config.location = LocationConfig(
        latitude=float(latitude),
        longitude=float(longitude),
        elevation=float(elevation)
    )
    # Detect timezone explicitly before saving so save_app_config() has no side effects
    _config_manager.auto_detect_timezone()
    _config_manager.save_app_config()
    _schedule_manager.set_location(_config_manager.app_config.location)

    logger.info(f"Updated location: {latitude}, {longitude}")
    return jsonify({'success': True})


@api.route('/sun', methods=['GET'])
@require_auth
def get_sun_times():
    """Get today's dawn and dusk times."""
    times = _schedule_manager.get_dawn_dusk_times()
    if times:
        return jsonify(times)
    return jsonify({'error': 'Location not configured or calculation failed'}), 400


# ============== Page Routes ==============

@pages.route('/')
@require_auth
def dashboard():
    """Dashboard page."""
    return render_template('dashboard.html')


@pages.route('/cameras')
@require_auth
def cameras_page():
    """Cameras management page."""
    return render_template('cameras.html')


@pages.route('/exports')
@require_auth
def exports_page():
    """Exports page."""
    return render_template('exports.html')


@pages.route('/settings')
@require_auth
def settings_page():
    """Settings page."""
    return render_template('settings.html')


# ============== App Factory ==============

def create_app(
    config_manager: ConfigManager,
    capture_manager: CaptureManager,
    schedule_manager: ScheduleManager,
    exporter: Exporter
) -> Flask:
    """Create and configure the Flask application."""
    global _config_manager, _capture_manager, _schedule_manager, _exporter

    _config_manager = config_manager
    _capture_manager = capture_manager
    _schedule_manager = schedule_manager
    _exporter = exporter

    app = Flask(
        __name__,
        template_folder=Path(__file__).parent / 'templates',
        static_folder=Path(__file__).parent / 'static'
    )

    # Persist the secret key so restarts don't invalidate sessions
    secret_key_file = config_manager.config_dir / '.secret_key'
    if secret_key_file.exists():
        secret_key = secret_key_file.read_bytes()
    else:
        secret_key = os.urandom(24)
        secret_key_file.write_bytes(secret_key)
    app.secret_key = os.environ.get('SECRET_KEY', secret_key)

    app.register_blueprint(api)
    app.register_blueprint(pages)

    # Inject configured timezone into every template so JS can read it
    # synchronously from a <meta> tag without an async API call.
    @app.context_processor
    def inject_timezone():
        loc = _config_manager.app_config.location if _config_manager else None
        return {'timezone': loc.timezone if loc and loc.timezone else ''}

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not found'}), 404
        return render_template('base.html', error='Page not found'), 404

    @app.errorhandler(500)
    def server_error(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal server error'}), 500
        return render_template('base.html', error='Internal server error'), 500

    return app
