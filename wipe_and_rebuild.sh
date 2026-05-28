#!/usr/bin/env bash
# Очищає БД і processed-dates щоб всі колектори пройшли з нуля повну історію.
# НЕ чіпає: chrome_profile*, ms_library.json, photo_groups.json, stock_colors.
set -euo pipefail

DATA_DIR="$HOME/Library/Application Support/StockAutomation"

echo "→ Зупиняю Flask (якщо запущений)"
pkill -f "main.py" 2>/dev/null || true
sleep 1

echo "→ Бекап поточної БД на робочий стіл"
BACKUP="$HOME/Desktop/sales-before-rebuild-$(date +%Y%m%d-%H%M%S).db"
if [ -f "$DATA_DIR/sales.db" ]; then
    cp "$DATA_DIR/sales.db" "$BACKUP"
    echo "  ✓ $BACKUP"
fi

echo "→ Видаляю sales.db"
rm -f "$DATA_DIR/sales.db" "$DATA_DIR/sales.db-shm" "$DATA_DIR/sales.db-wal"

echo "→ Створюю порожній sales.db щоб міграція з ~/Desktop НЕ запустилась"
# Створюємо валідний пустий SQLite файл — init_db потім додасть таблиці
python3 -c "import sqlite3; sqlite3.connect('$DATA_DIR/sales.db').close()"

echo "→ Видаляю processed-dates (щоб усі історичні чанки пройшли)"
rm -f "$DATA_DIR/recipes/_processed_dates.json"

echo "→ Видаляю асет-метадані (pHash/RGB будуть перерахуватись на нових thumbnails)"
# Опційно — можна закоментувати щоб залишити старі pHash, але вони з watermarked Adobe → краще скинути
# rm -f "$DATA_DIR/asset_meta_keep_for_now"

echo ""
echo "✅ Готово. Тепер:"
echo "   1. Відкрий Stock Automation"
echo "   2. На вкладці Browser або Downloads → Refresh"
echo "   3. У кожному вікні стоку — перевір що залогінений (Adobe, Shutterstock,"
echo "      Getty/accountmanagement, Depositphotos, Microstock+)"
echo "   4. Стартує паралельний збір повної історії + auto rebuild-matches в кінці"
echo ""
echo "   Бекап старої БД: $BACKUP"
