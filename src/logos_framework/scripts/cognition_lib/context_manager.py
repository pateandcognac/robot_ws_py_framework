# file: ~/robot_ws/src/logos_framework/scripts/cognition_lib/context_manager.py

import rospy
import threading
from pathlib import Path
from ruamel.yaml import YAML

yaml = YAML()

class ContextManager:
    """
    Manages the loading, execution logic, and persistence of Cognitive Hooks.
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

        # Get header/footer names from config
        self.header_name = self.config['header_name']
        self.footer_name = self.config['footer_name']
        self.header_config_path = self.config_path / f"{self.header_name}_config.yaml"
        self.footer_config_path = self.config_path / f"{self.footer_name}_config.yaml"

        # In-memory representation of hooks and their cached outputs
        self.header_hooks = []
        self.footer_hooks = []
        self.cached_output = {}

        self._load_configs() # Initial load
        rospy.loginfo("ContextManager: Initialization complete.")

    def _load_configs(self):
        """Loads the header and footer YAML configuration files into memory."""
        with self._lock:
            try:
                if self.header_config_path.exists():
                    with open(self.header_config_path, 'r') as f:
                        self.header_hooks = yaml.load(f) or []
                else:
                    self.header_hooks = []
                    rospy.logwarn(f"ContextManager: Header config not found at {self.header_config_path}")

                if self.footer_config_path.exists():
                    with open(self.footer_config_path, 'r') as f:
                        self.footer_hooks = yaml.load(f) or []
                else:
                    self.footer_hooks = []
                    rospy.logwarn(f"ContextManager: Footer config not found at {self.footer_config_path}")
            except Exception as e:
                rospy.logerr(f"ContextManager: Error loading hook configurations: {e}")

    def get_hooks_to_execute(self):
        """
        Determines which hooks need to be run in the current cycle.

        Returns:
            A tuple containing (list of header hooks to run, list of footer hooks to run).
        """
        with self._lock:
            self._load_configs()
            header_to_run = [s for s in self.header_hooks if self._should_run(s)]
            footer_to_run = [s for s in self.footer_hooks if self._should_run(s)]
        return header_to_run, footer_to_run

    def _should_run(self, hook: dict):
        """
        Logic to decide if a hook should be executed based on its TTL and cache status.
        - ttl > 0: Dynamic, runs every cycle.
        - ttl < 0: Cached, runs only if not already in cache.
        - ttl == 0: Disabled.
        - ttl == +/-99: Pinned, always runs (dynamic) or runs once and stays forever (cached).
        """
        ttl = hook.get('ttl', 0)
        name = hook.get('name', 'unnamed_hook')

        if ttl == 0:
            return False
        if ttl > 0: # Dynamic hook
            return True
        if ttl < 0: # Cached hook
            return name not in self.cached_output

        return False

    def update_and_save_configs(self):
        """
        Updates the TTLs of all hooks and writes the configurations back to their files.
        This should be called once per cognition cycle.
        """
        with self._lock:
            configs_changed = False
            
            # Process header hooks
            updated_header_hooks = []
            for s in self.header_hooks:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True
                
                if new_ttl == 0 and s.get('name') in self.cached_output:
                    rospy.loginfo(f"ContextManager: Invalidating cache for EOL hook '{s['name']}'.")
                    del self.cached_output[s['name']]
                    configs_changed = True


                if s['ttl'] == 0 and self.config.get('remove_header_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL header hook '{s['name']}'.")
                    configs_changed = True
                    continue
                updated_header_hooks.append(s)
            self.header_hooks = updated_header_hooks

            # Process footer hooks
            updated_footer_hooks = []
            for s in self.footer_hooks:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True

                if s['ttl'] == 0 and self.config.get('remove_footer_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL footer hook '{s['name']}'.")
                    configs_changed = True
                    continue
                updated_footer_hooks.append(s)
            self.footer_hooks = updated_footer_hooks

            if configs_changed:
                rospy.loginfo("ContextManager: Snippet TTLs changed, saving configs to disk.")
                try:
                    with open(self.header_config_path, 'w') as f:
                        yaml.dump(self.header_hooks, f)
                    with open(self.footer_config_path, 'w') as f:
                        yaml.dump(self.footer_hooks, f)
                except Exception as e:
                    rospy.logerr(f"ContextManager: Failed to save updated hook configs: {e}")

    def _get_updated_ttl(self, hook: dict):
        """Calculates the new TTL for a hook."""
        ttl = hook.get('ttl', 0)
        original_ttl = ttl
        
        if ttl in [99, -99, 0]:
            return ttl, False

        if ttl > 0:
            ttl -= 1
        elif ttl < 0:
            ttl += 1
            
        return ttl, ttl != original_ttl