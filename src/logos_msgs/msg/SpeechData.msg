# SpeechData.msg
# published on /face/tts_chunk
string text_snippet       # Text being spoken
string emoji              # Facial/arm animation identifier
int16[] audio_data        # Raw audio samples (PCM)
int32 sample_rate         # e.g., 24000 for Kokoro, 22050 for Piper
float32 duration          # Duration in seconds
int32 current_chunk_index # using index to track last chunk
int32 total_chunks