"""teamview.lol: scout an opposing League of Legends team from their Riot IDs.

Run with:  streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="teamview.lol", page_icon="🔎", layout="wide")

# List the pages ourselves so the main one is called "Scout", not "app" after this file.
# Anything in pages/ is added after it, titled from its file name (pages/draft.py -> "Draft").
pages = [st.Page("scout_page.py", title="Scout", icon="🔎", default=True)]
pages += [st.Page(path, title=path.stem.replace("_", " ").title())
          for path in sorted((Path(__file__).parent / "pages").glob("*.py"))]
st.navigation(pages).run()
