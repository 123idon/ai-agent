# KRX 샘플 조회 테스트 (pykrx) — 005930, 한 기간. 공매도 + 투자자 순매수.
# 로그인 필요 여부 확인용. 비밀번호 값은 절대 출력하지 않는다. (임시 스크립트)
import os, re, sys, pathlib
sys.stdout.reconfigure(encoding="utf-8")

USE_LOGIN = "--login" in sys.argv

# config/kis_api.yaml 에서 krx_id/krx_pw 읽기 (값은 출력 금지, 존재 여부만)
_raw = pathlib.Path("config/kis_api.yaml").read_bytes()
if _raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
    cfg = _raw.decode("utf-16"); _enc = "utf-16(BOM)"
elif _raw[:3] == b"\xef\xbb\xbf":
    cfg = _raw.decode("utf-8-sig"); _enc = "utf-8(BOM)"
else:
    cfg = _raw.decode("utf-8"); _enc = "utf-8"
print(f"[file] config/kis_api.yaml encoding={_enc}")
def grab(key):
    m = re.search(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$", cfg, re.M)
    if not m:
        return None
    v = m.group(1).rstrip()
    if v and v[0] in "'\"":               # 따옴표로 감싼 값: 닫는 따옴표까지 (비번 내 # 안전)
        q = v[0]; j = v.find(q, 1)
        return v[1:j] if j > 0 else v[1:]
    return (v.split("#")[0].strip() or None)
kid, kpw = grab("krx_id"), grab("krx_pw")
print(f"[creds] yaml krx_id 존재={bool(kid)} · krx_pw 존재={bool(kpw)} (값 미출력)")

if USE_LOGIN:
    if kid: os.environ["KRX_ID"] = kid
    if kpw: os.environ["KRX_PW"] = kpw
    print("[mode] LOGIN — KRX_ID/KRX_PW env 설정 후 pykrx import")
else:
    os.environ.pop("KRX_ID", None); os.environ.pop("KRX_PW", None)
    print("[mode] PUBLIC — 로그인 없이 pykrx import")

from pykrx import stock
import pandas as pd
pd.set_option("display.width", 200); pd.set_option("display.max_columns", 30)

TICKER, FROM, TO = "005930", "20260601", "20260617"
print(f"[query] ticker={TICKER} {FROM}~{TO}\n")

def show(name, fn):
    try:
        df = fn()
        print(f"=== {name} ===")
        if df is None or len(df) == 0:
            print("  (빈 결과)\n"); return
        print(f"  shape={df.shape} · columns={list(df.columns)}")
        print(df.tail(3).to_string(), "\n")
    except Exception as e:
        print(f"=== {name} === ERROR {type(e).__name__}: {e}\n")

show("공매도 거래량(거래량/비중)", lambda: stock.get_shorting_volume_by_date(FROM, TO, TICKER))
show("공매도 잔고(잔고/금액/비중)", lambda: stock.get_shorting_balance_by_date(FROM, TO, TICKER))
show("투자자별 순매수 거래대금(외인/기관 등)", lambda: stock.get_market_trading_value_by_date(FROM, TO, TICKER))
print("[done]")
