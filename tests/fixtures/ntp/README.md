# SYNTHETIC redispatch data (not real)

`redispatch_synthetic.csv` is invented data in the exact CSV format of the netztransparenz.de
Web API (documentation v1.14, "Format 5"). It contains no real measures: netztransparenz.de
content is copyright-protected and may not be reproduced without the TSOs' consent, so real
responses are never committed. Plant names are fictitious ("Plant A", "Wind cluster N1", ...).

It covers 2-8 November 2026 (the eval week) and is built to exercise the tricky cases:
a measure crossing midnight and the period boundary, total energy below mean MW x duration,
a plant test run ("Probefahrt", not congestion management), a countertrade, and all three
primary energy types.
