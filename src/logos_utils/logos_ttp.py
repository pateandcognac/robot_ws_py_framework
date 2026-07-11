#!/usr/bin/env python3
"""Send emoji-punctuated text-to-performance or silent command jobs to Logos."""

import argparse
import json
import sys
import time


DEFAULT_ENGINE = "kokoro"
DEFAULT_SPEED = 1.0
DEFAULT_VOLUME = 1.0


class CliError(Exception):
    """An error that should be presented without a traceback."""


def build_parser():
    examples = """how emoji punctuation works:
  Place a recognized emoji after the phrase it should perform. The Logos
  action server uses that emoji as a cue into its face and arm animation
  lookup tables, then plays the selected animatronics in time with speech.

  Text without emoji is still spoken normally. Available performances depend
  on the emoji presets loaded by the running Logos stack.

examples:
  Perform a short, emoji-punctuated line:
    %(prog)s "Hello from Logos! 👋"

  Cue different performances as the speech progresses:
    %(prog)s "I have an idea! 💡 Let us try it. 🤖"

  Quotes are optional for simple text; remaining words are joined:
    %(prog)s Hello there, Mark! 👋

  Choose an engine and voice:
    %(prog)s --engine kokoro --voice am_onyx "Systems online. 🤖"
    %(prog)s --engine piper --voice en_US-joe-medium "Hello! 👋"
    %(prog)s --engine espeak --voice en-us+m7 "Testing one two. 🎙️"
    %(prog)s --engine festival --voice cmu_us_slt_arctic_hts "Good morning. 🌞"

  Adjust speech:
    %(prog)s --speed 1.2 --volume 0.9 "A little faster! ⚡"

  Pass an engine-specific speaker index:
    %(prog)s --engine piper --voice en_US-arctic-medium --speaker 0 "Hello. 👋"

  Force fresh generated face/arm animation and wait for it to fully
  complete before speaking (perfect sync, more latency):
    %(prog)s --face generate --arms generate --sync 1.0 "Testing! 🧪"

  Same, but start as soon as the first frame streams in (snappier):
    %(prog)s --face generate --sync 0.0 "Quick reaction. 😲"

  Zero-latency canned expressions only, no generation:
    %(prog)s --face lut --arms lut "Classic moves. 🕺"

  Silent command mode: animate without speaking:
    %(prog)s --command --channel both --duration 3 "barely contained fury 🌋"
    %(prog)s --command --channel face --face fuzzy "I am not pleased."
    %(prog)s --command --channel arms --arms generate --sync 0.0 "big shrug"

  Add arbitrary engine parameters as JSON:
    %(prog)s --params '{"speaker": 2, "noise_scale": 0.5}' "Custom settings. ⚙️"

  Pipe or redirect text (stdin is automatic when no text is given):
    echo "This came from a pipe. 📣" | %(prog)s
    printf 'Line one. 👀\\nLine two. 👋\\n' | %(prog)s --engine kokoro
    %(prog)s < announcement.txt

  Explicitly read stdin, including interactively until Ctrl-D:
    %(prog)s --stdin
    generate_message | %(prog)s -

  Use a namespaced action server and hide chunk feedback:
    %(prog)s --action /robot/speak --no-feedback "Testing. 🧪"
"""

    parser = argparse.ArgumentParser(
        description=(
            "Logos emoji-punctuated text-to-performance (TTP): send text to "
            "the Logos speak action server, or use command mode to animate "
            "face/arms silently. Recognized emoji cue facial and arm "
            "animatronics; prose can drive fuzzy LUT lookup or generation. "
            "Text may be supplied as command-line words, through stdin, or "
            "by using '-' as the text."
        ),
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "text",
        nargs="*",
        help=(
            "text to perform; place cue emoji after their phrases "
            "(multiple arguments are joined with spaces)"
        ),
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read text from stdin until EOF (Ctrl-D in an interactive terminal)",
    )
    parser.add_argument(
        "-e",
        "--engine",
        default=DEFAULT_ENGINE,
        choices=("kokoro", "piper", "espeak", "festival"),
        help="TTS engine; ignored in command mode (default: %(default)s)",
    )
    parser.add_argument(
        "-V",
        "--voice",
        help="engine-specific voice name (default: backend default)",
    )
    parser.add_argument(
        "-s",
        "--speed",
        type=positive_float,
        default=DEFAULT_SPEED,
        help="speech speed multiplier (default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--volume",
        type=nonnegative_float,
        default=DEFAULT_VOLUME,
        help="volume multiplier (default: %(default)s)",
    )
    parser.add_argument(
        "--speaker",
        type=int,
        help="optional engine-specific speaker index",
    )
    parser.add_argument(
        "--face",
        metavar="POLICY",
        help=(
            "face-animation source cascade, e.g. 'lut', 'fuzzy', 'saved', "
            "'generate', or a comma-separated cascade like "
            "'generate,saved,fuzzy,lut' (default: system default cascade)"
        ),
    )
    parser.add_argument(
        "--arms",
        metavar="POLICY",
        help="arm-animation source cascade, same options as --face",
    )
    parser.add_argument(
        "--sync",
        type=sync_value,
        default=None,
        metavar="0.0-1.0",
        help=(
            "loosey-goosey dial: fraction of generated face/arm frames to "
            "wait for before playback starts. Speech mode defaults to 1.0, "
            "which waits for generation to fully complete and plays it in "
            "perfect sync with the audio; command mode defaults to legacy "
            "gesture behavior, roughly 0.0. Explicit 0.0 starts on the first "
            "generated frame (snappier, herkier); values in between fudge "
            "the rest in as they stream. Only matters when a policy actually "
            "generates -- 'lut'-only cues are always instant."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("speech", "command"),
        default="speech",
        help="speech sends a SpeakAction goal; command publishes silent face/arm cues",
    )
    parser.add_argument(
        "--command",
        action="store_const",
        const="command",
        dest="mode",
        help="shortcut for --mode command; animate silently without TTS",
    )
    parser.add_argument(
        "--channel",
        choices=("face", "arms", "both"),
        default="both",
        help="command-mode target channel (default: %(default)s)",
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        default=3.0,
        metavar="SECONDS",
        help="command-mode gesture duration in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "-p",
        "--params",
        type=json_object,
        default={},
        metavar="JSON",
        help=(
            "additional engine parameters as a JSON object; --voice, --speed, "
            "--volume, and --speaker take precedence"
        ),
    )
    parser.add_argument(
        "-a",
        "--action",
        default="/speak",
        help="ROS Speak action name (default: %(default)s)",
    )
    parser.add_argument(
        "--server-timeout",
        type=nonnegative_float,
        default=10.0,
        metavar="SECONDS",
        help="seconds to wait for the action server; 0 waits forever (default: %(default)s)",
    )
    parser.add_argument(
        "--result-timeout",
        type=nonnegative_float,
        default=0.0,
        metavar="SECONDS",
        help="seconds to wait for completion; 0 waits forever (default: %(default)s)",
    )
    parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="do not print per-chunk synthesis feedback",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only print errors",
    )
    return parser


