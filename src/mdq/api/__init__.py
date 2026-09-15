"""The HTTP API: a thin, generated surface over `mdq.service`.

```bash
make api                  # uvicorn mdq.api.app:app --reload --port 8000
open http://localhost:8000/docs
```

Every route does the same three things — parse the query, call one `MarketDataService`
method, page the answer — so the API holds no business logic of its own and cannot drift
from the dashboard, which calls the same service in process.

The analytic routes are not written at all: `mdq.api.routes_analytics` generates one per
entry in the analytic registry, deriving each route's query parameters from the analytic's
pydantic `Params` model. OpenAPI is the API documentation.
"""

from mdq.api.app import create_app

__all__ = ["create_app"]
