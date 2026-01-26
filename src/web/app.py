"""Flask web application for RTSP Timelapse Generator."""

import functools
import logging
import os
import uuid
from datetime import datetime
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
from ..exporter import Exporter
from ..models import (
    CameraConfig,
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

            if auth.username != expected_user or auth.password != expected_pass:
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
                last_capture_time = capture_time.isoformat()
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
            'schedules': [
                {
                    'name': s.name,
                    'frequency': s.frequency.value,
                    'enabled': s.enabled,
                    'value': s.value,
                    'time_window': {
                        'start': s.time_window.start.strftime('%H:%M') if s.time_window else None,
                        'end': s.time_window.end.strftime('%H:%M') if s.time_window else None,
                    } if s.time_window else None
                }
                for s in camera.schedules
            ]
        })

    return jsonify(cameras)


@api.route('/cameras', methods=['POST'])
@require_auth
def add_camera():
    """Add a new camera."""
    data = request.json

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Camera name is required'}), 400

    if name in _config_manager.cameras:
        return jsonify({'error': 'Camera already exists'}), 400

    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Camera URL is required'}), 400

    schedules = []
    for sched_data in data.get('schedules', []):
        tw = None
        if sched_data.get('time_window'):
            tw = TimeWindow(
                start=datetime.strptime(sched_data['time_window']['start'], '%H:%M').time(),
                end=datetime.strptime(sched_data['time_window']['end'], '%H:%M').time()
            )

        schedules.append(Schedule(
            name=sched_data.get('name', 'default'),
            frequency=FrequencyType(sched_data.get('frequency', 'hourly')),
            enabled=sched_data.get('enabled', True),
            value=sched_data.get('value', 1),
            time_window=tw
        ))

    camera = CameraConfig(
        name=name,
        url=url,
        enabled=data.get('enabled', True),
        schedules=schedules,
        capture_settings=CaptureSettings(
            jpeg_quality=data.get('jpeg_quality', 90),
            timeout_seconds=data.get('timeout_seconds', 10),
            retry_count=data.get('retry_count', 3)
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
                    end=datetime.strptime(sched_data['time_window']['end'], '%H:%M').time()
                )

            schedules.append(Schedule(
                name=sched_data.get('name', 'default'),
                frequency=FrequencyType(sched_data.get('frequency', 'hourly')),
                enabled=sched_data.get('enabled', True),
                value=sched_data.get('value', 1),
                time_window=tw
            ))
        camera.schedules = schedules

    if 'capture_settings' in data:
        cs = data['capture_settings']
        camera.capture_settings.jpeg_quality = cs.get('jpeg_quality', camera.capture_settings.jpeg_quality)
        camera.capture_settings.timeout_seconds = cs.get('timeout_seconds', camera.capture_settings.timeout_seconds)
        camera.capture_settings.retry_count = cs.get('retry_count', camera.capture_settings.retry_count)

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
            schedules.append({
                'camera': camera_name,
                'name': schedule.name,
                'frequency': schedule.frequency.value,
                'enabled': schedule.enabled,
                'value': schedule.value,
                'time_window': {
                    'start': schedule.time_window.start.strftime('%H:%M') if schedule.time_window else None,
                    'end': schedule.time_window.end.strftime('%H:%M') if schedule.time_window else None,
                } if schedule.time_window else None
            })

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
                    'timestamp': capture_time.isoformat(),
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

    if not str(full_path.resolve()).startswith(str(_capture_manager.captures_path.resolve())):
        abort(403)

    if not full_path.exists():
        abort(404)

    return send_file(full_path, mimetype='image/jpeg')


@api.route('/exports', methods=['GET'])
@require_auth
def list_exports():
    """List export history."""
    return jsonify({
        'exports': _exporter.list_exports(),
        'history': [
            {
                'id': h.id,
                'camera': h.camera,
                'start_date': h.start_date,
                'end_date': h.end_date,
                'preset': h.preset,
                'output_file': h.output_file,
                'created_at': h.created_at,
                'image_count': h.image_count,
                'duration_seconds': h.duration_seconds,
                'file_size_bytes': h.file_size_bytes
            }
            for h in _config_manager.export_history
        ],
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

    preset = _config_manager.export_presets[preset_name]

    if data.get('fps'):
        preset.fps = data['fps']

    try:
        history = _exporter.generate_timelapse(
            camera=camera,
            start_date=start_date,
            end_date=end_date,
            preset=preset
        )

        _config_manager.export_history.append(history)
        _config_manager.save_exports_config()

        return jsonify({
            'success': True,
            'export': {
                'id': history.id,
                'output_file': history.output_file,
                'image_count': history.image_count,
                'duration_seconds': history.duration_seconds,
                'file_size_bytes': history.file_size_bytes
            }
        })

    except Exception as e:
        logger.error(f"Export failed: {e}")
        return jsonify({'error': str(e)}), 500


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

    info = _exporter.calculate_export_info(camera, start_date, end_date, fps)
    return jsonify(info)


@api.route('/exports/<filename>', methods=['GET'])
@require_auth
def download_export(filename):
    """Download an export file."""
    export_path = _exporter.exports_path / filename

    if not str(export_path.resolve()).startswith(str(_exporter.exports_path.resolve())):
        abort(403)

    if not export_path.exists():
        abort(404)

    return send_file(export_path, as_attachment=True)


@api.route('/exports/<filename>/stream', methods=['GET'])
@require_auth
def stream_export(filename):
    """Stream an export file for in-browser playback."""
    export_path = _exporter.exports_path / filename

    if not str(export_path.resolve()).startswith(str(_exporter.exports_path.resolve())):
        abort(403)

    if not export_path.exists():
        abort(404)

    return send_file(export_path, mimetype='video/mp4')


@api.route('/exports/<filename>', methods=['DELETE'])
@require_auth
def delete_export(filename):
    """Delete an export file."""
    if _exporter.delete_export(filename):
        return jsonify({'success': True})
    return jsonify({'error': 'Export not found'}), 404


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
        with open(logs_path, 'r') as f:
            lines = f.readlines()

        lines = lines[-limit:]
        return jsonify({'lines': [l.strip() for l in lines]})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


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

    app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

    app.register_blueprint(api)
    app.register_blueprint(pages)

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
