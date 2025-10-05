#!/usr/bin/env python3
import rospy
import sys
import os
import re
import io
import code
import json
import time
import threading
import traceback
import linecache
from pathlib import Path
from enum import Enum 
from logos_framework.msg import CognitionInput, CognitionOutput



# python worker TODOs:
# Create a custom exception in the API for timeout and Interrupt
# wrap <py> code in try...except
# implement linecache "stuffing" for better tracebacks


class WorkerState(Enum):
    IDLE = 0
    EXECUTING = 1

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
        # Async Output & State Management
        self.state = WorkerState.IDLE
        self.state_lock = threading.RLock() # RLock for safety

        # Permanent Standard I/O Redirection
        # We replace sys.stdout/err so we capture output from ANY thread
        self.stdout_buffer = io.StringIO()
        self.stderr_buffer = io.StringIO()
        sys.stdout = self.stdout_buffer
        sys.stderr = self.stderr_buffer
        rospy.loginfo("Python Worker: Standard I/O permanently redirected to internal buffers.")

        # Interpreter state
        self.interpreter_lock = threading.Lock() # Protects the interpreter instance itself
        self.interpreter = None
        self._initialize_interpreter()

        # Publishers and Subscribers
        self.input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.output_sub = rospy.Subscriber('/cognition/output', CognitionOutput, self._output_callback, queue_size=10)

        # Async Output Polling Timer (e.g., every 0.5 seconds)
        self.async_timer = rospy.Timer(rospy.Duration(0.5), self._poll_async_output)

        rospy.loginfo("Python Worker Node: Ready for execution requests.")

    def _poll_async_output(self, event=None):
            """Timer callback to check for and publish background output."""
            with self.state_lock:
                # Only capture and publish if we are IDLE.
                # If EXECUTING, the _execute_code method owns the buffers.
                if self.state == WorkerState.IDLE:
                    stdout = self.stdout_buffer.getvalue()
                    stderr = self.stderr_buffer.getvalue()

                    if stdout or stderr:
                        # Clear buffers now that we've read them
                        self.stdout_buffer.truncate(0); self.stdout_buffer.seek(0)
                        self.stderr_buffer.truncate(0); self.stderr_buffer.seek(0)

                        content = ""
                        if stdout: content += f"# async stdout\n{stdout.strip()}\n"
                        if stderr: content += f"# async stderr\n{stderr.strip()}\n"
                        
                        # Publish as py_async. Usually doesn't trigger cognition loop.
                        self._publish_result(msg_type='py_async', content=content.strip(), loop_cognition=False, filename="async_output")


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
            # MODIFIED: Pass the filename from the message
            args=(code_to_run, do_reset, msg.type, msg.filename)
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
            self._publish_result(msg_type=msg.type, content=result_content, loop_cognition=True, filename=msg.filename)


    def _execute_code(self, code_str: str, do_reset: bool, request_type: str, filename: str):
            """The core execution logic with state management and buffer capture."""
            start_time = time.time()
            
            # 1. Set State to EXECUTING
            with self.state_lock:
                self.state = WorkerState.EXECUTING
                # Note: We do NOT clear buffers here. Temporal routing means anything
                # in the buffer now belongs to this execution cycle.

            # 2. Run Code (holding interpreter lock, but NOT state lock)
            with self.interpreter_lock:
                if do_reset:
                    self._initialize_interpreter()
                
                # Use passed filename or default
                code_filename = filename if filename else f"<{request_type}>"

                # NEW: Stuff the code into the linecache module.
                # This allows tracebacks to show the correct source code lines.
                linecache.cache[code_filename] = (len(code_str), None, [line + '\n' for line in code_str.splitlines()], code_filename)

                try:
                    # Output is automatically captured by sys.stdout/err redirection setup in __init__
                    compiled_code = compile(code_str, code_filename, 'exec')
                    self.interpreter.runcode(compiled_code)
                except Exception:
                    # Print traceback to our redirected stderr
                    traceback.print_exc(file=sys.stderr)

                # Check/Reset magic variables
                loop_cognition = self.interpreter.locals.get('loop_cognition', False)
                if not isinstance(loop_cognition, bool):
                    rospy.logwarn(f"Magic variable 'loop_cognition' non-boolean: {loop_cognition}. Defaulting False.")
                    loop_cognition = False 
                if 'loop_cognition' in self.interpreter.locals:
                    del self.interpreter.locals['loop_cognition']

            # 3. Set State to IDLE and Harvest Buffers
            with self.state_lock:
                self.state = WorkerState.IDLE
                # Capture everything generated during execution
                stdout = self.stdout_buffer.getvalue(); self.stdout_buffer.truncate(0); self.stdout_buffer.seek(0)
                stderr = self.stderr_buffer.getvalue(); self.stderr_buffer.truncate(0); self.stderr_buffer.seek(0)

            duration = time.time() - start_time
            rospy.loginfo(f"Execution finished in {duration:.2f}s. Request: {request_type}. Loop: {loop_cognition}")

            # 4. Format Results (Your refined logic, slightly tidied)
            stdout_str = stdout.strip()
            stderr_str = stderr.strip()
            result_parts = []

            if request_type == 'context':
                # Clean formatting for context
                if stdout_str: result_parts.append(stdout_str)
                if stderr_str: result_parts.append(stderr_str)
                # Context shouldn't have "no output" or timing info appended.
            else:
                # Verbose formatting for LLM/other
                if stdout_str: result_parts.append(f"# stdout\n{stdout_str}")
                if stderr_str: result_parts.append(f"# stderr\n{stderr_str}")
                
                if not result_parts:
                    result_parts.append("# No output produced.")
                
                result_parts.append(f"\n# Execution finished in {duration:.2f}s.")

            result_content = "\n".join(result_parts).strip()

            self._publish_result(msg_type=request_type, content=result_content, loop_cognition=loop_cognition, filename=filename)

    def _publish_result(self, msg_type: str, content: str, loop_cognition: bool, filename: str): # MODIFIED
        """Constructs and publishes the result message to the CognitionNode."""
        response_msg = CognitionInput()
        
        # Determine the response type based on the request type
        if msg_type == 'llm':
            response_msg.type = 'py_result'
        else: # 'context', 'state', etc.
            response_msg.type = msg_type

        response_msg.content = content
        response_msg.loop_cognition = loop_cognition
        response_msg.filename = filename # NEW

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