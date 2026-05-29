import os
import numpy as np
import av

SAMPLE_RATE = 16000

def analyze_pauses(path):
    print(f"Analyzing pauses in: {path}")
    try:
        container = av.open(path)
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format='fltp', layout='mono', rate=SAMPLE_RATE)
        
        audio_frames = []
        for frame in container.decode(stream):
            resampled_frames = resampler.resample(frame)
            for f in resampled_frames:
                audio_frames.append(f.to_ndarray())
        
        audio = np.concatenate(audio_frames, axis=1).flatten()
        duration = len(audio) / SAMPLE_RATE
        
        # Analyze volume in 100ms windows
        window_size = int(SAMPLE_RATE * 0.1)
        volumes = []
        for i in range(0, len(audio), window_size):
            window = audio[i:i+window_size]
            if len(window) == 0: continue
            volumes.append(np.max(np.abs(window)))
        
        volumes = np.array(volumes)
        threshold = 0.01 # Threshold for "silence"
        
        pauses = []
        in_pause = False
        pause_start = 0
        
        for i, v in enumerate(volumes):
            time = i * 0.1
            if v < threshold:
                if not in_pause:
                    in_pause = True
                    pause_start = time
            else:
                if in_pause:
                    in_pause = False
                    pause_duration = time - pause_start
                    if pause_duration > 0.5:
                        pauses.append((pause_start, pause_duration))
        
        print(f"Total duration: {duration:.2f}s")
        print(f"Pauses found (>0.5s): {len(pauses)}")
        for start, dur in pauses:
            print(f"  - Pause at {start:.1f}s, duration: {dur:.1f}s")
            
        return audio, pauses
    except Exception as e:
        print(f"Error: {e}")
        return None, []

if __name__ == "__main__":
    path = "/var/folders/83/sspzfbbx1dl51zgfzpvvnm6m0000gn/T/ru.keepcoder.Telegram/Новая запись 5.m4a"
    analyze_pauses(path)
