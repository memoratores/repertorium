set dotenv-load

venv   := ".venv"
python := venv + "/bin/python"
port   := "8000"
books  := env("BOOKS")

install:
    python3 -m venv {{venv}}
    {{python}} -m pip install -r requirements.txt

build:
    {{python}} build.py "{{books}}"

serve:
    PORT={{port}} {{python}} serve.py

dev: build serve
