#!/bin/bash

for year in $(seq 2021 $(date +%Y))
do
  for month in {1..12}
  do
    echo $year $month
    wget "https://www.trade-tariff.service.gov.uk/exchange_rates/view/files/monthly_csv_$year-$month.csv"
  done
done

