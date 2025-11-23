# file: ~/robot_ws/src/logos_framework/scripts/cognition_lib/config_manager.py

import rospy
import json
from pathlib import Path
from ruamel.yaml import YAML

yaml = YAML()

class ConfigManager:
    """Handles loading and accessing all agent configuration files."""
    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path
        self.framework = {}
        self.system_prompt = ""
        self.my_config = {}
        
    def load_configs(self):
        rospy.loginfo("ConfigManager: Loading configurations...")
        try:
            framework_path = self.workspace_path / ".system" / "framework_config.json"
            prompt_path = self.workspace_path / ".system" / "system_prompt.txt"
            with open(framework_path, 'r') as f: self.framework = json.load(f)
            with open(prompt_path, 'r') as f: self.system_prompt = f.read()

            my_config_path = self.workspace_path / "state" / "my_config.yaml"
            if my_config_path.exists():
                with open(my_config_path, 'r') as f: self.my_config = yaml.load(f)
            else:
                rospy.logwarn(f"ConfigManager: my_config.yaml not found at {my_config_path}. Using default values.")
                self.my_config = {} # Ensure it's a dict
        
            # Perform templating on the system prompt
            workspace_name = self.workspace_path.name
            agent_settings = self.framework.get('agent_settings', {})
            context_settings = self.framework.get('context', {})

            self.system_prompt = self.system_prompt.replace('{{header_name}}', context_settings.get('header_name', ''))
            self.system_prompt = self.system_prompt.replace('{{footer_name}}', context_settings.get('footer_name', ''))
            self.system_prompt = self.system_prompt.replace('{{workspace_name}}', workspace_name)
            self.system_prompt = self.system_prompt.replace('{{global_max_io_buffer_media}}', str(agent_settings.get('global_max_io_buffer_media', 'N/A')))
            
            rospy.loginfo("ConfigManager: System prompt templating complete.")

            rospy.loginfo("ConfigManager: All configurations loaded successfully.")
            return True

        except Exception as e:
            rospy.logerr(f"ConfigManager: Error loading configurations: {e}")
            return False