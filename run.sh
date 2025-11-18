#!/bin/bash

set -e

OUT_DIR=out
RELEASE_CONFIRMATIONS=release-confirmations
EXCHANGE_RATES=monthly-exchange-rates-by-hmrc
SALES=sales/sales.csv

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --out|-o)
            OUT_DIR="$2"
            shift 2
            ;;
        --release-confirmations|-r)
            RELEASE_CONFIRMATIONS="$2"
            shift 2
            ;;
        --exchange-rates|-x)
            EXCHANGE_RATES="$2"
            shift 2
            ;;
        --sales|-s)
            SALES="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--out DIR]"
            echo
            echo "Options:"
            echo "  --out, -o DIR   Specify output directory (default: ./out)"
            echo "  --release-confirmations, -r DIR   Specify directory that contains all the stock release confirmations (default: ./release-confirmations)"
            echo "  --exchange-rates, -x DIR   Specify directory that contains the monthly exchange rates (default: ./monthly-exchange-rates-by-hmrc)"
            echo "  --sales, -s DIR   Specify csv file that indicates extra sales (default: ./sales/sales.csv)"
            echo "  -h, --help      Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown parameter: $1"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done

# Create directory if it doesn't exist
mkdir -p "$OUT_DIR"

source "${PWD}/env/bin/activate"
echo $(which python3)


python3 bin/parse-stock-releases.py $RELEASE_CONFIRMATIONS/*.pdf > $OUT_DIR/parsed-releases.csv
python3 bin/combine.py $EXCHANGE_RATES/*.csv > $OUT_DIR/exchange-rates.csv
python3 bin/calculate-cost-basis.py -r $OUT_DIR/parsed-releases.csv -x $OUT_DIR/exchange-rates.csv -s $SALES > $OUT_DIR/cost-basis.csv

