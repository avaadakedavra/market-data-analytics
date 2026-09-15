"""The Streamlit entry point: `streamlit run src/mdq/dashboard/app.py`.

Deliberately three lines. Importing a Streamlit script *runs* it, so anything written here
would be unreachable from a test; the app itself is assembled in `mdq.dashboard.shell`,
which is an ordinary importable module.

    make ui                    # everything in one process
    make api + make ui-http    # the dashboard as a client of the API
    make dev                   # both, together
"""

from mdq.dashboard.shell import render

render()
