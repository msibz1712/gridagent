# Plant register test data (real, openly licensed)

`register_excerpt.json.gz` is the aggregate that `gridagent.data.mastr.sync_register`
builds, made from the Marktstammdatenregister reference export of **1 October 2026**
(`Stichtag/Gesamtdatenexport_20261001_26.1.zip`) with `skip_heavy=True`: all of Germany,
every technology except solar and storage (those two files are ~1.6 GB). Tools refuse
solar and storage questions on it instead of answering 0 MW.

Source: Marktstammdatenregister – © Bundesnetzagentur,
Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0,
https://www.govdata.de/dl-de/by-2-0), https://www.marktstammdatenregister.de/MaStR/Datendownload.
Aggregated (municipality x technology x year); not otherwise changed.

Rebuild:

```bash
uv run python -c "from pathlib import Path; from gridagent.data.mastr import sync_register; \
sync_register(Path('tests/fixtures/mastr/register_excerpt.json.gz'), \
url='https://download.marktstammdatenregister.de/Stichtag/Gesamtdatenexport_20261001_26.1.zip', \
skip_heavy=True, progress=print)"
```
