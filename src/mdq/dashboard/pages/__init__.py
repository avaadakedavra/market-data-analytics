"""One module per page, each exposing a single `render(backend)` function.

Pages are deliberately thin — read a widget, call `mdq.dashboard.cache`, draw a figure from
`mdq.dashboard.charts` — because `AppTest` can only assert on rendered widgets and is
brittle about anything more. Every judgement a page could get wrong lives one layer down in
`mdq.dashboard.backend`, where a test can reach it directly.

Not a Streamlit `pages/` directory in the framework's sense: navigation is declared
explicitly in `mdq.dashboard.app`, so these are importable modules rather than scripts, and
a test can call `overview.render(backend)` with a backend of its choosing.
"""
