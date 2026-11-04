# Experiments

The scripts behind the numbers in [the optimization list](../docs/optimizations.md). All use generated audio and tracks, and none use the network. They're rough research scripts rather than product code.

Run them from the repository root after installing the dependencies:

```bash
PYTHONPATH=. python experiments/track_budget.py
```

| Script | Question | Rough time |
| --- | --- | --- |
| `track_budget.py` | Does noise use up the tracker's budget? | 1 min |
| `detector_limits.py` | How do the frequency floor, bin size and window length affect detection? | 2 min |
| `matcher_gates.py` | Does the residual gate reject unrelated tones? Includes poor-geometry cases | 10 min |
| `matcher_flyovers.py` | How well does the search do on clean flyovers as conditions change? | 12 min |
| `matcher_cost.py` | How much search work repeats, and how long does it take? | 3 min |
