#!/usr/bin/env python3
"""Імпорт TSV виписок Getty/iStock → sales.db.

Використання:
  python3 import_getty.py                        # авто-завантажує всі виписки з сайту
  python3 import_getty.py --local                # тільки локальні файли з ~/Downloads/
  python3 import_getty.py ~/Downloads/getty/     # конкретна папка
  python3 import_getty.py file1.txt file2.txt    # конкретні файли
"""
import csv, sqlite3, sys, os, glob, io, time, json
from datetime import datetime

DB_NAME = "sales.db"
PROFILE_DIR = os.path.join(os.path.dirname(__file__), "getty_profile")
REPORTS_URL = "https://accountmanagement.gettyimages.com/Reports/Export"
OPTIONS_URL = "https://accountmanagement.gettyimages.com/Reports/StatementExportOptions"
CONTRACT_ID = "8368474:True"

MONTH_MAP = {
    "jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
    "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"
}

def parse_date(s):
    parts = s.strip().split("-")
    if len(parts) == 3:
        d, m, y = parts
        m2 = MONTH_MAP.get(m.lower(), "01")
        return f"{y}-{m2}-{d.zfill(2)} 00:00:00"
    return s

def is_getty_tsv(path):
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            header = f.readline()
        return "Asset Number" in header and "Gross Royalty" in header
    except Exception:
        return False

def find_tsv_files(args):
    if args:
        files = []
        for a in args:
            if os.path.isdir(a):
                files += glob.glob(os.path.join(a, "*.txt"))
                files += glob.glob(os.path.join(a, "*.tsv"))
            elif os.path.isfile(a):
                files.append(a)
        return sorted(files)
    downloads = os.path.expanduser("~/Downloads")
    candidates = glob.glob(os.path.join(downloads, "*.txt"))
    candidates += glob.glob(os.path.join(downloads, "*.tsv"))
    return sorted(f for f in candidates if is_getty_tsv(f))

