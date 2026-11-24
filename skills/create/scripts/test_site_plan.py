#!/usr/bin/env python3
"""신규 사이트 계획 요청문 자체점검 — `python test_site_plan.py` (임시 폴더, 네트워크 0).

지키는 것: 측정 없는 글에 숫자가 없다, 종류·로케일·유형·단계가 전부 정본에서 온다(사본 없음),
이미 측정한 사이트에는 내지 않는다.
"""
import io
import os
import re
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-siteplan-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.resolve().parents[1] / "capture" / "scripts"))
import dashboard  # noqa: E402
import db  # noqa: E402
import run_all  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402
import site_plan  # noqa: E402


def cli(*args) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = site_plan.main(list(args))
    return rc, out.getvalue(), err.getvalue()


# ── 모든 종류 × 로케일 표본: 숫자가 없다 ──────────────────────────────────
# 로케일은 표 전체를 돈다 — 한 나라 기준으로 짠 글이면 여기서 드러난다.
for type_id in dashboard.PROJECT_TYPE_IDS:
    for locale, label in serp_adapter.LOCALES:
        body = site_plan.build("site.example", type_id, locale)
        rest = body.replace(f"({locale})", "")              # 입력(로케일 코드)과
        for name in run_all.VALID_STAGE_NAMES:               # 단계 id(ga4)는 값이 아니라 이름이다
            rest = re.sub(r"\b" + name + r"\b", "", rest)
        assert not re.search(r"\d", rest), (type_id, locale, re.findall(r".{0,20}\d.{0,20}", rest)[:3])
        assert "%" not in body
        assert label in body and dict(dashboard.PROJECT_TYPES)[type_id] in body
        assert site_plan.NO_NUMBER_RULE in body and site_plan.UNKNOWN in body
        for head in ("## 한눈에", "## 규칙", "## 권장 URL 트리 뼈대", "## 첫 측정 계획", "## 만들어 줄 것",
                     "## 답의 형식"):
            assert body.count(head) == 1, (type_id, head)
# 같은 입력이면 같은 글.
assert site_plan.build("a.com", "saas", "en-US") == site_plan.build("a.com", "saas", "en-US")
# 언어는 로케일에서 온다 — 한국어로 고정돼 있지 않다.
assert "일본어로 짓습니다" in site_plan.build("a.jp", "commerce", "ja-JP")
assert "독일어로 짓습니다" in site_plan.build("a.de", "commerce", "de-DE")

# ── 정본에서 온다 ─────────────────────────────────────────────────────────
# 종류: PROJECT_TYPES 와 뼈대 표가 같은 집합(모듈 assert 가 지키고, 여기서 한 번 더 말로).
assert set(site_plan.TREES) == set(dashboard.PROJECT_TYPE_IDS)
# 단계: 글에 나온 단계 이름은 전부 run_all 의 것이고, 잴 수 있는 묶음은 빠짐없이 나온다.
lines = site_plan.measure_lines()
named = set(re.findall(r"\b[a-z0-9_]+\b", " ".join(lines)))
assert named <= set(run_all.VALID_STAGE_NAMES), named - set(run_all.VALID_STAGE_NAMES)
with_stages = [g for g in run_all.GROUPS if g["stages"]]
assert len(lines) == len(with_stages) and all(g["name"] in ln for g, ln in zip(with_stages, lines))
assert named == {s for g in with_stages for s in g["stages"]}
# 유료·무료는 STAGES 의 is_paid 를 따른다.
for g, ln in zip(with_stages, lines):
    for s in g["stages"]:
        paid_part = ln.split("유료 키가 있어야 도는 단계")[1] if "유료 키" in ln else ""
        assert (s in re.findall(r"[a-z0-9_]+", paid_part)) == run_all.STAGE_BY_NAME[s].is_paid, (g["id"], s)
# 유형: 속성은 SCHEMA_RULES 에서 읽은 그대로다.
body = site_plan.build("a.com", "commerce", "en-US")
assert "- Product: 필수 " + ", ".join(scoring.schema_props("Product", "required")) in body
assert "FAQPage" not in "".join(site_plan.build("a.com", t, "en-US") for t in site_plan.TREES)
# 지역 비즈니스의 홈 주인은 지점 유형이다.
assert "- 홈 | 누구의 사이트이고 무엇을 하는 곳인가 | WebSite + LocalBusiness" in \
    site_plan.build("a.com", "local_business", "en-US")
assert "WebSite + Organization" in site_plan.build("a.com", "saas", "en-US")

# ── 입력 검증 ─────────────────────────────────────────────────────────────
for bad in (("a.com", "blog", "en-US"), ("a.com", "saas", "xx-XX")):
    try:
        site_plan.build(*bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"모르는 값인데 글을 냈다: {bad}")
rc, out, err = cli("--domain", "a.com", "--type", "saas")
assert rc == 1 and not out and "--locale" in err, (rc, err)
rc, out, err = cli("--domain", "a.com", "--type", "saas", "--locale", "en-US")
assert rc == 0 and out.startswith("아래 새 사이트의 구조 계획서"), (rc, err)

# ── 등록한 사이트: Brain 에서 읽고, 측정이 있으면 내지 않는다 ────────────────────
conn = db.connect()
conn.execute("INSERT INTO projects(name, type, domain, locale) VALUES('new','commerce','new.fr','fr-FR')")
conn.commit()
pid = conn.execute("SELECT id FROM projects WHERE name='new'").fetchone()[0]
rc, out, err = cli("new")
assert rc == 0 and "new.fr" in out and "프랑스어" in out and "(commerce)" in out, (rc, out[:200], err)
# 이미지 생성 같은 create 쪽 장부 줄은 측정이 아니다.
db.finish_run(conn, db.start_run(conn, pid, "create_image"), api_calls=1, cost=0.03)
assert cli("new")[0] == 0
db.finish_run(conn, db.start_run(conn, pid, "gsc"), api_calls=1)
conn.close()
rc, out, err = cli("new")
assert rc == 1 and not out and "/create plan" in err, (rc, out[:80], err)
assert cli("new", "--force")[0] == 0
rc, out, err = cli("없는사이트")
assert rc == 1 and not out, (rc, out[:80])

print(f"ok — 신규 사이트 계획 요청문: 숫자 없음, 종류·로케일·유형·단계는 정본에서, 측정한 사이트엔 안 냄 ({HOME})")
