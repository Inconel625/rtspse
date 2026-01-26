"""Main entry point for RTSP Timelapse Generator."""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from logging.handlers import RotatingFileHandler

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .config import get_config, reload_config, ConfigManager
from .capture import CaptureManager
from .scheduler import ScheduleManager
from .exporter import Exporter


logger = logging.getLogger(__name__)

# Global instances
config_manager: ConfigManager = None
capture_manager: CaptureManager = None
schedule_manager: ScheduleManager = None
exporter: Exporter = None
config_observer: Observer = None
shutdown_event = threading.Event()


class ConfigFileHandler(FileSystemEventHandler):
    """Handles config file changes for hot-reload."""

    def __init__(self, callback):
        self.callback = callback
        self._debounce_timer = None

    def on_modified(self, event):
        if event.is_directory:
            return

        if event.src_path.endswith('.yaml'):
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(1.0, self._trigger_reload, [event.src_path])
            self._debounce_timer.start()

    def _trigger_reload(self, path):
        logger.info(f"Config file changed: {path}")
        self.callback()


def setup_logging(config: ConfigManager) -> None:
    """Configure logging."""
    logs_path = config.get_logs_path()
    logs_path.mkdir(parents=True, exist_ok=True)

    log_file = logs_path / "rtspse.log"
    log_level = getattr(logging, config.app_config.log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)

    max_bytes = config.app_config.storage.max_log_size_mb * 1024 * 1024
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=5
    )
    file_handler.setLevel(log_level)
    file_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    root_logger.addHandler(file_handler)

    logger.info(f"Logging configured: level={config.app_config.log_level}, file={log_file}")


def capture_callback(camera) -> None:
    """Callback for scheduled captures."""
    global capture_manager
    if capture_manager:
        capture_manager.capture_frame(camera)


def handle_config_reload() -> None:
    """Handle configuration reload."""
    global config_manager, schedule_manager

    try:
        old_cameras = dict(config_manager.cameras)

        reload_config()
        config_manager = get_config()

        new_cameras = config_manager.cameras
        for name in list(old_cameras.keys()):
            if name not in new_cameras:
                schedule_manager.remove_camera(name)
                logger.info(f"Removed camera: {name}")

        for name, camera in new_cameras.items():
            if name not in old_cameras:
                schedule_manager.update_camera(camera)
                logger.info(f"Added camera: {name}")
            elif camera != old_cameras[name]:
                schedule_manager.update_camera(camera)
                logger.info(f"Updated camera: {name}")

        logger.info("Configuration reloaded successfully")

    except Exception as e:
        logger.error(f"Failed to reload configuration: {e}")


def signal_handler(signum, frame) -> None:
    """Handle shutdown signals."""
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()


def main() -> int:
    """Main entry point."""
    global config_manager, capture_manager, schedule_manager, exporter, config_observer

    parser = argparse.ArgumentParser(description='RTSP Timelapse Generator')
    parser.add_argument(
        '--config-dir',
        type=str,
        default=None,
        help='Configuration directory path'
    )
    parser.add_argument(
        '--no-web',
        action='store_true',
        help='Disable web UI even if enabled in config'
    )
    args = parser.parse_args()

    try:
        if args.config_dir:
            from .config import ConfigManager as CM
            config_manager = CM(Path(args.config_dir))
            config_manager.load_all()
        else:
            config_manager = get_config()

        setup_logging(config_manager)

        logger.info("Starting RTSP Timelapse Generator")

        captures_path = config_manager.get_captures_path()
        exports_path = config_manager.get_exports_path()

        captures_path.mkdir(parents=True, exist_ok=True)
        exports_path.mkdir(parents=True, exist_ok=True)

        capture_manager = CaptureManager(captures_path)
        exporter = Exporter(captures_path, exports_path)
        schedule_manager = ScheduleManager()

        schedule_manager.set_capture_callback(capture_callback)
        schedule_manager.load_cameras(config_manager.cameras)
        schedule_manager.start()

        logger.info(f"Loaded {len(config_manager.cameras)} cameras")

        event_handler = ConfigFileHandler(handle_config_reload)
        config_observer = Observer()
        config_observer.schedule(event_handler, str(config_manager.config_dir), recursive=False)
        config_observer.start()
        logger.info("Config hot-reload enabled")

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGHUP, lambda s, f: handle_config_reload())

        web_enabled = config_manager.app_config.web_ui.enabled and not args.no_web

        if web_enabled:
            from .web.app import create_app

            app = create_app(
                config_manager=config_manager,
                capture_manager=capture_manager,
                schedule_manager=schedule_manager,
                exporter=exporter
            )

            host = config_manager.app_config.web_ui.host
            port = config_manager.app_config.web_ui.port

            logger.info(f"Starting web UI at http://{host}:{port}")

            def run_flask():
                app.run(
                    host=host,
                    port=port,
                    debug=False,
                    use_reloader=False,
                    threaded=True
                )

            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()

        logger.info("Application started. Press Ctrl+C to stop.")

        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")

    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1

    finally:
        logger.info("Shutting down...")

        if config_observer:
            config_observer.stop()
            config_observer.join()

        if schedule_manager:
            schedule_manager.stop()

        logger.info("Shutdown complete")

    return 0


if __name__ == "__main__":
    sys.exit(main())
