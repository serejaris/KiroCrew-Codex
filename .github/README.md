# GitHub automation

This community fork ships **source only**.

GitHub Actions from upstream (CI, Build, signing, CDN publish, OTA, review bots)
are intentionally **not** enabled here. Verify locally:

```bash
pip install -e ".[voice]" --group dev
pytest
cd website && npm ci && npm run check
```

Do not treat Actions artifacts (if any appear from history) as supported installers.
