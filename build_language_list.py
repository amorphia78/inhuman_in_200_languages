"""Build the language list (the WALS 200-language sample) from the WALS CLDF dataset.

Usage:
    python build_language_list.py            # use the saved copies in data/raw/
    python build_language_list.py --download # re-download them from GitHub first

Inputs (from https://github.com/cldf-datasets/wals, release v2020.6, folder cldf/):
    data/raw/wals_v2020.6_languages.csv
        Sample membership is recorded in the boolean columns Samples_100 and
        Samples_200. The 200 codes flagged there were checked (2 Oct 2026) against
        https://wals.info/languoid/samples/200 and match exactly.
    data/raw/wals_v2020.6_countries.csv
        Country names for the Country_ID codes in languages.csv.

Output:
    data/languages_wals200.csv  one row per language, sorted by WALS name.

The prompt_identifier column is the text shown to the model in brackets after the
language name, built from WALS genus, family and countries only, e.g.
    genus: Athapaskan; family: Na-Dene; spoken in: Canada
Genus is omitted when it equals the family; WALS's placeholder family "other" is omitted.

English, German, Mandarin and Russian (all in the sample) are flagged in is_control /
control_role as languages whose answers can be checked against known vocabulary.
"""

import argparse
import csv
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw" / "wals_v2020.6_languages.csv"
RAW_COUNTRIES = ROOT / "data" / "raw" / "wals_v2020.6_countries.csv"
URL = "https://raw.githubusercontent.com/cldf-datasets/wals/v2020.6/cldf/{table}.csv"
OUT = ROOT / "data" / "languages_wals200.csv"

COLUMNS = [
    "wals_code", "name", "family", "genus", "macroarea", "countries", "prompt_identifier",
    "wals_sample_100", "wals_sample_200",
    "is_control", "control_role", "inclusion_reason",
]

KNOWN_CONTROLS = {"eng", "ger", "mnd", "rus"}


def identifier(genus, family, countries):
    parts = []
    if genus and genus != family:
        parts.append(f"genus: {genus}")
    if family and family != "other":
        parts.append(f"family: {family}")
    parts.append(f"spoken in: {countries}")
    return "; ".join(parts)


def to_row(w, country_names):
    code = w["ID"]
    countries = ", ".join(country_names[c] for c in w["Country_ID"].split())
    return {
        "wals_code": code,
        "name": w["Name"],
        "family": w["Family"],
        "genus": w["Genus"],
        "macroarea": w["Macroarea"],
        "countries": countries,
        "prompt_identifier": identifier(w["Genus"], w["Family"], countries),
        "wals_sample_100": w["Samples_100"],
        "wals_sample_200": w["Samples_200"],
        "is_control": "true" if code in KNOWN_CONTROLS else "false",
        "control_role": "known_language" if code in KNOWN_CONTROLS else "",
        "inclusion_reason": "WALS 200-language sample",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download", action="store_true", help="re-download the WALS files from GitHub")
    args = parser.parse_args()

    if args.download:
        RAW.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL.format(table="languages"), RAW)
        urllib.request.urlretrieve(URL.format(table="countries"), RAW_COUNTRIES)
    for path in (RAW, RAW_COUNTRIES):
        print(f"WALS file: {path.relative_to(ROOT)}  sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")

    with open(RAW, encoding="utf-8", newline="") as f:
        wals = list(csv.DictReader(f))
    with open(RAW_COUNTRIES, encoding="utf-8", newline="") as f:
        country_names = {r["ID"]: r["Name"] for r in csv.DictReader(f)}

    sample = sorted((w for w in wals if w["Samples_200"] == "true"), key=lambda w: w["Name"].casefold())
    if len(sample) != 200:
        sys.exit(f"Expected 200 languages flagged Samples_200, found {len(sample)}")

    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(to_row(w, country_names) for w in sample)
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(sample)} languages")


if __name__ == "__main__":
    main()
