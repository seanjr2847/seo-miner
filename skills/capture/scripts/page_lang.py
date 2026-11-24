#!/usr/bin/env python3
"""언어별 표 한 벌 — 페이지 점검(collect_page)이 글자를 볼 때 쓰는 언어마다 다른 규칙.

대상은 전 세계다(사이트 로케일 단위). 한 언어의 목록을 수집기·판정 여기저기에 적으면 다른
언어는 조용히 안 걸린다(영어 'click here' 만 잡고 'こちら' 는 통과). 그래서 언어마다 다른
것은 전부 이 파일에 표로 두고, 쓰는 쪽은 `html lang` 의 앞 두 글자로 고른다. 표에 없는
언어는 **판정하지 않는다** — 영어 규칙을 대신 대지 않는다.

여기 있는 것:
  GENERIC_ANCHORS   링크 글자만으로는 어디로 가는지 모르는 앵커('여기', 'click here')
  JUNK_ALT_WORDS    그림을 설명하지 않는 alt 낱말('image', '이미지')
  TRUST_PATHS       소개·연락처 페이지 주소 조각(E-E-A-T 신호 세기)
  NO_SPACE_LANGS    띄어쓰기로 낱말을 못 세는 언어(낱말 수 기준 판정을 안 한다)
  READABILITY       공식이 있는 언어의 가독성 공식(영어·독일어·스페인어)

stdlib 만 쓴다. 판정 문구는 여기 없다 — 세는 재료만 준다(문구는 scoring).
"""
import re
import unicodedata

# ── 언어 고르기 ─────────────────────────────────────────────────────────────


def lang_of(html_lang: str | None) -> str:
    """'ko-KR'·'EN_us'·' de ' → 'ko'·'en'·'de'. 못 읽으면 ''(= 언어 모름, 표를 안 댄다)."""
    v = (html_lang or "").strip().lower().replace("_", "-")
    head = v.split("-", 1)[0]
    return head if 2 <= len(head) <= 3 and head.isalpha() else ""


