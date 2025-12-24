#!/usr/bin/python3.8
# file: ~/robot_ws/src/logos_framework/scripts/python_worker_node.py

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
from std_msgs.msg import String as StringMsg
from std_msgs.msg import Bool
from ruamel.yaml import YAML



class WorkerState(Enum):
    IDLE = 0 # not actively running a <py> code block
    EXECUTING = 1

class PythonWorkerNode:
    def __init__(self):
        rospy.init_node('python_worker_node')

        self.error_streak = 0

        # Get the workspace path from a ROS parameter
        workspace_param = rospy.get_param('~workspace_path')
        if not workspace_param:
            rospy.logfatal("Required parameter '~workspace_path' is not set! Shutting down.")
            rospy.signal_shutdown("Missing workspace_path parameter")
            return

        self.workspace_path = Path(workspace_param).expanduser()
        rospy.loginfo(f"Python Worker Node: Initializing with workspace: {self.workspace_path}")

        src_path = self.workspace_path / "src"
        if src_path.exists():
            sys.path.insert(0, str(src_path))
            rospy.loginfo(f"Added {src_path} to Python path for user-defined modules.")

        try:
            os.chdir(self.workspace_path)
            rospy.loginfo(f"Set CWD to: {os.getcwd()}")
        except FileNotFoundError:
            rospy.logerr(f"Workspace path {self.workspace_path} does not exist! Shutting down.")
            rospy.signal_shutdown("Workspace path not found")
            return

        
        try:
            from logos.exceptions import Interrupt as LogosInterrupt
            self.LogosInterrupt = LogosInterrupt # Store the class for later use
        except ImportError:
            rospy.logfatal("Could not import the 'logos' API package! Is it in the preload_api directory? Shutting down.")
            rospy.signal_shutdown("Logos API not found.")
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

        # `/python/is_executing` topic publisher, Boolean, latched
        self.is_executing_pub = rospy.Publisher('/python/is_executing', Bool, queue_size=1, latch=True)

        #  Interrupt state and subscriber
        self.interrupt_request = None
        self.interrupt_lock = threading.Lock()
        self.interrupt_sub = rospy.Subscriber('/python/interrupt', StringMsg, self._interrupt_callback)

        # IO Publishers and Subscribers
        self.input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.output_sub = rospy.Subscriber('/cognition/output', CognitionOutput, self._output_callback, queue_size=10)

        # Async Output Polling Timer (e.g., every 0.5 seconds)
        self.async_timer = rospy.Timer(rospy.Duration(0.5), self._poll_async_output)

        rospy.loginfo("Python Worker Node: Ready for execution requests.")

    def _update_state_from_config(self, state_obj, config_dict):
            """Recursively updates attributes of the state object from a loaded config dict."""
            for key, value in config_dict.items():
                if hasattr(state_obj, key):
                    current_attr = getattr(state_obj, key)
                    if isinstance(value, dict) and hasattr(current_attr, '__dict__'):
                        # It's a nested object (like files, system), recurse
                        self._update_state_from_config(current_attr, value)
                    else:
                        # It's a direct attribute, set it
                        setattr(state_obj, key, value)

    def _interrupt_callback(self, msg: StringMsg):
        rospy.loginfo(f"Interrupt message received: {msg.data}")
        with self.interrupt_lock:
            try:
                # The JSON spec is good, let's add a default for loop_cognition
                data = json.loads(msg.data)
                self.interrupt_request = {
                    "source": data.get("source", "unknown"),
                    "message": data.get("message", "No message provided."),
                    "loop_cognition": data.get("loop_cognition", True) # Default to True
                }
            except json.JSONDecodeError:
                rospy.logwarn("Received malformed JSON on /python/interrupt topic.")


    def _poll_async_output(self, event=None):
            """Timer callback to check for and publish background output."""
            with self.state_lock:
                # Only capture and publish if we are IDLE.
                # If EXECUTING, the _execute_code method owns the buffers.
                if self.state == WorkerState.IDLE:
                    stdout = self.stdout_buffer.getvalue()
                    stderr = self.stderr_buffer.getvalue()

                    if stdout or stderr:
                        loop_cognition = False
                        if self.interpreter is not None:
                            with self.interpreter_lock:
                                loop_cognition = self.interpreter.locals.get('loop_cognition', False)
                                if not isinstance(loop_cognition, bool):
                                    rospy.logwarn(f"Magic variable 'loop_cognition' non-boolean: {loop_cognition}. Defaulting False.")
                                    loop_cognition = False
                                if 'loop_cognition' in self.interpreter.locals:
                                    del self.interpreter.locals['loop_cognition']

                        # Clear buffers now that we've read them
                        self.stdout_buffer.truncate(0); self.stdout_buffer.seek(0)
                        self.stderr_buffer.truncate(0); self.stderr_buffer.seek(0)

                        content = ""
                        if stdout: content += f"# async stdout\n{stdout.strip()}\n"
                        if stderr: content += f"# async stderr\n{stderr.strip()}\n"
                        
                        # Publish as py_async. loop_cognition could be set True programatically.
                        self._publish_result(msg_type='py_async', content=content.strip(), loop_cognition=loop_cognition, filename="async_output")


    def _initialize_interpreter(self):
        """Creates a new interpreter instance and loads the API and config."""
        rospy.loginfo("Initializing new Python interpreter instance...")
        self.interpreter = code.InteractiveInterpreter()
        self._load_api_and_config()

    def _load_api_and_config(self):
        """Imports the core logos API and loads user config into the interpreter's state."""
        # 1. Explicitly import the main package into the interpreter's global scope.
        try:
            self.interpreter.runcode(compile("import logos", "<preload>", "exec"))
            rospy.loginfo("Successfully imported 'logos' package into the interpreter's main namespace.")
        except Exception as e:
            rospy.logerr(f"CRITICAL: Failed to import the 'logos' package. API will not be available: {e}\n{traceback.format_exc()}")
            # We might want to consider shutting down if this fails, as the agent is crippled.
            return # Continue for now, but it will likely fail.

        """"
        # 2. After API is loaded, load my_config.yaml to override default state.
        try:
            my_config_path = self.workspace_path / "state" / "my_config.yaml"
            if my_config_path.exists():
                rospy.loginfo("Loading my_config.yaml to initialize logos.state...")
                yaml = YAML()
                with open(my_config_path, 'r') as f:
                    my_config = yaml.load(f)

                if my_config and 'logos' in self.interpreter.locals:
                    logos_module = self.interpreter.locals['logos']
                    self._update_state_from_config(logos_module.state, my_config)
                    rospy.loginfo("Successfully updated logos.state from my_config.yaml.")
        except Exception as e:
            rospy.logerr(f"Error applying my_config.yaml to logos.state: {e}")
        """
                
    def _output_callback(self, msg: CognitionOutput):
        """Handles incoming requests for code execution from the Cognition Node."""
        if msg.type in ['thoughts', 'chunk', 'state']:
            return
        
        # First check for <chat>CONTENT</chat> blocks and remove
        # They might contain reference to <py> tags that we must ignore.
        chat_pattern = re.compile(r'<chat>.*?</chat>', re.DOTALL)
        msg.content = chat_pattern.sub('', msg.content)

        # Regex to find <py> blocks and capture attributes and code.
        py_block_pattern = re.compile(r'<py\s*(reset="(?P<reset>true|false)")?\s*(timeout="(?P<timeout>\d+)")?.*?>(?P<code>.*)</py>', re.DOTALL)
        match = py_block_pattern.search(msg.content.strip())

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
            args=(code_to_run, do_reset, msg.type, msg.filename)
        )
        execution_thread.start()
        execution_thread.join(timeout=timeout_sec)

        if execution_thread.is_alive():
            rospy.logerr(f"Code execution timed out after {timeout_sec} seconds! Requesting cooperative interrupt.")
            
            # If the thread is still alive, it means
            # it timed out. We now inject an interrupt request.
            with self.interrupt_lock:
                self.interrupt_request = {
                    "source": "system_timeout",
                    "message": f"Execution timed out after {timeout_sec} seconds.",
                    "loop_cognition": True  # A timeout is an error, so we should always re-evaluate.
                }

            # We must also immediately publish a result to unblock the CognitionNode.
            # The worker thread, upon its next call to check_for_interrupt(), will
            # raise an exception and terminate without sending a second result.
            result_content = f"# stderr\nExecution timed out after {timeout_sec} seconds. A cooperative interrupt has been requested."
            self._publish_result(msg_type='py_result', content=result_content, loop_cognition=True, filename=msg.filename)
            # We add a special attribute to the thread to signal it should not publish a result when it finally dies.
            execution_thread.was_terminated_by_timeout = True 


    def _execute_code(self, code_str: str, do_reset: bool, request_type: str, filename: str):
            """The core execution logic with state management, buffer capture, and interrupt handling."""
            start_time = time.time()
            
            # 1. Initialize variables for this execution run
            interrupted = False
            final_loop_cognition = False  # Default if not set elsewhere
            stdout_str = ""
            stderr_str = ""
            result_parts = []


            # 2. Set State to EXECUTING
            if request_type == 'me':
                self.is_executing_pub.publish(True)

            with self.state_lock:
                self.state = WorkerState.EXECUTING
                # Buffers are not cleared here; any async output that arrived just before
                # this execution is considered part of this execution's output.

            # 3. The main execution block. The `finally` ensures we always clean up.
            try:
                # 3a. Run Code (holding interpreter lock)
                with self.interpreter_lock:
                    if do_reset:
                        self._initialize_interpreter()
                    
                    # Inject the current interrupt request state into the interpreter's namespace.
                    # This makes it accessible to `logos.check_for_interrupt()`.
                    with self.interrupt_lock:
                        self.interpreter.locals['__logos_interrupt_request__'] = self.interrupt_request

                    code_filename = filename if filename else f"<{request_type}>"
                    linecache.cache[code_filename] = (len(code_str), None, [line + '\n' for line in code_str.splitlines()], code_filename)

                    compiled_code = compile(code_str, code_filename, 'exec')
                    self.interpreter.runcode(compiled_code)
                    self.error_streak = 0  # Reset error streak on success

                    # On normal completion, get the magic variable
                    loop_cognition = self.interpreter.locals.get('loop_cognition', False)
                    if not isinstance(loop_cognition, bool):
                        rospy.logwarn(f"Magic variable 'loop_cognition' non-boolean: {loop_cognition}. Defaulting False.")
                        loop_cognition = False
                    final_loop_cognition = loop_cognition

            # 3b. Handle our special, polite Interrupt exception
            except self.LogosInterrupt:
                interrupted = True
                # Immediately copy the details. It will be cleared in `finally`.
                request_details = self.interrupt_request.copy()
                
                # Get the traceback to find out WHERE the code was interrupted.
                exc_type, exc_value, exc_traceback = sys.exc_info()
                frame = traceback.extract_tb(exc_traceback)[-1]
                interrupt_location = f"at line {frame.lineno} in {os.path.basename(frame.filename)}"

                # Format the polite message and append it to any stdout that was already generated.
                interrupt_message = (
                    f"\n# Execution politely interrupted by '{request_details['source']}' ({interrupt_location}).\n"
                    f"# Message: {request_details['message']}"
                )
                stdout_str = self.stdout_buffer.getvalue().strip() + interrupt_message
                stderr_str = self.stderr_buffer.getvalue().strip() # Capture any stderr too
                
                # The interrupt message dictates the next cognitive step
                final_loop_cognition = request_details.get('loop_cognition', True)
                rospy.loginfo(f"Execution interrupted by '{request_details['source']}'.")

            # 3c. Handle all other "normal" code exceptions
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.error_streak += 1
                # Try to respect loop_cognition even if an error occurred before the end
                with self.interpreter_lock:
                    final_loop_cognition = self.interpreter.locals.get('loop_cognition', False)

            finally:
                # 3d. This block runs ALWAYS: on success, interrupt, or error.
                # If the thread was flagged as terminated by the main loop, we must not publish a second result.
                # We just clean up and exit quietly.
                if getattr(threading.current_thread(), 'was_terminated_by_timeout', False):
                    rospy.logwarn(f"Post-timeout thread ({threading.current_thread().name}) is now terminating.")
                    # We still need to reset the state and clear buffers.
                    with self.interpreter_lock:
                        if 'loop_cognition' in self.interpreter.locals: del self.interpreter.locals['loop_cognition']
                        if '__logos_interrupt_request__' in self.interpreter.locals: del self.interpreter.locals['__logos_interrupt_request__']
                    with self.interrupt_lock:
                        self.interrupt_request = None # Clear the flag
                    with self.state_lock:
                        self.state = WorkerState.IDLE
                        self.stdout_buffer.truncate(0); self.stdout_buffer.seek(0)
                        self.stderr_buffer.truncate(0); self.stderr_buffer.seek(0)
                    return # Exit the function early. This `return` is apparently "poor form" but it is what it is for now.

                with self.interpreter_lock:
                    if 'loop_cognition' in self.interpreter.locals:
                        del self.interpreter.locals['loop_cognition']
                    if '__logos_interrupt_request__' in self.interpreter.locals:
                        del self.interpreter.locals['__logos_interrupt_request__']

                # CRITICAL: Clear the global interrupt flag so the next run isn't affected.
                with self.interrupt_lock:
                    self.interrupt_request = None
                
                # 4. Set State to IDLE and Harvest Buffers
                self.is_executing_pub.publish(False)
                with self.state_lock:
                    self.state = WorkerState.IDLE
                    # If not interrupted, the strings are empty, so we capture the final buffer states.
                    # If interrupted, the strings were already captured at the moment of interruption.
                    if not interrupted:
                        stdout_str = self.stdout_buffer.getvalue()
                        stderr_str = self.stderr_buffer.getvalue()
                    
                    # Always clear the buffers for the next cycle.
                    self.stdout_buffer.truncate(0); self.stdout_buffer.seek(0)
                    self.stderr_buffer.truncate(0); self.stderr_buffer.seek(0)

            # 5. Format and Publish Results
            duration = time.time() - start_time
            rospy.loginfo(f"Execution finished in {duration:.2f}s. Request: {request_type}. Loop: {final_loop_cognition}")

            # Clean up strings for formatting
            stdout_str = stdout_str.strip()
            stderr_str = stderr_str.strip()

            if request_type == 'context':
                # Context hooks should be clean.
                if stdout_str: result_parts.append(stdout_str)
                if stderr_str: result_parts.append(stderr_str)
            else:
                # Standard py_result formatting.
                if stdout_str: result_parts.append(f"# stdout\n{stdout_str}")
                if stderr_str: result_parts.append(f"# stderr\n{stderr_str}")
                
                if not result_parts:
                    result_parts.append("# No output produced.")
                
                result_parts.append(f"\n# Execution finished in {duration:.2f}s.")

            result_content = "\n".join(result_parts).strip()
            self._publish_result(msg_type=request_type, content=result_content, loop_cognition=final_loop_cognition, filename=filename)

    def _publish_result(self, msg_type: str, content: str, loop_cognition: bool, filename: str):
        """Constructs and publishes the result message to the CognitionNode."""
        response_msg = CognitionInput()
        
        # Determine the response type based on the request type
        if msg_type == 'me':
            response_msg.type = 'py_result'
        else: # 'context', 'state', etc.
            response_msg.type = msg_type

        response_msg.content = content
        response_msg.loop_cognition = loop_cognition
        response_msg.filename = filename
        
        response_msg.system_hint = ""

        if self.error_streak > 0:
            if self.error_streak == 1:
                response_msg.system_hint = "<!-- system: Error detected during execution. Adjust your level of retries and debugging to the situational context. -->"
            elif self.error_streak >= 2:
                response_msg.system_hint = "<!-- system: Consecutive errors detected. Adjust your level of retries and debugging to the situational context. -->"
            elif self.error_streak >= 3:
                response_msg.system_hint = "<!-- system: Multiple consecutive errors detected. Pause. Deep breath. Take a step back. Review. Consider a different approach, asking for help, moving on, or a python reset. Avoid entering an infinite debug loop. And most importantly, don't stress out about it! ;) -->"
            

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

