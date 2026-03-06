# file: ~/robot_ws/src/logos_framework/scripts/cognition_lib/context_manager.py

import rospy
import threading
from pathlib import Path
from ruamel.yaml import YAML

yaml = YAML()


class ContextManager:
    """
    Manages the loading and execution logic of Cognitive Hooks.
    """

    def __init__(self, workspace_path: Path, config: dict):
        """
        Initializes the ContextManager.

        Args:
            workspace_path: The root path of the agent's workspace.
            config: The 'context' section of the framework_config.json.
        """
        rospy.loginfo("ContextManager: Initializing...")
        self.workspace_path = workspace_path
        self.config_path = self.workspace_path / "config"
        self.config = config
        self._lock = threading.RLock()
        self._config_mtimes = {}

        # Get header/footer names from config
        self.header_name = self.config['header_name']
        self.footer_name = self.config['footer_name']
        self.header_config_path = self.config_path / f"{self.header_name}_config.yaml"
        self.footer_config_path = self.config_path / f"{self.footer_name}_config.yaml"

        # In-memory representation of hooks
        self.header_hooks = []
        self.footer_hooks = []

        self._load_configs(force=True)
        rospy.loginfo("ContextManager: Initialization complete.")

    def _get_config_mtime(self, path: Path):
        """Returns the current file mtime in nanoseconds, or None if unavailable."""
        try:
            return path.stat().st_mtime_ns if path.exists() else None
        except OSError as e:
            rospy.logwarn(f"ContextManager: Failed to stat config {path}: {e}")
            return None

    def _read_config_file(self, path: Path, current_hooks: list, label: str, force: bool = False):
        """Reads a config file only when it has changed on disk."""
        current_mtime = self._get_config_mtime(path)
        previous_mtime = self._config_mtimes.get(path)

        if not force and current_mtime == previous_mtime:
            return current_hooks, False

        self._config_mtimes[path] = current_mtime
        if not path.exists():
            rospy.logwarn(f"ContextManager: {label} config not found at {path}")
            return [], current_hooks != []

        try:
            with open(path, 'r') as f:
                loaded_hooks = yaml.load(f) or []
        except Exception as e:
            rospy.logerr(f"ContextManager: Error loading {label.lower()} hook config {path}: {e}")
            return current_hooks, False

        if not isinstance(loaded_hooks, list):
            rospy.logerr(f"ContextManager: {label} config at {path} must contain a list of hooks.")
            return [], current_hooks != []

        rospy.loginfo(f"ContextManager: Reloaded {label.lower()} hook config from {path}")
        return loaded_hooks, True

    def _load_configs(self, force: bool = False):
        """Loads the header and footer YAML configuration files into memory when they change."""
        with self._lock:
            header_hooks, header_changed = self._read_config_file(
                self.header_config_path,
                self.header_hooks,
                "Header",
                force=force,
            )
            footer_hooks, footer_changed = self._read_config_file(
                self.footer_config_path,
                self.footer_hooks,
                "Footer",
                force=force,
            )

            if header_changed:
                self.header_hooks = header_hooks
            if footer_changed:
                self.footer_hooks = footer_hooks

    def get_hooks_to_execute(self):
        """
        Determines which hooks should be run in the current cycle.

        Returns:
            A tuple containing (list of header hooks to run, list of footer hooks to run).
        """
        with self._lock:
            self._load_configs(force=False)
            header_to_run = [hook.copy() for hook in self.header_hooks if self._is_active(hook)]
            footer_to_run = [hook.copy() for hook in self.footer_hooks if self._is_active(hook)]
        return header_to_run, footer_to_run

    def _is_active(self, hook: dict):
        """Returns True when a hook config is explicitly marked active."""
        active = hook.get('active', False)
        if isinstance(active, bool):
            return active
        if active is not None:
            rospy.logwarn(
                f"ContextManager: Hook '{hook.get('name', 'unnamed_hook')}' has non-boolean "
                f"'active' value {active!r}; treating as inactive."
            )
        return False