def fold(text: str) -> str:
    """견줄 때 쓰는 꼴 — NFKC, 소문자(casefold), 빈칸 하나로. 글자는 지우지 않는다(한글·CJK 그대로)."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


# ── 일반 앵커 — 글자만 보고는 어디로 가는지 모른다 ────────────────────────────
# 링크 글자 **전체**가 이 말일 때만 센다('여기에서 가격 보기'는 일반 앵커가 아니다).
# 끝의 화살표·점·느낌표는 떼고 견준다(_ANCHOR_TRIM).
GENERIC_ANCHORS = {
    "en": ("click here", "here", "read more", "more", "learn more", "see more", "this",
           "this page", "link", "details", "more info", "continue reading", "view more"),
    "ko": ("여기", "여기를 클릭", "여기 클릭", "클릭", "더 보기", "더보기", "자세히 보기", "자세히",
           "자세히 알아보기", "바로가기", "바로 가기", "링크", "보기", "이곳"),
    "ja": ("こちら", "ここ", "ここをクリック", "詳細", "詳細はこちら", "詳しくはこちら", "もっと見る",
           "続きを読む", "リンク", "クリック"),
    "zh": ("点击这里", "点击此处", "这里", "此处", "更多", "查看更多", "了解更多", "详情", "查看详情",
           "點擊這裡", "這裡", "了解更多", "閱讀更多", "链接"),
    "de": ("hier", "hier klicken", "klicken sie hier", "mehr", "mehr erfahren", "weiterlesen",
           "mehr lesen", "link", "details", "mehr infos"),
    "es": ("aquí", "aqui", "haz clic aquí", "haga clic aquí", "clic aquí", "leer más", "más",
           "ver más", "más información", "saber más", "enlace"),
    "fr": ("ici", "cliquez ici", "cliquer ici", "lire la suite", "en savoir plus", "plus",
           "voir plus", "lien", "détails"),
    "pt": ("aqui", "clique aqui", "saiba mais", "leia mais", "mais", "ver mais", "link",
           "mais informações"),
    "it": ("qui", "clicca qui", "leggi tutto", "scopri di più", "continua a leggere", "altro",
           "maggiori informazioni", "link"),
    "nl": ("hier", "klik hier", "lees meer", "meer", "meer informatie", "meer lezen", "link"),
    "ru": ("здесь", "тут", "нажмите здесь", "подробнее", "читать далее", "далее", "ещё", "ссылка"),
    "tr": ("buraya tıklayın", "burada", "buraya", "devamını oku", "daha fazla", "detaylar"),
    "id": ("klik di sini", "di sini", "disini", "selengkapnya", "baca selengkapnya", "lihat semua"),
    "vi": ("tại đây", "bấm vào đây", "nhấn vào đây", "xem thêm", "đọc thêm", "chi tiết"),
}
_ANCHOR_TRIM = " \t\r\n.!…»›→>:·-–—"


def is_generic_anchor(text: str, lang: str) -> bool | None:
    """링크 글자가 일반 앵커인가. 표에 없는 언어면 None(모름 — 세지 않는다)."""
    table = GENERIC_ANCHORS.get(lang)
    if table is None:
        return None
    return fold(text).strip(_ANCHOR_TRIM) in table


# ── 뜻 없는 alt ─────────────────────────────────────────────────────────────
# alt 가 있긴 한데 그림을 설명하지 않는다. 언어와 무관한 것(파일 이름·카메라 번호·숫자뿐)은
# 정규식으로, 언어마다 다른 것('image'·'이미지')은 표로. 표에 없는 언어는 무관한 쪽만 본다.
JUNK_ALT_WORDS = {
    "en": ("image", "img", "picture", "photo", "pic", "graphic", "icon", "logo", "banner",
           "untitled", "placeholder", "thumbnail", "screenshot", "alt", "alt text"),
    "ko": ("이미지", "사진", "그림", "아이콘", "로고", "배너", "썸네일", "무제", "대체 텍스트"),
    "ja": ("画像", "写真", "イメージ", "アイコン", "ロゴ", "バナー", "サムネイル", "無題"),
    "zh": ("图片", "图像", "照片", "图标", "圖片", "圖像", "未命名", "横幅"),
    "de": ("bild", "foto", "grafik", "symbol", "unbenannt", "abbildung"),
    "es": ("imagen", "foto", "fotografía", "icono", "gráfico", "sin título"),
    "fr": ("image", "photo", "icône", "graphique", "sans titre", "illustration"),
    "pt": ("imagem", "foto", "ícone", "gráfico", "sem título"),
    "it": ("immagine", "foto", "icona", "grafico", "senza titolo"),
    "nl": ("afbeelding", "foto", "plaatje", "icoon", "naamloos"),
    "ru": ("изображение", "картинка", "фото", "иконка", "без названия"),
    "tr": ("resim", "görsel", "fotoğraf", "simge", "adsız"),
    "id": ("gambar", "foto", "ikon", "tanpa judul"),
    "vi": ("hình ảnh", "ảnh", "hình", "biểu tượng"),
}
# 파일 이름(확장자로 끝남), 카메라·화면 캡처가 붙인 이름, 숫자·기호뿐
_ALT_FILE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|bmp|tiff?|heic)$", re.I)
_ALT_CAMERA = re.compile(r"^(img|dsc|dscn|dscf|pxl|mvimg|screenshot|screen shot|image|photo|pic)"
                         r"[\s_-]*\d+", re.I)
_ALT_NO_LETTERS = re.compile(r"^[\W\d_]+$")


def is_junk_alt(alt: str, lang: str = "") -> bool:
    """alt 가 그림을 설명하지 않는가(빈 alt 는 여기 것이 아니다 — 그건 'alt 없음'·장식이다).
    일반어 표는 그 언어 것 + 영어(파일·CMS 기본값은 어느 언어 사이트든 영어로 박힌다)."""
    t = fold(alt)
    if not t:
        return False
    if _ALT_FILE.search(t) or _ALT_CAMERA.match(t) or _ALT_NO_LETTERS.match(t):
        return True
    words = set(JUNK_ALT_WORDS["en"]) | set(JUNK_ALT_WORDS.get(lang, ()))
    return t.strip(_ANCHOR_TRIM) in words


# ── 소개·연락처 페이지 주소 조각 ─────────────────────────────────────────────
# 사이트 안 링크의 **경로 칸**이 이 말이면 그 페이지로 가는 링크다(/about, /회사소개/).
# 주소 조각은 영어로 쓰는 사이트가 많아 영어 표는 늘 같이 본다.
TRUST_PATHS = {
    "about": {
        "en": ("about", "about-us", "aboutus", "company", "who-we-are", "our-story", "team",
               "our-team"),
        "ko": ("회사소개", "소개", "병원소개", "회사-소개"),
        "ja": ("会社概要", "会社案内", "私たちについて", "企業情報"),
        "zh": ("关于我们", "關於我們", "公司简介", "关于"),
        "de": ("ueber-uns", "über-uns", "uber-uns", "unternehmen", "impressum"),
        "es": ("sobre-nosotros", "quienes-somos", "nosotros", "acerca-de", "empresa"),
        "fr": ("a-propos", "à-propos", "qui-sommes-nous", "notre-histoire", "mentions-legales"),
        "pt": ("sobre", "sobre-nos", "quem-somos", "empresa"),
        "it": ("chi-siamo", "azienda", "la-nostra-storia"),
        "nl": ("over-ons", "over", "bedrijf"),
        "ru": ("о-нас", "о-компании", "o-nas", "o-kompanii"),
        "tr": ("hakkimizda", "hakkımızda", "kurumsal"),
        "id": ("tentang-kami", "tentang"),
        "vi": ("gioi-thieu", "ve-chung-toi"),
    },
    "contact": {
        "en": ("contact", "contact-us", "contactus", "support", "help"),
        "ko": ("문의", "연락처", "오시는길", "오시는-길", "고객센터", "상담"),
        "ja": ("お問い合わせ", "問い合わせ", "アクセス", "連絡先"),
        "zh": ("联系我们", "聯絡我們", "联系", "联系方式"),
        "de": ("kontakt", "kontaktieren"),
        "es": ("contacto", "contactar", "contactenos"),
        "fr": ("contact", "nous-contacter", "contactez-nous"),
        "pt": ("contato", "contacto", "fale-conosco"),
        "it": ("contatti", "contattaci"),
        "nl": ("contact", "neem-contact-op"),
        "ru": ("контакты", "kontakty", "связаться"),
        "tr": ("iletisim", "iletişim"),
        "id": ("kontak", "hubungi-kami"),
        "vi": ("lien-he",),
    },
}


def trust_kind(path: str, lang: str = "") -> str | None:
    """링크 경로 → 'about'|'contact'|None. 경로의 칸 하나가 표의 말과 **같을** 때만(부분 일치 아님
    — /contact-lens-guide 는 연락처가 아니다)."""
    from urllib.parse import unquote
    segs = {fold(unquote(s)) for s in (path or "").split("/") if s}
    segs |= {s.rsplit(".", 1)[0] for s in segs if "." in s}          # about.html
    for kind, table in TRUST_PATHS.items():
        words = set(table["en"]) | set(table.get(lang, ()))
        if segs & words:
            return kind
    return None


# ── 낱말을 띄어쓰기로 못 세는 언어 ────────────────────────────────────────────
# 이 언어의 페이지는 '단어 수'가 문단 하나를 한 낱말로 센다. 낱말 수로 가르는 판정(긴 문단·
# 구간 첫 문단 길이)은 하지 않고 '못 잼'으로 둔다 — 글자 수 기준을 지어내지 않는다.
NO_SPACE_LANGS = frozenset({"ja", "zh", "th", "lo", "km", "my", "bo"})


# ── 가독성 — 공식이 있는 언어만 ───────────────────────────────────────────────
# 점수로 판정하지 않는다(정보 한 줄). 공식이 그 언어용으로 발표된 것만 싣는다. 나머지 언어는
# 문장 길이·긴 문단 수만 낸다 — 영어 공식을 다른 언어에 대지 않는다(영어 전용이라 뺐던 이유).
#   en  Flesch Reading Ease: 206.835 − 1.015·(낱말/문장) − 84.6·(음절/낱말)
#       https://en.wikipedia.org/wiki/Flesch%E2%80%93Kincaid_readability_tests
#   de  Amstad: 180 − (낱말/문장) − 58.5·(음절/낱말)
#       https://de.wikipedia.org/wiki/Lesbarkeitsindex
#   es  Fernández-Huerta: 206.84 − 60·(음절/낱말) − 1.02·(낱말/문장)
#       통상 표기는 206.84 − 0.60·P − 1.02·F(P=100낱말당 음절)다. F 의 뜻이 자료마다 갈린다
#       ('100낱말당 문장 수' / '문장당 낱말 수'). 앞의 것으로 읽으면 문장이 짧을수록 어렵다고
#       나와 Flesch 를 옮긴 공식이라는 취지와 어긋난다 — 그 불일치를 짚은 글이 G. Law,
#       LINGUIST List 22.2332(2011-06)이고, 널리 쓰는 구현은 '문장당 낱말 수'다. 여기도 그렇게
#       읽는다. 1959년 원논문은 확인하지 못했다(값은 참고용 정보로만 쓴다).
#       https://listserv.linguistlist.org/pipermail/linguist/2011-June/053303.html
# 음절 수는 **모음 묶음을 세는 근사**다(사전이 아니다). 그래서 값도 근사라고 말한다.
_VOWELS = {
    "en": "aeiouy",
    "de": "aeiouyäöü",
    "es": "aeiouáéíóúü",
}


def syllables(word: str, lang: str) -> int:
    """낱말의 음절 수 근사 — 모음 묶음 수(영어는 끝의 묵음 e 를 뺀다). 최소 1."""
    vowels = _VOWELS[lang]
    w = word.casefold()
    n, prev = 0, False
    for ch in w:
        v = ch in vowels
        n += v and not prev
        prev = v
    if lang == "en" and n > 1 and w.endswith("e") and not w.endswith(("le", "ee", "ie", "ye")):
        n -= 1
    return max(n, 1)


def _flesch(wps: float, spw: float) -> float:
    return 206.835 - 1.015 * wps - 84.6 * spw


def _amstad(wps: float, spw: float) -> float:
    return 180 - wps - 58.5 * spw


def _fernandez_huerta(wps: float, spw: float) -> float:
    return 206.84 - 60 * spw - 1.02 * wps


READABILITY = {
    "en": ("Flesch Reading Ease", _flesch),
    "de": ("Amstad", _amstad),
    "es": ("Fernández-Huerta", _fernandez_huerta),
}

_SENT_END = re.compile(r"[.!?。！？…]+(?:\s+|$)")
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
READ_MIN_WORDS = 100          # 이보다 짧은 글은 공식이 뜻이 없다(문장 몇 개에 값이 출렁인다)


def readability(text: str, lang: str) -> dict | None:
    """본문 글자 → {"lang", "sentences", "words", "wps"[, "formula", "score"]}. 못 재면 None.

    wps = 문장당 낱말 수. 공식이 있는 언어만 formula·score 가 붙는다(READABILITY).
    띄어쓰기로 낱말을 못 세는 언어(NO_SPACE_LANGS)와 언어를 모르는 글은 None 이다."""
    if not lang or lang in NO_SPACE_LANGS:
        return None
    words = _WORD.findall(text or "")
    sents = [s for s in _SENT_END.split(text or "") if _WORD.search(s)]
    if len(words) < READ_MIN_WORDS or not sents:
        return None
    wps = len(words) / len(sents)
    out = {"lang": lang, "sentences": len(sents), "words": len(words), "wps": round(wps, 1)}
    if lang in READABILITY:
        name, fn = READABILITY[lang]
        spw = sum(syllables(w, lang) for w in words) / len(words)
        out.update(formula=name, score=round(fn(wps, spw), 1))
    return out


# ── 자리 채움 글 ─────────────────────────────────────────────────────────────
# 조판용 라틴어 채움 글 — 어느 언어 사이트든 템플릿에서 그대로 남는다(언어 무관).
LOREM = re.compile(r"lorem\s+ipsum(?:\s+dolor)?", re.I)


def _selfcheck() -> None:
    assert lang_of("ko-KR") == "ko" and lang_of("EN_us") == "en" and lang_of("") == ""
    assert lang_of("x") == "" and lang_of("zh-Hant-TW") == "zh"
    # 일반 앵커 — 글자 전체가 그 말일 때만, 끝 화살표는 뗀다. 표에 없는 언어는 모름
    assert is_generic_anchor("Click here »", "en") and is_generic_anchor("자세히 보기 →", "ko")
    assert is_generic_anchor("詳細はこちら", "ja") and is_generic_anchor("Mehr erfahren", "de")
    assert is_generic_anchor("여기에서 가격 보기", "ko") is False
    assert is_generic_anchor("click here", "sv") is None
    # 뜻 없는 alt — 파일 이름·카메라 번호는 언어 무관, 일반어는 그 언어 + 영어
    for junk in ("IMG_1234", "hero-banner.png", "DSC00012.JPG", "123", "image", "Screenshot 2026"):
        assert is_junk_alt(junk), junk
    assert is_junk_alt("이미지", "ko") and is_junk_alt("画像", "ja") and is_junk_alt("Bild", "de")
    assert not is_junk_alt("이미지", "en"), "다른 언어 표를 댄다"
    assert not is_junk_alt("밀리아 제거 전후 사진 비교", "ko") and not is_junk_alt("")
    assert not is_junk_alt("Image of a red bicycle", "en")
    # 소개·연락처 — 경로 칸이 같을 때만
    assert trust_kind("/about-us/", "en") == "about" and trust_kind("/ko/회사소개", "ko") == "about"
    assert trust_kind("/kontakt.html", "de") == "contact"
    assert trust_kind("/contact-lens-guide", "en") is None and trust_kind("/", "en") is None
    # 가독성 — 공식 있는 언어만 점수, 나머지는 문장 길이만, 못 세는 언어는 None
    en = "The cat sat on the mat. It was a sunny day and the bird sang. " * 12
    r = readability(en, "en")
    assert r and r["formula"] == "Flesch Reading Ease" and r["score"] > 80, r
    assert abs(r["wps"] - 7.5) < 0.01, r                                # 15 낱말 / 2 문장
    hard = ("Institutional heterogeneity necessitates comprehensive organizational "
            "reconsideration regarding administrative responsibilities. ") * 12
    assert readability(hard, "en")["score"] < 0 < r["score"], "어려운 글이 더 쉽게 나온다"
    ko = readability("이 글은 밀리아를 설명합니다. 원인과 치료를 다룹니다. " * 20, "ko")
    assert ko and "score" not in ko and ko["sentences"] == 40, ko
    assert readability("これは日本語の文章です。" * 50, "ja") is None
    assert readability(en, "") is None and readability("짧은 글.", "ko") is None
    # 공식 값 — 손으로 푼 것과 같다(낱말/문장 10, 음절/낱말 1.5)
    assert abs(_flesch(10, 1.5) - 69.785) < 1e-9
    assert abs(_amstad(10, 1.5) - 82.25) < 1e-9
    assert abs(_fernandez_huerta(10, 1.5) - 106.64) < 1e-9
    assert _fernandez_huerta(30, 1.5) < _fernandez_huerta(10, 1.5), "긴 문장이 더 쉽게 나온다"
    assert syllables("make", "en") == 1 and syllables("table", "en") == 2
    assert syllables("Straße", "de") == 2 and syllables("camión", "es") == 2
    assert LOREM.search("Lorem  ipsum dolor sit amet") and not LOREM.search("lore ipsum")
    # 표의 언어 키는 전부 같은 꼴(소문자 두 글자)이고, 영어는 모든 표에 있다
    for table in (GENERIC_ANCHORS, JUNK_ALT_WORDS, *TRUST_PATHS.values()):
        assert "en" in table and all(k == lang_of(k) for k in table), table.keys()
    print("page_lang self-check ok")


if __name__ == "__main__":
    _selfcheck()
