"""Check audio framing helpers without requiring LiveKit or Google packages."""

import io
import struct
import unittest
import wave

from ivr_poc.connectors.voice import decode_cx_audio, rms


class AudioFormatTests(unittest.TestCase):
    def test_pcm_energy(self):
        self.assertEqual(rms(b"\0" * 320), 0)
        self.assertAlmostEqual(rms(struct.pack("<hhhh", 1000, -1000, 1000, -1000)), 1000)

    def test_wav_header_is_removed_before_livekit_playback(self):
        output = io.BytesIO()
        pcm = struct.pack("<hhhh", 1, -1, 2, -2)
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(pcm)
        self.assertEqual(decode_cx_audio(output.getvalue()), pcm)

    def test_wrong_sample_rate_and_odd_pcm_are_rejected(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(b"\0" * 8)
        with self.assertRaises(ValueError):
            decode_cx_audio(output.getvalue())
        with self.assertRaises(ValueError):
            decode_cx_audio(b"\0")
