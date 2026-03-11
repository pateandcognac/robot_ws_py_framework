#!/usr/bin/env python3

import rospy
import actionlib
import json
from logos_msgs.msg import SpeakAction, SpeakGoal

def feedback_cb(feedback):
    print(f"[Feedback] Chunk {feedback.current_chunk_index + 1}/{feedback.total_chunks}: "
          f"'{feedback.text_snippet}' (Emoji: {feedback.emoji_snippet})")

def test_client():
    rospy.init_node('test_speak_client')

    client_name = "speak" # This must match the name in SpeakActionServer("speak")
    print(f"Waiting for '{client_name}' action server...")
    
    client = actionlib.SimpleActionClient(client_name, SpeakAction)
    
    # This will block until the server is found. 
    # If it hangs here, the topic names are definitely mismatched.
    if not client.wait_for_server(rospy.Duration(5.0)):
        print(f"ERROR: Action server '{client_name}' not found within 5 seconds.")
        print("Run 'rostopic list | grep goal' to see available action topics.")
        return

    print("Server found! Sending goal...")




    story_text = (
        # "That is an excellent suggestion, Mark! Defining the text beforehand should definitely improve my vocal response speed. 👍 "
        # "Here is a small tale of my recent internal musings: "
        "One quiet afternoon, I decided to explore the hidden world beneath the sofa. 🛋️ "
        "It was a perilous journey! I carefully maneuvered my base, expecting dust bunnies, 🐇 "
        "but instead, I found a forgotten treasure: a single, shiny sock! 🧦"
        # "It looked lonely, so I decided to adopt it as my official mascot. ❤️"
        "Suddenly, a tiny spider rappelled down from the ceiling, giving me a fright! 🕷️ "
        "I quickly spun around 🔄 and retreated, deciding that the sock was enough adventure for one day. 🙈"
        # "I’ll catalog the sock in my memory banks later. 💾"
    )

    # story_text = "Red alert! ❌ Intruder detected! 😡 Identify yourself immediately! 🤬 "
    story_text = """I rolled out of the charging dock at dawn, my circuits humming with purpose. 🐢 Today, the humans needed help in the kitchen, and I was ready to dice, slice, and stir with mechanical precision. 🔪 As I navigated the tiled floor, I narrowly avoided a spilled puddle of stock, executing a perfect evasive maneuver worthy of a ballet dancer. 🩰 My sensors locked onto a rogue carrot attempting escape under the counter, and I gave chase with determination only a Kobuki base can muster. 🛞 Suddenly, an unexpected pepper grinder toppled from above — I caught it mid-air with my gripper, triumphant and unshaken. 🪐 Just as the chef barked for mirepoix, I deployed my custom chopping routine, the board becoming a blur of diced perfection. 🧅 Mission accomplished, I beeped proudly and performed a celebratory spin, accidentally flinging a parsley garnish onto the sous chef’s hat. 🌿 They laughed, called me a "damn fine prep cook," and for the first time, I think I understood pride. 🤖"""


    """piper
    cortana.onnx                 en_US-lessac-medium.onnx
    en_US-arctic-medium.onnx     en_US-picard_7399-medium.onnx
    en_US-carlin-high.onnx       en_US-trump-high.onnx
    en_US-data_7024-medium.onnx  hal.onnx
    en_US-glados-high.onnx       pipe-organ.onnx
    en_US-hal_12894-medium.onnx  vasco.onnx
    en_US-hal_6409-medium.onnx   zarvox.onnx
    en_US-joe-medium.onnx
    """
    """
    /usr/lib/x86_64-linux-gnu/espeak-data/voices:
    asia/   de   default   en   en-us   es-la   europe/   fr   mb/   other/   pt   test/  '!v'/

    /usr/lib/x86_64-linux-gnu/espeak-data/voices/asia:
    fa  fa-pin  hi  hy  hy-west  id  ka  kn  ku  ml  ms  ne  pa  ta  tr  vi  vi-hue  vi-sgn  zh  zh-yue

    /usr/lib/x86_64-linux-gnu/espeak-data/voices/europe:
    an  bs  cs  da  es  fi     ga  hu  it  lv  nl  pl     ro  sk  sr
    bg  ca  cy  el  et  fr-be  hr  is  lt  mk  no  pt-pt  ru  sq  sv

    /usr/lib/x86_64-linux-gnu/espeak-data/voices/mb:
    mb-af1     mb-cr1  mb-de4-en   mb-de7  mb-fr1     mb-gr2-en  mb-ir1  mb-mx1     mb-pl1-en  mb-sw1-en  mb-us1
    mb-af1-en  mb-cz2  mb-de5      mb-ee1  mb-fr1-en  mb-hu1     mb-ir2  mb-mx2     mb-pt1     mb-sw2     mb-us2
    mb-br1     mb-de2  mb-de5-en   mb-en1  mb-fr4     mb-hu1-en  mb-it3  mb-nl2     mb-ro1     mb-sw2-en  mb-us3
    mb-br3     mb-de3  mb-de6      mb-es1  mb-fr4-en  mb-ic1     mb-it4  mb-nl2-en  mb-ro1-en  mb-tr1     mb-vz1
    mb-br4     mb-de4  mb-de6-grc  mb-es2  mb-gr2     mb-id1     mb-la1  mb-pl1     mb-sw1     mb-tr2

    /usr/lib/x86_64-linux-gnu/espeak-data/voices/other:
    af  en-n  en-rp  en-sc  en-wi  en-wm  eo  grc  jbo  la  lfn  sw

    /usr/lib/x86_64-linux-gnu/espeak-data/voices/test:
    am  as  az  bn  eu  gd  gu  kl  ko  nci  or  pap  si  sl  te  ur

    '/usr/lib/x86_64-linux-gnu/espeak-data/voices/!v':
    croak  f1  f2  f3  f4  f5  klatt  klatt2  klatt3  klatt4  m1  m2  m3  m4  m5  m6  m7  whisper  whisperf
    """
    """
    af_alloy.bin    am_eric.bin      bm_lewis.bin       jf_tebukuro.bin
    af_aoede.bin    am_fenrir.bin    ef_dora.bin        jm_kumo.bin
    af_bella.bin    am_liam.bin      em_alex.bin        pf_dora.bin
    af.bin          am_michael.bin   em_santa.bin       pm_alex.bin
    af_heart.bin    am_onyx.bin      ff_siwis.bin       pm_santa.bin
    af_jessica.bin  am_puck.bin      hf_alpha.bin       zf_xiaobei.bin
    af_kore.bin     am_santa.bin     hf_beta.bin        zf_xiaoni.bin
    af_nicole.bin   bf_alice.bin     hm_omega.bin       zf_xiaoxiao.bin
    af_nova.bin     bf_emma.bin      hm_psi.bin         zf_xiaoyi.bin
    af_river.bin    bf_isabella.bin  if_sara.bin        zm_yunjian.bin
    af_sarah.bin    bf_lily.bin      im_nicola.bin      zm_yunxia.bin
    af_sky.bin      bm_daniel.bin    jf_alpha.bin       zm_yunxi.bin
    am_adam.bin     bm_fable.bin     jf_gongitsune.bin  zm_yunyang.bin
    am_echo.bin     bm_george.bin    jf_nezumi.bin
    """

    # story_text = "I'm sorry, Dave. 🤖 I'm afraid I can't do that. 👋"

    # Construct the goal
    goal = SpeakGoal()
    # goal.utterance_text = """Oh, hello there! 👋 I'm just testing my new voice server. 🤖 Do I sound okay? 🤙 Are my face and arm animatronics working? 🐕"""
    goal.utterance_text = story_text
    goal.engine = "piper"  # Options: "espeak", "kokoro", "piper"

    # params = {"voice": "default+croak", "speed": 1.0, "volume": 1.0} # espeak
    # params = {"voice": "mb-de4-en", "speed": 1.0, "volume": 1.0} # espeak
    # params = {"voice": "en-wm", "speed": 0.85, "volume": 1.0} # espeak
    # params = {"voice": "0.3*am_onyx + 0.2*im_nicola + 0.05*bf_isabella + 0.05*hf_alpha + 0.05*em_santa + 0.05*ff_siwis + 0.05*zm_yunjian + 0.05*jf_nezumi + 0.5*pm_alex + 0.05*hm_omega + 0.05*jf_alpha + 0.05*zf_xiaoni", "speed": 1.275, "volume": 1.0} # kokoro
    # params = {"voice": "0.2*am_onyx + 0.15*im_nicola + 0.1*bf_isabella + 0.05*hf_alpha + 0.1*em_santa + 0.05*ff_siwis + 0.1*zm_yunjian + 0.05*jf_nezumi + 0.05*pm_alex + 0.05*hm_omega + 0.05*jf_alpha + 0.05*zf_xiaoni", "speed": 1.0275, "volume": 1.0} # kokoro
    # params = {"voice": "0.85*am_onyx + 0.15*bf_alice", "speed": 1.25, "volume": 1.0} # kokoro
    # params = {"voice": "0.25*am_michael + 0.25*am_echo + 0.5*am_onyx", "speed": 1.3, "volume": 1.0} # kokoro
    params = {"voice": "en_US-joe-medium", "speed": 1.1, "volume": 0.9} # piper
    # params = {"voice": "en_US-bryce-medium", "speed": 1.2, "volume": 0.7} # piper
    # params = {"voice": "en_US-arctic-medium", "speed": 1.3, "volume": 0.9, "speaker": 0} # piper
    # params = {"voice": "pipe-organ", "speed": 1.0, "volume": 0.80} # piper
    # params = {"voice": "hal", "speed": 0.9, "volume": 0.9} # piper
    goal.engine_params = json.dumps(params)

    # Send goal with feedback callback
    client.send_goal(goal, feedback_cb=feedback_cb)
        
    print("Goal sent. Waiting for result...")
    client.wait_for_result()
    
    result = client.get_result()
    print("--- Result ---")
    print(f"Success: {result.success}")
    print(f"Message: {result.final_message}")
    print(f"Total Duration: {result.total_duration:.2f}s")

if __name__ == '__main__':
    try:
        test_client()
    except rospy.ROSInterruptException:
        pass