def positive_float(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_float(value):
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def sync_value(value):
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0.0 and 1.0")
    return number


def json_object(value):
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def read_text(args):
    uses_dash = args.text == ["-"]
    if "-" in args.text and not uses_dash:
        raise CliError("'-' must be the only text argument when reading stdin")
    if args.stdin and args.text:
        raise CliError("--stdin cannot be combined with command-line text")

    if args.stdin or uses_dash or not args.text:
        if not (args.stdin or uses_dash) and sys.stdin.isatty():
            raise CliError("no text supplied; pass text, pipe input, or use --stdin")
        text = sys.stdin.read()
    else:
        text = " ".join(args.text)

    if not text.strip():
        raise CliError("text input is empty")
    return text


def build_engine_params(args):
    params = dict(args.params)
    params["speed"] = args.speed
    params["volume"] = args.volume
    if args.voice:
        params["voice"] = args.voice
    if args.speaker is not None:
        params["speaker"] = args.speaker

    # TTP performance-pipeline knobs (face/arm source cascade + sync dial),
    # same shape logos.emote.ttp() sends; the director strips this key
    # before it reaches the TTS engine itself.
    performance = dict(params.get("performance") or {})
    if args.face:
        performance["face_policy"] = args.face
    if args.arms:
        performance["arm_policy"] = args.arms
    performance["sync"] = 1.0 if args.sync is None else args.sync
    params["performance"] = performance

    return params


def build_command_payload(args, text, channel):
    payload = {
        "text": text,
        "duration": args.duration,
        "cue_id": "cli_{}_{}".format(channel, int(time.time() * 1000)),
    }
    if args.sync is None:
        payload["expect_track"] = True
    else:
        payload["sync"] = args.sync

    policy = args.face if channel == "face" else args.arms
    if policy:
        payload["policy"] = policy
    return payload


def perform(args, text):
    import actionlib
    import rospy
    from logos_msgs.msg import SpeakAction, SpeakGoal

    rospy.init_node("logos_ttp_cli", anonymous=True, disable_signals=True)
    client = actionlib.SimpleActionClient(args.action, SpeakAction)

    if not args.quiet:
        print(
            f"Waiting for Logos text-to-performance action server "
            f"'{args.action}'...",
            file=sys.stderr,
        )

    if args.server_timeout == 0:
        server_found = client.wait_for_server()
    else:
        server_found = client.wait_for_server(rospy.Duration(args.server_timeout))
    if not server_found:
        raise CliError(
            f"Logos action server '{args.action}' was not available within "
            f"{args.server_timeout:g} seconds"
        )

    goal = SpeakGoal()
    goal.utterance_text = text
    goal.engine = args.engine
    goal.engine_params = json.dumps(build_engine_params(args))

    def feedback_cb(feedback):
        if args.no_feedback or args.quiet:
            return
        chunk_number = feedback.current_chunk_index + 1
        detail = repr(feedback.text_snippet)
        if feedback.emoji_snippet:
            detail += f" (emoji: {feedback.emoji_snippet})"
        print(
            f"[{chunk_number}/{feedback.total_chunks}] {detail}",
            file=sys.stderr,
        )

    client.send_goal(goal, feedback_cb=feedback_cb)
    if not args.quiet:
        print(
            f"Sent {len(text)} characters using {args.engine}; "
            "waiting for synchronized performance...",
            file=sys.stderr,
        )

    if args.result_timeout == 0:
        completed = client.wait_for_result()
    else:
        completed = client.wait_for_result(rospy.Duration(args.result_timeout))
    if not completed:
        client.cancel_goal()
        raise CliError(
            f"text-to-performance job did not finish within "
            f"{args.result_timeout:g} seconds; cancel requested"
        )

    result = client.get_result()
    if result is None:
        raise CliError("text-to-performance action returned no result")
    if not result.success:
        raise CliError(f"text-to-performance failed: {result.final_message}")

    if not args.quiet:
        print(
            f"Performance queued: {result.final_message} "
            f"(audio duration {result.total_duration:.2f}s)"
        )


def wait_for_subscribers(rospy, publishers, timeout_s):
    if timeout_s == 0:
        deadline = None
    else:
        deadline = time.time() + timeout_s

    while not rospy.is_shutdown():
        if all(pub.get_num_connections() > 0 for pub in publishers):
            return True
        if deadline is not None and time.time() >= deadline:
            return False
        rospy.sleep(0.05)
    return False


def command(args, text):
    import rospy
    from std_msgs.msg import String

    rospy.init_node("logos_ttp_cli_command", anonymous=True, disable_signals=True)

    publishers = []
    messages = []
    if args.channel in ("face", "both"):
        pub = rospy.Publisher("/face/emoji_command", String, queue_size=5)
        publishers.append(pub)
        messages.append(("face", pub, build_command_payload(args, text, "face")))
    if args.channel in ("arms", "both"):
        pub = rospy.Publisher("/arm/emoji_command", String, queue_size=5)
        publishers.append(pub)
        messages.append(("arms", pub, build_command_payload(args, text, "arms")))

    if not publishers:
        raise CliError("no command channel selected")

    if not args.quiet:
        channels = ", ".join(label for label, _, _ in messages)
        print(f"Waiting for command subscribers on {channels}...", file=sys.stderr)

    if not wait_for_subscribers(rospy, publishers, args.server_timeout):
        raise CliError(
            f"command subscribers were not available within "
            f"{args.server_timeout:g} seconds"
        )

    for _, pub, payload in messages:
        pub.publish(String(data=json.dumps(payload, ensure_ascii=False)))

    # Give rospy a moment to flush the one-shot publish before the process exits.
    rospy.sleep(0.2)
    if not args.quiet:
        channels = ", ".join(label for label, _, _ in messages)
        print(
            f"Silent command queued on {channels}: {len(text)} characters "
            f"for {args.duration:.2f}s"
        )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        text = read_text(args)
        if args.mode == "command":
            command(args, text)
        else:
            perform(args, text)
    except CliError as exc:
        parser.exit(1, f"{parser.prog}: error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, f"\n{parser.prog}: interrupted\n")
    except ImportError as exc:
        parser.exit(
            1,
            f"{parser.prog}: error: ROS dependencies are unavailable ({exc}); "
            "source devel/setup.bash first\n",
        )


if __name__ == "__main__":
    main()
