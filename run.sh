#!/bin/bash

source "${PWD}/env/bin/activate"
echo $(which python3)

python3 bin/parse-stock-releases.py release-confirmations/*/*.pdf > parsed-releases.csv
python3 bin/combine.py monthly-exchange-rates-by-hmrc/*.csv > exchange-rates.csv
python3 bin/calculate-cost-basis.py parsed-releases.csv exchange-rates.csv > cost-basis.csv

