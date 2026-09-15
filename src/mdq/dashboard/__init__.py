"""The Streamlit dashboard: four pages over a `Backend`, and no logic in the pages.

```
make ui                      # everything in one process (EmbeddedBackend)
make api  +  make ui-http    # the dashboard as an HTTP client of the API (HttpBackend)
```

The split that matters is `mdq.dashboard.backend`. It holds the protocol, both
implementations and every decision a page could get wrong — which severity the findings
table opens on, how an activity profile becomes a heatmap, how a JSON instant becomes a
Polars timestamp — so all of it is unit-tested without a script runner, and the two
backends are asserted against each other rather than assumed to agree.

`app.py` is only importable by Streamlit (importing it *runs* the app), which is why every
page is a plain `render(backend)` function that a test drives directly.
"""
