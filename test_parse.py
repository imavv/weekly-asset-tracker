"""Quick test: run parse + validate on saved raw output without calling Claude."""
import sys
sys.path.insert(0, ".")
from portfolio_tracker import parse_table, validate_rows, preview_table, apply_holdings

START_ROW = 1632

RAW = """2026-06-16\tCash\tMandiri\t34897289\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t=GOOGLEFINANCE("CURRENCY:USDIDR")
2026-06-16\tCash\tBCA\t91260296\t\t\t\t\t\t\t
2026-06-16\tCash\tSeabank\t82398657\t\t\t\t\t\t\t
2026-06-16\tCash\tOthers\t404993\t\t\t\t\t\t\t
2026-06-16\tCash\tSuperbank\t3405620\t\t\t\t\t\t\t
2026-06-16\tDeposit\tSuperbank Deposit\t106691335\t\t\t\t\t\t\t
2026-06-16\tMF Bonds\tBibit\t44898593\t\t\t\t\t\t\t
2026-06-16\tCash\tBNI (RDN)\t0\t\t\t\t\t\t\t
2026-06-16\tStock\tBBCA\t=F1640*G1640\t\t0\t0\t\t=(G1640-H1640)/H1640\t=(G1640-H1640)*F1640\t
2026-06-16\tStock\tICBP\t=F1641*G1641\t\t0\t0\t\t=(G1641-H1641)/H1641\t=(G1641-H1641)*F1641\t
2026-06-16\tStock\tBBRI\t=F1642*G1642\t\t0\t0\t\t=(G1642-H1642)/H1642\t=(G1642-H1642)*F1642\t
2026-06-16\tCash\tAjaib\t=0*$K$1632\t\t\t\t\t\t\t
2026-06-16\tETF\tVOO\t=F1644*G1644*$K$1632\t\t0\t693.83\t\t=(G1644-H1644)/H1644\t=(G1644-H1644)*F1644\t
2026-06-16\tETF\tVT\t=F1645*G1645*$K$1632\t\t0\t158.76\t\t=(G1645-H1645)/H1645\t=(G1645-H1645)*F1645\t
2026-06-16\tETF\tVTI\t=F1646*G1646*$K$1632\t\t0\t372.53\t\t=(G1646-H1646)/H1646\t=(G1646-H1646)*F1646\t
2026-06-16\tETF\tSPYM\t=F1647*G1647*$K$1632\t\t0\t88.25\t\t=(G1647-H1647)/H1647\t=(G1647-H1647)*F1647\t
2026-06-16\tETF\tGDX\t=F1648*G1648*$K$1632\t\t0\t86.64\t\t=(G1648-H1648)/H1648\t=(G1648-H1648)*F1648\t
2026-06-16\tETF\tVEA\t=F1649*G1649*$K$1632\t\t0\t72.42\t\t=(G1649-H1649)/H1649\t=(G1649-H1649)*F1649\t
2026-06-16\tETF\tSMH\t=F1650*G1650*$K$1632\t\t0\t619.96\t\t=(G1650-H1650)/H1650\t=(G1650-H1650)*F1650\t
2026-06-16\tETF\tGLD\t=F1651*G1651*$K$1632\t\t0\t386.54\t\t=(G1651-H1651)/H1651\t=(G1651-H1651)*F1651\t
2026-06-16\tETF\tIGV\t=F1652*G1652*$K$1632\t\t0\t90.70\t\t=(G1652-H1652)/H1652\t=(G1652-H1652)*F1652\t
2026-06-16\tETF\tXLP\t=F1653*G1653*$K$1632\t\t0\t83.25\t\t=(G1653-H1653)/H1653\t=(G1653-H1653)*F1653\t
2026-06-16\tETF\tXLE\t=F1654*G1654*$K$1632\t\t0\t56.19\t\t=(G1654-H1654)/H1654\t=(G1654-H1654)*F1654\t"""

rows = parse_table(RAW, START_ROW)
rows = apply_holdings(rows)
validate_rows(rows, START_ROW)
preview_table(rows, START_ROW)
print("[✓] Parse + validate + holdings injection passed — writing to Sheets...")
from portfolio_tracker import post_to_sheets
result = post_to_sheets(rows, START_ROW)
if result.get("status") == 200:
    print("[✓] Done. Check your sheet.")
else:
    print(f"[✗] Write failed: {result}")
