#!/usr/bin/env python3
import rospy
from pathlib import Path
from ruamel.yaml import YAML
import threading

# Initialize a YAML instance that preserves comments and formatting
yaml = YAML()

class ContextManager:
    """
    Manages the loading, execution logic, and persistence of context snippets.
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
        self.state_path = self.workspace_path / "state"
        self.config = config
        self._lock = threading.Lock()

        # Get header/footer names from config
        self.header_name = self.config['header_name']
        self.footer_name = self.config['footer_name']
        self.header_config_path = self.state_path / f"{self.header_name}_config.yaml"
        self.footer_config_path = self.state_path / f"{self.footer_name}_config.yaml"

        # In-memory representation of snippets and their cached outputs
        self.header_snippets = []
        self.footer_snippets = []
        self.cached_output = {}

        self._load_configs()
        rospy.loginfo("ContextManager: Initialization complete.")

    def _load_configs(self):
        """Loads the header and footer YAML configuration files into memory."""
        with self._lock:
            try:
                if self.header_config_path.exists():
                    with open(self.header_config_path, 'r') as f:
                        self.header_snippets = yaml.load(f) or []
                else:
                    rospy.logwarn(f"ContextManager: Header config not found at {self.header_config_path}")

                if self.footer_config_path.exists():
                    with open(self.footer_config_path, 'r') as f:
                        self.footer_snippets = yaml.load(f) or []
                else:
                    rospy.logwarn(f"ContextManager: Footer config not found at {self.footer_config_path}")
            except Exception as e:
                rospy.logerr(f"ContextManager: Error loading snippet configurations: {e}")

    def get_snippets_to_execute(self):
        """
        Determines which snippets need to be run in the current cycle.

        Returns:
            A tuple containing (list of header snippets to run, list of footer snippets to run).
        """
        with self._lock:
            header_to_run = [s for s in self.header_snippets if self._should_run(s)]
            footer_to_run = [s for s in self.footer_snippets if self._should_run(s)]
        return header_to_run, footer_to_run

    def _should_run(self, snippet: dict):
        """
        Logic to decide if a snippet should be executed based on its TTL and cache status.
        - ttl > 0: Dynamic, runs every cycle.
        - ttl < 0: Cached, runs only if not already in cache.
        - ttl == 0: Disabled.
        - ttl == +/-99: Pinned, always runs (dynamic) or runs once and stays forever (cached).
        """
        ttl = snippet.get('ttl', 0)
        name = snippet.get('name', 'unnamed_snippet')

        if ttl == 0:
            return False
        if ttl > 0: # Dynamic snippet
            return True
        if ttl < 0: # Cached snippet
            return name not in self.cached_output

        return False

    def update_and_save_configs(self):
        """
        Updates the TTLs of all snippets and writes the configurations back to their files.
        This should be called once per cognition cycle.
        """
        with self._lock:
            configs_changed = False
            
            # Process header snippets
            updated_header_snippets = []
            for s in self.header_snippets:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True
                
                # Removal logic
                if s['ttl'] == 0 and self.config.get('remove_header_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL header snippet '{s['name']}'.")
                    configs_changed = True
                    continue # Skip appending it to the updated list
                updated_header_snippets.append(s)
            self.header_snippets = updated_header_snippets

            # Process footer snippets
            updated_footer_snippets = []
            for s in self.footer_snippets:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True

                # Removal logic
                if s['ttl'] == 0 and self.config.get('remove_footer_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL footer snippet '{s['name']}'.")
                    configs_changed = True
                    continue
                updated_footer_snippets.append(s)
            self.footer_snippets = updated_footer_snippets

            # If any TTLs were changed or snippets removed, write back to the file
            if configs_changed:
                rospy.loginfo("ContextManager: Snippet TTLs changed, saving configs to disk.")
                try:
                    with open(self.header_config_path, 'w') as f:
                        yaml.dump(self.header_snippets, f)
                    with open(self.footer_config_path, 'w') as f:
                        yaml.dump(self.footer_snippets, f)
                except Exception as e:
                    rospy.logerr(f"ContextManager: Failed to save updated snippet configs: {e}")

    def _get_updated_ttl(self, snippet: dict):
        """Calculates the new TTL for a snippet."""
        ttl = snippet.get('ttl', 0)
        original_ttl = ttl
        
        # Pinned snippets don't have their TTLs changed
        if ttl in [99, -99, 0]:
            return ttl, False

        if ttl > 0:
            ttl -= 1
        elif ttl < 0:
            ttl += 1
            
        return ttl, ttl != original_ttl