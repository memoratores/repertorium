#!/usr/bin/env python3
import os
from pathlib import Path

from livereload import Server

ROOT = Path(__file__).parent
DOCS_DIR = ROOT / "docs"

server = Server()
server.watch(str(DOCS_DIR / "index.json"))
server.watch(str(DOCS_DIR / "index.html"))
server.serve(port=int(os.getenv("PORT", "8000")), root=str(DOCS_DIR))
