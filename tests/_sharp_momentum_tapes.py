"""Deterministic operating-curve tapes for momentum v2 (sharp-move) statistical validation.

These are SEPARATE, long, committed seeded fixtures — DISTINCT from the sealed 4-tick golden
fixtures (``tests/golden/*.json``). The two roles must never be conflated:

* The golden fixtures are the sealed-byte regression trap for the ORCHESTRATOR/EVIDENCE path;
  they stay 4-tick and v1 (byte-identical), and nothing here touches them.
* These tapes exercise the SHARP-MOMENTUM statistics (warmup, min-samples, directional
  Page-Hinkley, robust-z scale-floor, cooldown, no-lookahead, and the false-positive operating
  curve). They need many more ticks than the golden — hence a separate, purpose-built file.

Each tape is a per-tick ``home`` de-vigged probability in basis points (``away = 10000 - home``),
authored once with a fixed RNG then FROZEN as literals here — no runtime randomness, so the unit
tests are reproducible and offline. (A real long TxLINE-derived tape belongs to the demo /
curated-artifact lane, not this deterministic suite.)
"""

from __future__ import annotations

# (a) NULL / NOISE: mean 5000, ~35 bps sd, no drift — the false-positive-budget tape.
TAPE_NOISE: list[int] = [4963, 4978, 5053, 4977, 4998, 4990, 4944, 5005, 5013, 4999, 5015, 5060, 5015, 5030, 4958, 4981, 4983, 4998, 5015, 4956, 4977, 5025, 4960, 5014, 5007, 5025, 4999, 4945, 4971, 5035, 5065, 4994, 5048, 5043, 4983, 5037, 4994, 4975, 5005, 4957]  # noqa: E501

# (b) INJECTED SHARP move: flat warmup then a strong sustained rise (home).
TAPE_SHARP: list[int] = [5000, 5009, 4992, 4973, 4986, 4970, 5002, 5040, 4985, 4981, 5015, 5011, 5003, 4972, 5132, 5306, 5425, 5603, 5734, 5906, 6055, 6247, 6387, 6578, 6582, 6572, 6502, 6561, 6576, 6581, 6532, 6563]  # noqa: E501

# (c) SLOW DRIFT: gentle +11 bps/tick drift with noise — v1 false-fires; v2 should be quieter.
TAPE_DRIFT: list[int] = [5001, 5052, 5059, 5018, 5035, 5039, 5083, 5075, 5110, 5044, 5157, 5118, 5152, 5139, 5143, 5179, 5201, 5181, 5193, 5230, 5194, 5186, 5254, 5233, 5206, 5251, 5272, 5261, 5263, 5320, 5357, 5334, 5330, 5375, 5396, 5376, 5412, 5438, 5412, 5405]  # noqa: E501

# (d) SINGLE OUTLIER: noise around 5000 with one +550 bps spike at idx 20 that reverts.
TAPE_OUTLIER: list[int] = [5002, 4986, 5002, 5021, 4947, 5051, 4986, 4982, 4969, 5028, 5020, 5037, 5027, 5008, 5010, 5028, 4974, 4999, 5011, 4986, 5572, 4989, 5020, 5004, 5014, 4954, 4974, 5040, 5005, 4998, 5029, 5023, 4999, 4981, 5059, 5021, 4953, 5025, 5023, 5024]  # noqa: E501

# (e) SUSTAINED REPRICING: flat warmup then a ramp to a new level that holds (the catch case).
TAPE_REPRICE: list[int] = [5037, 4954, 5008, 4990, 4992, 4996, 4964, 4996, 4984, 5060, 5004, 4994, 4995, 4988, 4981, 4993, 5150, 5289, 5457, 5590, 5743, 5916, 6051, 6185, 6340, 6501, 6540, 6496, 6496, 6521, 6483, 6495, 6519, 6513, 6503, 6514]  # noqa: E501

# (f) DOWN then small UP bounce (home): a sustained DOWN move then a small up bounce on home.
TAPE_DOWN_UP: list[int] = [5008, 4974, 5019, 5024, 4951, 4967, 5003, 4992, 5000, 4979, 5022, 5019, 5002, 5028, 4887, 4713, 4585, 4411, 4294, 4127, 3975, 3816, 3878, 3864, 3862, 3862]  # noqa: E501

# Short volatility-noise tape that nets > 50 bps inside v1's 8-tick lookback: v1's raw last-first
# delta false-fires, v2 stays quiet (the direct v1-vs-v2 contrast, AC-2D-502).
TAPE_V1_TRAP: list[int] = [5000, 5030, 4995, 5040, 5010, 5050, 5015, 5055]