def import_tsv_content(content, conn):
    """Імпортує TSV з рядка/байтів. Повертає (saved, skipped)."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="ignore")
    saved = skipped = 0
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    for row in reader:
        asset_id   = row.get("Asset Number", "").strip()
        filename   = row.get("Alternate Asset Number", "").strip()
        title      = row.get("Asset Description", "").strip()
        collection = row.get("Collection", "").strip()
        date_raw   = row.get("Sales Date", "").strip()
        price_raw  = row.get("Gross Royalty in USD", "0").strip()

        if not asset_id or not date_raw:
            continue

        stock = "iStockphoto" if "premium" in collection.lower() else "iStock"
        price = float(price_raw) if price_raw else 0.0
        date  = parse_date(date_raw)
        day   = date[:10]

        exists = conn.execute(
            'SELECT 1 FROM sales WHERE stock=? AND asset_id=? AND price=? AND date LIKE ?',
            (stock, asset_id, price, day + '%')
        ).fetchone()
        if exists:
            skipped += 1
            continue

        photo_name = filename or title or asset_id
        conn.execute(
            'INSERT INTO sales (asset_id,photo_name,stock,price,thumb_url,date) VALUES (?,?,?,?,?,?)',
            (asset_id, photo_name, stock, price, None, date)
        )
        saved += 1

    conn.commit()
    return saved, skipped

def import_file(path, conn):
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()
    return import_tsv_content(content, conn)

def check_session(page):
    """Перевіряє чи сесія активна (не редирект на login)."""
    return "sign-in" not in page.url and "login" not in page.url.lower()

def download_all_statements(conn):
    """Завантажує всі доступні TSV виписки з accountmanagement.gettyimages.com."""
    from playwright.sync_api import sync_playwright

    total_saved = total_skipped = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--restore-last-session",
                "--disable-session-crashed-bubble",
            ],
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        print("Переходимо на сторінку виписок...")
        page.goto(REPORTS_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        if not check_session(page):
            print("⚠️  Сесія закінчилась. Залогуйся і закрий браузер.")
            browser.wait_for_event("close", timeout=0)
            print("Браузер закрито. Продовжуємо...")
            browser2 = pw.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = browser2.pages[0] if browser2.pages else browser2.new_page()
            page.goto(REPORTS_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            browser = browser2

        # Отримуємо список доступних виписок
        print("Завантажуємо список доступних виписок...")
        resp = page.evaluate("""async () => {
            const r = await fetch('/Reports/StatementExportOptions?reportType=&year=', {credentials: 'include'});
            return await r.json();
        }""")

        years_data = resp.get("Years", [])
        if not years_data:
            print("❌ Не вдалось отримати список виписок")
            browser.close()
            return 0, 0

        # Збираємо всі (year, month) пари
        periods = []
        for y_obj in years_data:
            year = str(y_obj.get("Year", ""))
            for m_obj in y_obj.get("Months", []):
                month = str(m_obj.get("Value", "")).zfill(2)
                periods.append((year, month))

        print(f"Знайдено виписок: {len(periods)}")

        # Отримуємо CSRF токен з форми на сторінці
        csrf_token = page.evaluate("""() => {
            const el = document.querySelector('input[name="__RequestVerificationToken"]');
            return el ? el.value : null;
        }""")

        if not csrf_token:
            # Спробуємо через meta тег
            csrf_token = page.evaluate("""() => {
                const el = document.querySelector('meta[name="csrf-token"]') ||
                           document.querySelector('meta[name="__RequestVerificationToken"]');
                return el ? el.getAttribute('content') : null;
            }""")

        if not csrf_token:
            print("❌ Не вдалось знайти CSRF токен")
            browser.close()
            return 0, 0

        print(f"CSRF токен отримано ({csrf_token[:20]}...)")

        for year, month in periods:
            label = f"{year}-{month}"
            try:
                # Використовуємо fetch з браузера для отримання файлу
                tsv_content = page.evaluate("""async ([year, month, contract, csrf]) => {
                    const body = new URLSearchParams();
                    body.append('reportTypes', 'monthly');
                    body.append('Years', year);
                    body.append('Months', month);
                    body.append('contractId', contract);
                    body.append('exportFormat', 'tsv');
                    body.append('__RequestVerificationToken', csrf);

                    const r = await fetch('/Reports/Export', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: body.toString()
                    });

                    if (!r.ok) return null;
                    const text = await r.text();
                    return text;
                }""", [year, month, CONTRACT_ID, csrf_token])

                if not tsv_content or len(tsv_content.strip()) < 10:
                    print(f"  {label}: порожня відповідь, пропускаємо")
                    continue

                if "Asset Number" not in tsv_content:
                    print(f"  {label}: не схоже на TSV виписку")
                    continue

                saved, skipped = import_tsv_content(tsv_content, conn)
                total_saved += saved
                total_skipped += skipped
                print(f"  {label}: +{saved} нових, {skipped} дублікатів")
                time.sleep(0.5)

            except Exception as e:
                print(f"  {label}: помилка — {e}")

        browser.close()

    return total_saved, total_skipped

def main():
    args = sys.argv[1:]

    # --local: тільки локальні файли
    if args and args[0] == "--local":
        args = args[1:]
        files = find_tsv_files(args)
        if not files:
            print("❌ Файли не знайдено в ~/Downloads/")
            return
        print(f"Знайдено файлів: {len(files)}")
        total_saved = total_skipped = 0
        with sqlite3.connect(DB_NAME) as conn:
            for path in files:
                name = os.path.basename(path)
                saved, skipped = import_file(path, conn)
                print(f"  {name}: +{saved} нових, {skipped} дублікатів")
                total_saved += saved
                total_skipped += skipped
        print(f"\nРазом: +{total_saved} нових, {total_skipped} дублікатів пропущено")
        return

    # Конкретні файли/папки вказані — локальний режим
    if args:
        files = find_tsv_files(args)
        if not files:
            print("❌ Файли не знайдено.")
            return
        print(f"Знайдено файлів: {len(files)}")
        total_saved = total_skipped = 0
        with sqlite3.connect(DB_NAME) as conn:
            for path in files:
                name = os.path.basename(path)
                saved, skipped = import_file(path, conn)
                print(f"  {name}: +{saved} нових, {skipped} дублікатів")
                total_saved += saved
                total_skipped += skipped
        print(f"\nРазом: +{total_saved} нових, {total_skipped} дублікатів пропущено")
        return

    # Без аргументів — авто-завантаження з сайту
    print("Авто-завантаження виписок Getty/iStock з accountmanagement.gettyimages.com...")
    with sqlite3.connect(DB_NAME) as conn:
        total_saved, total_skipped = download_all_statements(conn)

    print(f"\nРазом: +{total_saved} нових, {total_skipped} дублікатів пропущено")

if __name__ == "__main__":
    main()
