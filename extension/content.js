// ПЕРЕВІРКА: ЧИ ЗАПУЩЕНО АВТОМАТИЧНИЙ РЕЖИМ
if (window.location.search.includes('full_auto=1')) {
    console.log("Stocker AI: Автономний режим активовано.");
    setTimeout(startFullMonthScrape, 5000); // Чекаємо завантаження React
}

async function startFullMonthScrape() {
    // 1. Знаходимо всі посилання на дні (напр. 04/01/2026)
    const dayLinks = Array.from(document.querySelectorAll('a'))
                          .filter(a => /\d{2}\/\d{2}\/\d{4}/.test(a.innerText));

    console.log(`Знайдено днів для обробки: ${dayLinks.length}`);

    for (let i = 0; i < dayLinks.length; i++) {
        const link = dayLinks[i];
        const dateText = link.innerText.trim();
        
        console.log(`Перехід до дати: ${dateText}`);
        link.click();
        await new Promise(r => setTimeout(r, 3000)); // Чекаємо переходу

        // 2. Проходимо по всіх вкладках (Subscriptions, On Demand, Enhanced)
        const tabs = document.querySelectorAll('.MuiTab-root');
        for (let tab of tabs) {
            const tabName = tab.innerText;
            console.log(`Аналіз вкладки: ${tabName}`);
            tab.click();
            await new Promise(r => setTimeout(r, 2000));

            // Відправляємо HTML вкладки в Gemini
            chrome.runtime.sendMessage({ 
                type: "ANALYZE_WITH_GEMINI", 
                html: document.body.innerHTML 
            });
        }

        // Повертаємося до головної таблиці
        window.history.back();
        await new Promise(r => setTimeout(r, 3000));
    }

    console.log("Збір місяця завершено. Закриваю вкладку.");
    chrome.runtime.sendMessage({ type: "CLOSE_AUTO_TAB" });
}

// РУЧНА КНОПКА (ЯКЩО ЗАХОЧЕТЬСЯ ЗАПУСТИТИ САМОМУ)
if (!document.getElementById('stocker-btn')) {
    const btn = document.createElement("button");
    btn.id = 'stocker-btn';
    btn.innerHTML = "🚀 ЗАПУСТИТИ AI-ЗБІР";
    btn.style = "position: fixed; top: 100px; right: 20px; z-index: 99999; padding: 15px 25px; background: #8E44AD; color: #fff; border: none; border-radius: 10px; cursor: pointer; font-weight: bold; box-shadow: 0 4px 10px rgba(0,0,0,0.4);";
    document.body.appendChild(btn);
    
    btn.onclick = () => {
        btn.innerHTML = "🔄 ПРАЦЮЄ РОБОТ...";
        startFullMonthScrape();
    };
}