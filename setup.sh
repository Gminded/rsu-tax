#!/bin/bash

python3 -m venv "${PWD}/env"
source "${PWD}/env/bin/activate"

pip install pandas pdfminer.six pytest streamlit playwright exchange_calendars
python -m playwright install chromium

