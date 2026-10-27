import asyncio
import logging
from typing import Dict, Any, List
import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

# Настройки по умолчанию
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
CHECK_INTERVAL = 300  # 5 минут

logging.basicConfig(level=logging.INFO)
router = Router()

# Временное хранилище в памяти (в prod используем PostgreSQL/Redis)
# Формат: {user_id: [{"article": "123456", "target_price": 1000, "last_price": None}]}
TRACKED_ITEMS: Dict[int, List[Dict[str, Any]]] = {}

async def get_wb_item_data(article: str) -> Dict[str, Any] | None:
    """Запрос к открытому API Wildberries для получения актуальной цены и наличия."""
    url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={article}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    products = data.get("data", {}).get("products", [])
                    if products:
                        product = products[0]
                        # Берём цену со скидкой покупателя (в копейках -> в рубли)
                        price_rub = product.get("sizes", [{}])[0].get("price", {}).get("total", 0) // 100
                        name = product.get("name", "Товар WB")
                        brand = product.get("brand", "Неизвестно")
                        return {"name": name, "brand": brand, "price": price_rub}
    except Exception as e:
        logging.error(f"Ошибка при запросе артикула {article}: {e}")
    return None

@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "🚀 <b>WB Competitor Sentinel</b> — Твой инструмент контроля цен конкурентов!\n\n"
        "<b>Команды:</b>\n"
        "/track <code>[артикул] [порог_цены]</code> — Добавить товар для отслеживания\n"
        "Пример: <code>/track 211603581 1500</code>\n\n"
        "/list — Список ваших отслеживаемых товаров\n"
        "/clear — Очистить список отслеживания"
    )
    await message.answer(welcome_text, parse_mode="HTML")

@router.message(Command("track"))
async def cmd_track(message: Message):
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("❌ Ошибка! Используйте формат: <code>/track [артикул] [целевая_цена]</code>", parse_mode="HTML")
        return

    article, target_price = args[0], args[1]
    if not target_price.isdigit():
        await message.answer("❌ Целевая цена должна быть числом!")
        return

    user_id = message.from_user.id
    wb_data = await get_wb_item_data(article)
    
    if not wb_data:
        await message.answer(f"❌ Не удалось найти товар с артикулом <code>{article}</code> на Wildberries.", parse_mode="HTML")
        return

    current_price = wb_data["price"]
    item_info = {
        "article": article,
        "target_price": int(target_price),
        "last_price": current_price,
        "name": wb_data["name"]
    }

    if user_id not in TRACKED_ITEMS:
        TRACKED_ITEMS[user_id] = []
    TRACKED_ITEMS[user_id].append(item_info)

    await message.answer(
        f"✅ <b>Товар добавлен в мониторинг!</b>\n\n"
        f"📦 <b>Название:</b> {wb_data['name']}\n"
        f"🏷 <b>Бренд:</b> {wb_data['brand']}\n"
        f"💰 <b>Текущая цена:</b> {current_price} ₽\n"
        f"🎯 <b>Порог алерта:</b> ≤ {target_price} ₽",
        parse_mode="HTML"
    )

@router.message(Command("list"))
async def cmd_list(message: Message):
    user_id = message.from_user.id
    items = TRACKED_ITEMS.get(user_id, [])
    if not items:
        await message.answer("У вас пока нет отслеживаемых товаров.")
        return
    
    response = "📋 <b>Ваши отслеживаемые товары:</b>\n\n"
    for i, item in enumerate(items, 1):
        response += f"{i}. <b>{item['name']}</b> (<code>{item['article']}</code>)\n   Цена: {item['last_price']} ₽ | Порог: {item['target_price']} ₽\n"
    await message.answer(response, parse_mode="HTML")

async def background_monitoring(bot: Bot):
    """Фоновая задача проверки цен конкурентов."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        for user_id, items in TRACKED_ITEMS.items():
            for item in items:
                wb_data = await get_wb_item_data(item["article"])
                if wb_data:
                    new_price = wb_data["price"]
                    target_price = item["target_price"]
                    
                    # Уведомление, если цена упала ниже порога
                    if new_price <= target_price and (item["last_price"] is None or new_price < item["last_price"]):
                        diff = (item["last_price"] or new_price) - new_price
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"⚠️ <b>АЛЕРТ ДЕМПИНГА!</b>\n\n"
                                 f"📦 <b>{item['name']}</b>\n"
                                 f"🔗 https://www.wildberries.ru/catalog/{item['article']}/detail.aspx\n"
                                 f"📉 Новая цена: <b>{new_price} ₽</b> (Снизилась на {diff} ₽)\n"
                                 f"🎯 Ваш порог: {target_price} ₽",
                            parse_mode="HTML"
                        )
                    item["last_price"] = new_price

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    # Запуск фонового парсера
    asyncio.create_task(background_monitoring(bot))
    
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Укажите ваш токен перед запуском
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(" ОШИБКА: Замените BOT_TOKEN в main.py на реальный токен Telegram-бота.")
    else:
        asyncio.run(main())
