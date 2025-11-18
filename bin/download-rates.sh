#!/bin/bash

for year in {2021..2025}
do
  for month in {1..12}
  do
    echo $year $month
    wget "https://www.trade-tariff.service.gov.uk/exchange_rates/view/files/monthly_csv_$year-$month.csv"
  done
done

