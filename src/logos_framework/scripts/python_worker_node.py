#!/usr/bin/env python3
import rospy
import os
import re
import io
import code
import json
import time
import threading
import traceback
import contextlib
from pathlib import Path

from logos_framework.msg import CognitionInput, CognitionOutput


# python worker TODOs:
# Add a rospy.Timer to poll for and capture async output from background tasks running in the interpreter.
# Create a custom exception in the API for timeout and Interrupt
# wrap <py> code in try...except
# filename is currently request_type_snippet, use msg-id or snippet_name for context collection. tweak CognitionOutput.msg
# implement linecache "stuffing" for better tracebacks


class PythonWorkerNode:
    def __init__(self):
        rospy.init_node('python_worker_node')

        # Get the workspace path from a ROS parameter
        workspace_param = rospy.get_param('~workspace_path')
        if not workspace_param:
            rospy.logfatal("Required parameter '~workspace_path' is not set! Shutting down.")
            rospy.signal_shutdown("Missing workspace_path parameter")
            return

        self.workspace_path = Path(workspace_param).expanduser()
        rospy.loginfo(f"Python Worker Node: Initializing with workspace: {self.workspace_path}")

        # CRITICAL: Set the current working directory for the LLM's code
        try:
            os.chdir(self.workspace_path)
            rospy.loginfo(f"Set CWD to: {os.getcwd()}")
        except FileNotFoundError:
            rospy.logerr(f"Workspace path {self.workspace_path} does not exist! Shutting down.")
            rospy.signal_shutdown("Workspace path not found")
            return

        # Load framework config to get python settings
        self.config = {}
        try:
            config_path = self.workspace_path / ".system" / "framework_config.json"
            with open(config_path, 'r') as f:
                self.config = json.load(f)
        except Exception as e:
            rospy.logfatal(f"Failed to load framework_config.json: {e}. Shutting down.")
            rospy.signal_shutdown("Config load failure")
            return

        # Interpreter state
        self.interpreter_lock = threading.Lock()
        self.interpreter = None
        self._initialize_interpreter()

        # Publishers and Subscribers
        self.input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.output_sub = rospy.Subscriber('/cognition/output', CognitionOutput, self._output_callback, queue_size=10)

        # TODO: Add a rospy.Timer here to poll for and capture async output
        # from background tasks running in the interpreter.

        rospy.loginfo("Python Worker Node: Ready for execution requests.")

    def _initialize_interpreter(self):
        """Creates a new interpreter instance and loads preload APIs."""
        rospy.loginfo("Initializing new Python interpreter instance...")
        self.interpreter = code.InteractiveInterpreter()
        self._load_preload_apis()

    def _load_preload_apis(self):
        """Executes files specified in the config to populate the interpreter's namespace."""
        preload_path = self.workspace_path / "preload_api"
        preload_files = self.config.get("python", {}).get("preload_api_files", [])

        if not preload_files:
            rospy.loginfo("No preload API files specified.")
            return

        for filename in preload_files:
            file_path = preload_path / filename
            if file_path.exists():
                rospy.loginfo(f"Loading preload API: {filename}")
                try:
                    with open(file_path, 'r') as f:
                        api_code = f.read()
                    self.interpreter.runcode(compile(api_code, filename, 'exec'))
                except Exception as e:
                    rospy.logerr(f"Error loading preload API {filename}: {e}\n{traceback.format_exc()}")
            else:
                rospy.logwarn(f"Preload API file not found: {file_path}")

    def _output_callback(self, msg: CognitionOutput):
        """Handles incoming requests for code execution from the Cognition Node."""
        if msg.type in ['thoughts', 'chunk', 'state']:
            return

        # Regex to find <py> blocks and capture attributes and code
        py_block_pattern = re.compile(r'<py\s*(reset="(?P<reset>true|false)")?\s*(timeout="(?P<timeout>\d+)")?.*?>(?P<code>.*)</py>', re.DOTALL)
        match = py_block_pattern.search(msg.content)

        if not match:
            rospy.logdebug(f"No <py> block found in message of type '{msg.type}'. Ignoring.")
            return

        code_to_run = match.group('code').strip()
        do_reset = match.group('reset') == 'true'
        
        try:
            timeout_str = match.group('timeout')
            timeout_sec = float(timeout_str) if timeout_str else self.config.get("python", {}).get("default_timeout", 300)
        except (ValueError, TypeError):
            timeout_sec = self.config.get("python", {}).get("default_timeout", 300)
            rospy.logwarn(f"Invalid timeout value '{match.group('timeout')}', using default {timeout_sec}s.")

        rospy.loginfo(f"Executing code from '{msg.type}' message with timeout={timeout_sec}s, reset={do_reset}")
        
        # Execute in a separate thread to handle timeouts
        execution_thread = threading.Thread(
            target=self._execute_code,
            args=(code_to_run, do_reset, msg.type)
        )
        execution_thread.start()
        execution_thread.join(timeout=timeout_sec)

        if execution_thread.is_alive():
            rospy.logerr(f"Code execution timed out after {timeout_sec} seconds!")
            # Note: We can't forcefully kill the thread, but we can stop waiting
            # and report the timeout. The thread will eventually finish or block.
            # This is a known limitation of Python's threading.
            # TODO: Use Custom Exception in API to trigger "Interrupt"
            result_content = f"# stderr\nExecution timed out after {timeout_sec} seconds."
            self._publish_result(msg_type=msg.type, content=result_content, loop_cognition=True)


    def _execute_code(self, code_str: str, do_reset: bool, request_type: str):
        """The core execution logic."""
        start_time = time.time()
        
        with self.interpreter_lock:
            if do_reset:
                self._initialize_interpreter()

            output_buffer = io.StringIO()
            error_buffer = io.StringIO()
            
            try:
                with contextlib.redirect_stdout(output_buffer), contextlib.redirect_stderr(error_buffer):
                    # Compile and run the code in the persistent interpreter
                    compiled_code = compile(code_str, f"<{request_type}_snippet>", 'exec')
                    self.interpreter.runcode(compiled_code)
                
            except Exception:
                # Catch any exception and write the traceback to the error buffer
                traceback.print_exc(file=error_buffer)

            stdout = output_buffer.getvalue()
            stderr = error_buffer.getvalue()

            # Check for the magic variable to control the cognition loop
            loop_cognition = self.interpreter.locals.get('loop_cognition', False)
            if not isinstance(loop_cognition, bool):
                rospy.logwarn(f"Magic variable 'loop_cognition' was set to a non-boolean value: {loop_cognition}. Defaulting to False.")
                loop_cognition = True
            
            # Reset the magic variable after reading it
            if 'loop_cognition' in self.interpreter.locals:
                del self.interpreter.locals['loop_cognition']

        duration = time.time() - start_time
        rospy.loginfo(f"Code execution finished in {duration:.2f}s. Loop cognition requested: {loop_cognition}")

        # Format the result content
        result_content = ""
        if stdout:
            result_content += f"# stdout\n{stdout.strip()}\n"
        if stderr:
            result_content += f"# stderr\n{stderr.strip()}\n"
        
        result_content += f"\n# Execution finished in {duration:.2f}s."

        if not stdout and not stderr:
            result_content = f"# No output produced.\n{result_content.strip()}"

        self._publish_result(msg_type=request_type, content=result_content.strip(), loop_cognition=loop_cognition)

    def _publish_result(self, msg_type: str, content: str, loop_cognition: bool):
        """Constructs and publishes the result message to the CognitionNode."""
        response_msg = CognitionInput()
        
        # Determine the response type based on the request type
        if msg_type == 'llm':
            response_msg.type = 'py_result'
        else: # 'context', 'state', etc.
            response_msg.type = msg_type

        response_msg.content = content
        response_msg.loop_cognition = loop_cognition

        # TODO: if error_buffer not empty, set system_hint to something like "<!-- <system>: Avoid troubleshooting loops more than 3 turns. Try a different approach or ask for help. -->"
        # or track more state and count errors, offer unique hints based on counter

        response_msg.system_hint = ""

        self.input_pub.publish(response_msg)
        rospy.logdebug(f"Published result of type '{response_msg.type}'")

    def run(self):
        rospy.spin()

if __name__ == '__main__':
    try:
        node = PythonWorkerNode()
        node.run()
    except rospy.ROSInterruptException:
        pass
