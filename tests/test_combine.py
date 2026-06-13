"""Tests for bin/combine.py — combines HMRC monthly exchange-rate CSVs."""
import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def write_hmrc_csv(path, data_rows):
    """Write a minimal HMRC-style exchange-rate CSV in ISO-8859-1."""
    with open(path, "wb") as f:
        header = (
            "Country/Territories,Currency,Currency code,"
            "Currency units per £1,Start Date,End Date\n"
        )
        f.write(header.encode("ISO-8859-1"))
        for row in data_rows:
            f.write((row + "\n").encode("ISO-8859-1"))


# ── get_usd_row ───────────────────────────────────────────────────────────────

class TestGetUsdRow:
    def test_returns_parsed_row_when_found(self, tmp_path, combine):
        p = tmp_path / "rates.csv"
        write_hmrc_csv(p, ["USA,Dollar,USD,1.2643,01/01/2024,31/01/2024"])
        row = combine.get_usd_row(p)
        assert row is not None
        assert row[0] == "USA"
        assert row[2] == "USD"
        assert row[3] == "1.2643"
        assert row[4] == "01/01/2024"

    def test_returns_none_when_no_usd_row(self, tmp_path, combine):
        """Must return None — not loop forever — if the USD row is absent."""
        p = tmp_path / "rates.csv"
        write_hmrc_csv(p, ["France,Euro,EUR,1.1500,01/01/2024,31/01/2024"])
        assert combine.get_usd_row(p) is None

    def test_returns_none_for_empty_file(self, tmp_path, combine):
        p = tmp_path / "empty.csv"
        p.write_bytes(b"")
        assert combine.get_usd_row(p) is None

    def test_returns_none_for_header_only_file(self, tmp_path, combine):
        p = tmp_path / "rates.csv"
        write_hmrc_csv(p, [])
        assert combine.get_usd_row(p) is None

    def test_usd_row_found_after_other_currencies(self, tmp_path, combine):
        p = tmp_path / "rates.csv"
        write_hmrc_csv(p, [
            "France,Euro,EUR,1.1500,01/01/2024,31/01/2024",
            "Japan,Yen,JPY,185.00,01/01/2024,31/01/2024",
            "USA,Dollar,USD,1.2643,01/01/2024,31/01/2024",
        ])
        row = combine.get_usd_row(p)
        assert row is not None
        assert row[2] == "USD"

    def test_handles_iso8859_encoded_non_ascii_chars(self, tmp_path, combine):
        """Lines with accented characters (common in HMRC files) should not crash."""
        p = tmp_path / "rates.csv"
        with open(p, "wb") as f:
            f.write("Côte d'Ivoire,Franc,XOF,750.00,01/01/2024,31/01/2024\n".encode("ISO-8859-1"))
            f.write("USA,Dollar,USD,1.2643,01/01/2024,31/01/2024\n".encode("ISO-8859-1"))
        row = combine.get_usd_row(p)
        assert row is not None
        assert row[2] == "USD"


# ── main ──────────────────────────────────────────────────────────────────────

class TestMain:
    def test_single_file_outputs_header_and_usd_row(self, tmp_path, combine, capsys):
        p = tmp_path / "jan.csv"
        write_hmrc_csv(p, ["USA,Dollar,USD,1.2643,01/01/2024,31/01/2024"])
        rc = combine.main(["combine.py", str(p)])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert out[0].startswith("Country/Territories")
        assert "USD" in out[1]
        assert "1.2643" in out[1]

    def test_multiple_files_all_rows_present(self, tmp_path, combine, capsys):
        p1 = tmp_path / "jan.csv"
        p2 = tmp_path / "feb.csv"
        write_hmrc_csv(p1, ["USA,Dollar,USD,1.2643,01/01/2024,31/01/2024"])
        write_hmrc_csv(p2, ["USA,Dollar,USD,1.2800,01/02/2024,29/02/2024"])
        rc = combine.main(["combine.py", str(p1), str(p2)])
        assert rc == 0
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == 3          # 1 header + 2 data
        assert "1.2643" in out
        assert "1.2800" in out

    def test_missing_usd_row_reports_error_to_stderr(self, tmp_path, combine, capsys):
        p = tmp_path / "eur_only.csv"
        write_hmrc_csv(p, ["France,Euro,EUR,1.1500,01/01/2024,31/01/2024"])
        rc = combine.main(["combine.py", str(p)])
        assert rc == 1
        assert "Error" in capsys.readouterr().err

    def test_valid_files_still_output_when_one_is_missing_usd(self, tmp_path, combine, capsys):
        good = tmp_path / "good.csv"
        bad  = tmp_path / "bad.csv"
        write_hmrc_csv(good, ["USA,Dollar,USD,1.2643,01/01/2024,31/01/2024"])
        write_hmrc_csv(bad,  ["France,Euro,EUR,1.1500,01/01/2024,31/01/2024"])
        rc = combine.main(["combine.py", str(good), str(bad)])
        out, err = capsys.readouterr()
        assert rc == 1
        assert "1.2643" in out          # good file still in output
        assert "Error" in err           # bad file reported

    def test_no_args_prints_usage_to_stderr(self, combine, capsys):
        rc = combine.main(["combine.py"])
        assert rc != 0
        assert capsys.readouterr().err != ""
