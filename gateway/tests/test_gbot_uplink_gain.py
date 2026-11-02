# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
from stackchan_mcp.gbot_voice_bridge import apply_pcm16_gain, pcm16_rms
import struct

def test_apply_pcm16_gain_identity():
    pcm = struct.pack("<4h", 0, 1000, -1000, 20000)
    assert apply_pcm16_gain(pcm, 1.0) == pcm
    assert apply_pcm16_gain(pcm, 0.0) == pcm

def test_apply_pcm16_gain_scales_and_clips():
    pcm = struct.pack("<3h", 1000, -1000, 20000)
    out = apply_pcm16_gain(pcm, 2.0)
    assert struct.unpack("<3h", out) == (2000, -2000, 32767)
    assert pcm16_rms(pcm) > 0
