# SpeechData.msg
# published on /face/tts_chunk
string text_snippet # text being spoken
string emoji # facial and arm animation emoji identifier
int16[] audio_data # spoken text_snippet
float32 duration # of audio (and hence, intended duration of face or arm animation, or how long to display text_snippet for subtitle, or twist kobuki base)