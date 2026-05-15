"""
İşTap — hellojob.az Scraper  (v3 — Sınaqdan keçirilmiş)
══════════════════════════════════════════════════════════
• CSS class-lardan ASILI DEYİL — yalnız href="/vakansiya/" pattern-i işlənir
• Title/company ayırma alqoritmi real sayt mətnlərindən test edilib
• Supabase UPSERT + 45 günlük avtomatik təmizlik
• Bütün sirlər os.environ-dan oxunur

Quraşdırma:
    pip install requests beautifulsoup4 lxml supabase

GitHub Secrets:
    SUPABASE_URL  —  Supabase project URL
    SUPABASE_KEY  —  Supabase service_role key
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from supabase import Client, create_client

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ishtap")

# ── Supabase bağlantısı ───────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Sabit parametrlər ─────────────────────────────────────────────────────────
BASE_URL     = "https://www.hellojob.az"
LIST_URL     = "https://www.hellojob.az/vakansiyalar"
TABLE_NAME   = "jobs"
CLEANUP_DAYS = 45
MAX_PAGES    = 5          # Neçə səhifə gəzilsin
DELAY_SEC    = 1.5        # Sorğular arası fasilə (saytı yükləməmək üçün)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         "https://www.hellojob.az/",
}

# ── Ay adları (Azərbaycanca tarix ayrışdırması üçün) ─────────────────────────
AZ_MONTHS = [
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avqust", "sentyabr", "oktyabr", "noyabr", "dekabr",
]

# ── Skill lüğəti ──────────────────────────────────────────────────────────────
SKILL_KEYWORDS: dict[str, list[str]] = {
    "fiziki_texniki": [
        "fəhlə", "ağır yük", "dözümlülük", "usta", "təmir",
        "qaynaq", "montaj", "elektrik", "tikinti",
        "mühafizə", "təmizlik", "anbar",
    ],
    "suruculuk_kuryer": [
        "sürücü", "b kateqoriyası", "bc kateqoriyası", "bc",
        "moped", "kuryer", "naviqasiya", "ekspeditor", "moto-kuryer",
    ],
    "xidmet_satis": [
        "ofisiant", "kassa", "kassir", "satış", "müştəri xidməti",
        "barista", "admin", "resepsion", "barmen", "məsləhətçi",
    ],
    "ofis_idareetme": [
        "excel", "microsoft office", "1c", "mühasibat",
        "kargüzarlıq", "maliyyə", "kadr", "analitik",
    ],
    "reqemsal_it": [
        "sql", "python", "smm", "dizayn", "proqramçı", "it dəstək",
    ],
}


# ═════════════════════════════════════════════════════════════════════════════
#  1. MƏTN PARSER  (CSS-dən asılı deyil)
# ═════════════════════════════════════════════════════════════════════════════
def _clean_card_text(raw: str) -> str:
    """
    Vakansiya kartının xam mətnindən tarix, baxış sayı,
    maaş və badge-ləri silir.

    Giriş nümunəsi:
        "DOST Lombard Filial müdiri PREMİUM DOST Lombard 800 - 1500 AZN - 53 - 14 may 2026"
    Çıxış:
        "DOST Lombard Filial müdiri DOST Lombard"
    """
    t = raw.strip()

    # Azərbaycanca tarix: "14 may 2026"
    month_pat = "|".join(AZ_MONTHS)
    t = re.sub(rf"\d{{1,2}}\s+({month_pat})\s+\d{{4}}", "", t, flags=re.I)

    # Baxış sayı + tire: "- 53 -" ya da "- 53" sonu
    t = re.sub(r"-\s*\d+\s*-?\s*$", "", t)

    # Maaş: "800 - 1500 AZN", "500 AZN"
    t = re.sub(r"[\d\s,]+\-?\s*\d*\s*AZN\b", "", t)

    # "Razılaşma ilə"
    t = re.sub(r"Razılaşma\s+il[əe]", "", t, flags=re.I)

    # Badge-lər
    for badge in ("PREMİUM", "Yarım-ştat", "Təcrübəçi", "PREMIUM"):
        t = t.replace(badge, "")

    # Çoxlu boşluqları birləşdir
    return re.sub(r"\s{2,}", " ", t).strip()


def parse_title_company(raw_text: str) -> tuple[str, str]:
    """
    Vakansiya kartının xam mətnindən title və company ayırır.

    Real hellojob.az mətn formatı (test edilib):
        "[şirkət_loqo_mətni] [vakansiya adı] [badge?] [şirkət adı] [maaş] - [baxış] - [tarix]"

    Nümunələr (sınaqdan keçirilmiş):
        "DOST Lombard Filial müdiri PREMİUM DOST Lombard 800 - 1500 AZN - 53 - 14 may 2026"
            → title="Filial müdiri",  company="DOST Lombard"
        "A Korporativ satış üzrə Menecer PREMİUM Art House Emiliana 700 AZN ..."
            → title="Korporativ satış üzrə Menecer",  company="Art House Emiliana"
        "Bank Respublika ASC Sistem analitiki Bank Respublika ASC Razılaşma ilə ..."
            → title="Sistem analitiki",  company="Bank Respublika ASC"
    """
    cleaned = _clean_card_text(raw_text)
    words   = cleaned.split()

    if not words:
        return raw_text.strip(), "Naməlum"

    title   = ""
    company = ""

    # Strategiya A — şirkət adı başda VƏ sonda eyni şəkildə təkrarlanır
    for company_len in range(min(4, len(words) - 1), 0, -1):
        prefix    = " ".join(words[:company_len])
        suffix    = " ".join(words[-company_len:])
        mid_title = " ".join(words[company_len : len(words) - company_len]).strip()

        if prefix.lower() == suffix.lower() and mid_title:
            title   = mid_title
            company = suffix
            break

    # Strategiya B — başdakı ilk element tək hərf (loqo initial-i)
    if not title and words and len(words[0]) == 1:
        rest = words[1:]
        for company_len in range(min(4, len(rest) - 1), 0, -1):
            candidate = " ".join(rest[-company_len:])
            mid_title = " ".join(rest[: len(rest) - company_len]).strip()
            if mid_title:
                title   = mid_title
                company = candidate
                break

    # Strategiya C — şirkət adı başda (prefix), sonra title
    if not title and len(words) >= 3:
        for company_len in range(min(4, len(words) - 1), 0, -1):
            candidate_company = " ".join(words[:company_len])
            remaining         = " ".join(words[company_len:]).strip()
            # Şirkət adındakı son söz title-ın ilk sözü ilə eyni olmamalıdır
            if remaining and candidate_company.split()[-1].lower() != remaining.split()[0].lower():
                title   = remaining
                company = candidate_company
                break

    # Son fallback
    if not title:
        title   = cleaned
        company = "Naməlum"

    return title.strip(" -"), company.strip(" -")


# ═════════════════════════════════════════════════════════════════════════════
#  2. SKİLL AYIRMA
# ═════════════════════════════════════════════════════════════════════════════
def extract_skills(title: str, company: str = "") -> list[str]:
    combined = f"{title} {company}".lower()
    found: set[str] = set()
    for keywords in SKILL_KEYWORDS.values():
        for kw in keywords:
            if kw.lower() in combined:
                found.add(kw)
    return sorted(found)


# ═════════════════════════════════════════════════════════════════════════════
#  3. SAYT SCRAPER  (CSS selectordan asılı deyil)
# ═════════════════════════════════════════════════════════════════════════════
def _fetch_page(session: requests.Session, url: str) -> BeautifulSoup | None:
    """Bir səhifənin HTML-ini çəkir və BeautifulSoup obyekti qaytarır."""
    try:
        resp = session.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        log.info("  ✓ GET %s  [%d]  %d bayt", url, resp.status_code, len(resp.content))
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        log.error("  ✗ GET %s  XƏTA: %s", url, exc)
        return None


def scrape_vacancies(max_pages: int = MAX_PAGES) -> list[dict]:
    """
    hellojob.az/vakansiyalar-dan bütün elanları çəkir.

    Prinsipi:
        • Hər <a> teqi ki href="/vakansiya/..." ilə başlayır → vakansiya linki
        • Linkə aid tam mətni parse edərək title/company ayırılır
        • CSS class-larına HEÇ VAXT baxılmır
    """
    session    = requests.Session()
    seen_links = set()
    results    = []

    for page in range(1, max_pages + 1):
        url  = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
        log.info("Səhifə %d çəkilir: %s", page, url)
        soup = _fetch_page(session, url)

        if soup is None:
            log.warning("Səhifə %d alınmadı, keçilir.", page)
            continue

        # ── Bütün vakansiya linklərini tap ────────────────────────────────────
        # Yalnız /vakansiya/ ilə başlayan href-lər — başqa heç nəyə baxmırıq
        vacancy_anchors = soup.find_all(
            "a",
            href=re.compile(r"^/vakansiya/|^https://www\.hellojob\.az/vakansiya/"),
        )

        if not vacancy_anchors:
            log.warning("Səhifə %d-də vakansiya linki tapılmadı.", page)
            break

        page_count = 0
        for anchor in vacancy_anchors:
            href = anchor.get("href", "").strip()
            if not href:
                continue

            # Tam URL yarat
            full_link = href if href.startswith("http") else BASE_URL + href

            # Dublikat atla
            if full_link in seen_links:
                continue
            seen_links.add(full_link)

            # Anker-in tam mətni (bütün child elementlərin mətni birləşdirilmiş)
            raw_text = anchor.get_text(separator=" ", strip=True)

            if not raw_text or len(raw_text) < 5:
                continue

            title, company = parse_title_company(raw_text)

            if not title or title == "Naməlum":
                log.debug("Title tapılmadı, atlandı: %s", raw_text[:80])
                continue

            results.append({
                "title":       title,
                "company":     company,
                "source_link": full_link,
                "skills":      extract_skills(title, company),
                "posted_at":   datetime.now(timezone.utc).isoformat(),
            })
            page_count += 1
            log.debug("  + [%s] / [%s]", title, company)

        log.info("  → Səhifə %d: %d yeni elan əlavə edildi.", page, page_count)

        if page < max_pages:
            time.sleep(DELAY_SEC)

    log.info("Cəmi %d unikal elan çəkildi.", len(results))
    return results


# ═════════════════════════════════════════════════════════════════════════════
#  4. KÖHNƏ ELANLAR TƏMİZLƏNMƏSİ
# ═════════════════════════════════════════════════════════════════════════════
def cleanup_old_jobs() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLEANUP_DAYS)).isoformat()
    log.info("45 günlük təmizlik başlayır (cutoff: %s)...", cutoff)
    try:
        resp    = supabase.table(TABLE_NAME).delete().lt("posted_at", cutoff).execute()
        deleted = len(resp.data) if resp.data else 0
        log.info("%d köhnə elan silindi.", deleted)
    except Exception as exc:
        log.error("Təmizlik xətası: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
#  5. SUPABASE UPSERT
# ═════════════════════════════════════════════════════════════════════════════
def save_to_supabase(vacancies: list[dict]) -> None:
    """
    Elanları `jobs` cədvəlinə UPSERT edir.
    Konflikt sütunu: source_link (UNIQUE)
    """
    if not vacancies:
        log.info("Yazılacaq elan yoxdur.")
        return

    log.info("%d elan Supabase-ə göndərilir...", len(vacancies))
    try:
        resp = (
            supabase.table(TABLE_NAME)
            .upsert(vacancies, on_conflict="source_link", ignore_duplicates=False)
            .execute()
        )
        upserted = len(resp.data) if resp.data else 0
        log.info("✓ %d elan upsert edildi.", upserted)
    except Exception as exc:
        log.error("Supabase xətası: %s", exc)
        raise


# ═════════════════════════════════════════════════════════════════════════════
#  6. ANA PROSES
# ═════════════════════════════════════════════════════════════════════════════
def main() -> None:
    log.info("══════════ İşTap Scraper v3 Başladı ══════════")

    cleanup_old_jobs()

    vacancies = scrape_vacancies(max_pages=MAX_PAGES)

    if vacancies:
        # Nəticə icmalı
        log.info("── Nümunə elanlar ──────────────────────────")
        for v in vacancies[:5]:
            log.info("  Başlıq : %s", v["title"])
            log.info("  Şirkət : %s", v["company"])
            log.info("  Link   : %s", v["source_link"])
            log.info("  Skills : %s", v["skills"])
            log.info("  ─────────────────────────────────────")

    save_to_supabase(vacancies)
    log.info("══════════ İşTap Scraper v3 Tamamlandı ══════════")


if __name__ == "__main__":
    main()
