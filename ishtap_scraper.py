"""
İşTap — hellojob.az Scraper Botu
Mənbə : hellojob.az → Son vakansiyalar
Xüsusiyyətlər:
  • Skill extraction (5 kateqoriya)
  • 45 günlük avtomatik təmizlik
  • Supabase upsert (source_link UNIQUE)
  • Sirlər yalnız environment variable-lardan oxunur
"""

import os
import logging
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ishtap")

# ── Supabase bağlantısı ───────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]   # GitHub Secret / .env
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]   # GitHub Secret / .env

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Skill açar sözləri (genişləndirilə bilər) ─────────────────────────────────
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

# ── Sabit parametrlər ─────────────────────────────────────────────────────────
BASE_URL        = "https://hellojob.az"
VACANCIES_PATH  = "/vacancies"          # "Son vakansiyalar" bölməsi
HEADERS         = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
CLEANUP_DAYS    = 45                    # Neçə gündən köhnə elanlar silinsin
TABLE_NAME      = "jobs"


# ═════════════════════════════════════════════════════════════════════════════
#  1. KÖHNƏ ELANLAR TƏMİZLƏNMƏSİ
# ═════════════════════════════════════════════════════════════════════════════
def cleanup_old_jobs() -> None:
    """
    `jobs` cədvəlindəki posted_at > 45 gün olan elanları silir.
    posted_at sütunu ISO-8601 formatında saxlanılmalıdır.
    """
    cutoff: str = (
        datetime.now(timezone.utc) - timedelta(days=CLEANUP_DAYS)
    ).isoformat()

    log.info("Köhnə elanlar silinir (cutoff: %s) ...", cutoff)
    response = (
        supabase.table(TABLE_NAME)
        .delete()
        .lt("posted_at", cutoff)   # posted_at < cutoff
        .execute()
    )

    deleted_count = len(response.data) if response.data else 0
    log.info("%d köhnə elan bazadan silindi.", deleted_count)


# ═════════════════════════════════════════════════════════════════════════════
#  2. SKİLL EXTRACTION
# ═════════════════════════════════════════════════════════════════════════════
def extract_skills(text: str) -> list[str]:
    """
    Verilən mətn içindəki SKILL_KEYWORDS-ə uyğun gələn sözləri tapır
    və unikal siyahı qaytarır.

    Axtarış kiçik hərfə çevirilib aparılır (case-insensitive).
    """
    lower_text = text.lower()
    found: set[str] = set()

    for _category, keywords in SKILL_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lower_text:
                found.add(kw)

    return sorted(found)


# ═════════════════════════════════════════════════════════════════════════════
#  3. HELLOJOB.AZ SCRAPER
# ═════════════════════════════════════════════════════════════════════════════
def _get_vacancy_detail_text(session: requests.Session, url: str) -> str:
    """
    Elanın daxil olduğu səhifənin tam mətnini qaytarır.
    Şəbəkə xətasında boş sətir qaytarır — bot dayanmır.
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Ən çox istifadə edilən konteyner sinifləri; sayt dəyişsə yenilə
        body_tag = (
            soup.find("div", class_="vacancy-description")
            or soup.find("div", class_="job-description")
            or soup.find("main")
            or soup.body
        )
        return body_tag.get_text(" ", strip=True) if body_tag else ""
    except Exception as exc:
        log.warning("Detal səhifəsi oxunmadı (%s): %s", url, exc)
        return ""


def scrape_hellojob(max_pages: int = 3) -> list[dict]:
    """
    hellojob.az/vacancies bölməsindən elanları çəkir.

    Parametr
    --------
    max_pages : neçə səhifə gəzilsin (default 3)

    Qaytarır
    --------
    list[dict] — hər element: title, company, source_link, skills, posted_at
    """
    session   = requests.Session()
    vacancies = []

    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}{VACANCIES_PATH}?page={page}"
        log.info("Səhifə çəkilir: %s", url)

        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Səhifə açılmadı (%s): %s", url, exc)
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # ── Elan kartlarını tap ───────────────────────────────────────────────
        # hellojob.az-ın mövcud HTML strukturuna görə; dəyişsə güncəllə.
        cards = soup.select("div.job-list-item, article.vacancy-card, li.vacancy-item")

        if not cards:
            # Alternativ selector cəhdi
            cards = soup.select("a[href*='/vacancy/']")

        if not cards:
            log.warning("Səhifə %d-də kart tapılmadı. Selektoru yoxla.", page)
            break

        for card in cards:
            # — title —
            title_tag = (
                card.select_one("h2, h3, .vacancy-title, .job-title, .title")
                or card
            )
            title = title_tag.get_text(strip=True)

            # — company —
            company_tag = card.select_one(
                ".company-name, .employer, .firm-name, span[class*='company']"
            )
            company = company_tag.get_text(strip=True) if company_tag else "Naməlum"

            # — link —
            link_tag = card if card.name == "a" else card.find("a")
            href      = link_tag["href"] if link_tag and link_tag.get("href") else ""
            full_link = href if href.startswith("http") else BASE_URL + href

            if not href:
                log.debug("Link tapılmadı, kart atlandı: %s", title)
                continue

            # — skill extraction (detal səhifəsindən) —
            detail_text = _get_vacancy_detail_text(session, full_link)
            combined    = f"{title} {company} {detail_text}"
            skills      = extract_skills(combined)

            vacancies.append({
                "title":       title,
                "company":     company,
                "source_link": full_link,
                "skills":      skills,
                "posted_at":   datetime.now(timezone.utc).isoformat(),
            })
            log.debug("Elan əlavə edildi: %s | Skills: %s", title, skills)

    log.info("Cəmi %d elan çəkildi.", len(vacancies))
    return vacancies


# ═════════════════════════════════════════════════════════════════════════════
#  4. SUPABASE UPSERT
# ═════════════════════════════════════════════════════════════════════════════
def save_to_supabase(vacancies: list[dict]) -> None:
    """
    Elanları Supabase `jobs` cədvəlinə yazır.
    source_link UNIQUE olduğu üçün dublikat girişlər
    `on_conflict` ilə ignore edilir (no update).

    Supabase cədvəl strukturu:
        id          bigserial PRIMARY KEY
        title       text
        company     text
        skills      text[]
        source_link text UNIQUE
        posted_at   timestamptz
    """
    if not vacancies:
        log.info("Yazılacaq elan yoxdur.")
        return

    log.info("%d elan Supabase-ə göndərilir ...", len(vacancies))

    # Batch upsert — source_link konflikt olduqda heç nə etmə
    response = (
        supabase.table(TABLE_NAME)
        .upsert(vacancies, on_conflict="source_link", ignore_duplicates=True)
        .execute()
    )

    inserted = len(response.data) if response.data else 0
    log.info("%d yeni elan bazaya əlavə edildi (dublikatlar atlandı).", inserted)


# ═════════════════════════════════════════════════════════════════════════════
#  5. ANA PROSES
# ═════════════════════════════════════════════════════════════════════════════
def main() -> None:
    log.info("══════════ İşTap Scraper Başladı ══════════")

    # Addım 1 — Köhnə elanları təmizlə
    cleanup_old_jobs()

    # Addım 2 — Yeni elanları çək
    vacancies = scrape_hellojob(max_pages=3)

    # Addım 3 — Bazaya yaz
    save_to_supabase(vacancies)

    log.info("══════════ İşTap Scraper Tamamlandı ══════════")


if __name__ == "__main__":
    main()
