# Heartbeat sound provenance

All five heartbeat WAVs derive from BigSoundBank's **Heart Beat #4**
(recording 1929): https://bigsoundbank.com/heart-beat-4-s1929.html

The source is released under **CC0 / public domain**. License information:
https://bigsoundbank.com/licenses.html

All files are mono 44.1 kHz signed 16-bit PCM and exactly 200 ms. The four
variants fade to digital silence by 198 ms and carry a 2 ms zero tail; Natural
retains its clean two-sample zero endpoint. No synthesized layers were added.

## Files and processing

- `heartbeat_lub_dub.wav` (**Natural**, -0.9999 dBFS peak): source ranges
  73.521-178.521 ms and 303.146-398.146 ms were placed at 0 and 105.011 ms,
  respectively; gains 0.42/0.34 and 1.5 ms input/10 ms output fades were
  applied before resampling. A final +10.320933 dB gain set the delivery peak.
- `heartbeat_deep_chest.wav` (**Deep Chest**, -1.4999 dBFS peak): Natural was
  pitch-shifted to 0.88x with Rubber Band's smooth transients, soft detector,
  short window, and smoothing; filtered with 2-pole 24 Hz high-pass and
  145 Hz low-pass, +3.0 dB at 52 Hz (Q 0.9), and -1.5 dB at 92 Hz (Q 1.0).
  A 188-200 ms fade and final -1.469134710 dB gain were applied.
- `heartbeat_soft_resting.wav` (**Soft Resting**, -3.0000 dBFS peak): Natural
  received 2-pole 22 Hz high-pass and 112 Hz low-pass filters, -1.5 dB at
  78 Hz (Q 0.9), and 80% wet RMS compression (threshold 0.12, ratio 2:1,
  14 ms attack, 90 ms release, knee 4, 1x makeup). A 190-200 ms fade and
  final +4.550381510 dB gain were applied.
- `heartbeat_clear_stethoscope.wav` (**Clear Stethoscope**, -1.4999 dBFS
  peak): Natural was pitch-shifted to 1.14x with Rubber Band's crisp
  transients, percussive detector, short window, and no smoothing; filtered
  with 2-pole 42 Hz high-pass and 320 Hz low-pass, +3.0 dB at 105 Hz
  (Q 0.9), and +4.0 dB at 175 Hz (Q 0.8). It received 40% wet peak
  compression (threshold 0.16, ratio 1.8:1, 4 ms attack, 45 ms release,
  knee 2, 1x makeup), a 190-200 ms fade, and -1.347305237 dB final gain.
- `heartbeat_strong_pulse.wav` (**Strong Pulse**, -0.9999 dBFS peak): Natural
  received a 2-pole 28 Hz high-pass, +2.5 dB at 72 Hz (Q 0.8), +2.0 dB at
  125 Hz (Q 0.9), and 80% wet peak compression (threshold 0.10, ratio 4:1,
  3 ms attack, 70 ms release, knee 3, 2x makeup). Mild tanh soft clipping
  used threshold 0.82 and 4x oversampling before a 2-pole 260 Hz low-pass,
  a 190-200 ms fade, and +10.517292651 dB final gain.
