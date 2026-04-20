"""
handlers/register.py

FSM-поток регистрации автосервиса.
Доступен любому пользователю (в будущем — платная функция).
"""

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from database import db
from keyboards import kb_cancel, kb_owner_main

logger = logging.getLogger(__name__)
router = Router()


# ─────────────────────────────────────────────────────────────────────────────

class RegService(StatesGroup):
    name     = State()
    phone    = State()
    city     = State()
    address  = State()
    admin_id = State()


# ── Вход ─────────────────────────────────────────────────────────────────────

@router.message(Command("register_service"))
@router.message(F.text == "📝 Зарегистрировать сервис")
async def register_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🚗 <b>Регистрация автосервиса</b>\n\n"
        "<b>Шаг 1/5.</b> Введите <b>название</b> вашего автосервиса:",
        parse_mode="HTML",
        reply_markup=kb_cancel(),
    )
    await state.set_state(RegService.name)


# ── Шаги ─────────────────────────────────────────────────────────────────────

@router.message(RegService.name)
async def reg_name(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        return await _cancel(message, state)
    name = message.text.strip()
    if len(name) < 3:
        await message.answer("❌ Название должно быть не короче 3 символов. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await message.answer(
        "<b>Шаг 2/5.</b> Введите <b>номер телефона</b> сервиса:\n"
        "<i>Пример: +7 (999) 123-45-67</i>",
        parse_mode="HTML",
    )
    await state.set_state(RegService.phone)


@router.message(RegService.phone)
async def reg_phone(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        return await _cancel(message, state)
    phone = message.text.strip()
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("❌ Некорректный номер телефона. Попробуйте ещё раз.")
        return
    await state.update_data(phone=phone)
    await message.answer(
        "<b>Шаг 3/5.</b> Введите <b>город</b>:\n<i>Пример: Москва</i>",
        parse_mode="HTML",
    )
    await state.set_state(RegService.city)


@router.message(RegService.city)
async def reg_city(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        return await _cancel(message, state)
    city = message.text.strip()
    if len(city) < 2:
        await message.answer("❌ Слишком короткое название города.")
        return
    await state.update_data(city=city)
    await message.answer(
        "<b>Шаг 4/5.</b> Введите <b>адрес</b> (улица и дом):\n"
        "<i>Пример: ул. Пушкина, д. 10</i>",
        parse_mode="HTML",
    )
    await state.set_state(RegService.address)


@router.message(RegService.address)
async def reg_address(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        return await _cancel(message, state)
    address = message.text.strip()
    if len(address) < 5:
        await message.answer("❌ Адрес слишком короткий.")
        return
    await state.update_data(address=address)
    await message.answer(
        "<b>Шаг 5/5.</b> Укажите <b>Telegram-аккаунт администратора</b>:\n\n"
        "• <code>@username</code>\n"
        "• <code>123456789</code> — числовой ID (узнать: @userinfobot)\n\n"
        "<i>Можно указать себя.</i>",
        parse_mode="HTML",
    )
    await state.set_state(RegService.admin_id)


@router.message(RegService.admin_id)
async def reg_admin(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        return await _cancel(message, state)

    admin_tg_id, admin_display = await _resolve_user(message, message.text.strip())
    if admin_tg_id is None:
        return  # ошибка уже отправлена

    data = await state.get_data()
    await state.clear()

    try:
        idservice = await db.create_service(
            name=data["name"],
            phone=data["phone"],
            city=data["city"],
            address=data["address"],
            owner_tg_id=message.from_user.id,
        )
        await db.add_admin(idservice, admin_tg_id)
    except Exception as exc:
        logger.exception("Ошибка при регистрации сервиса")
        await message.answer(f"❌ Ошибка при сохранении:\n<code>{exc}</code>", parse_mode="HTML")
        return

    link = db.service_link(idservice)
    await message.answer(
        "✅ <b>Сервис успешно зарегистрирован!</b>\n\n"
        f"<b>Название:</b> {data['name']}\n"
        f"<b>Телефон:</b> {data['phone']}\n"
        f"<b>Город:</b> {data['city']}\n"
        f"<b>Адрес:</b> {data['address']}\n"
        f"<b>Администратор:</b> {admin_display}\n\n"
        f"<b>ID сервиса:</b> <code>{idservice}</code>\n\n"
        "🔗 <b>Ссылка для клиентов</b> (разместите её там, где вас найдут):\n"
        f"<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=kb_owner_main(),
    )

    # Уведомляем администратора (если он отличается от регистратора)
    if admin_tg_id != message.from_user.id:
        try:
            await message.bot.send_message(
                admin_tg_id,
                f"👋 Вас назначили администратором сервиса <b>{data['name']}</b>!\n\n"
                f"Нажмите /start для работы с заявками.",
                parse_mode="HTML",
            )
        except Exception:
            pass  # пользователь не запустил бота — ок


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    from keyboards import kb_client_main
    await message.answer("↩️ Регистрация отменена.", reply_markup=kb_client_main())


async def _resolve_user(message: Message, text: str) -> tuple[int | None, str]:
    """Вернуть (tg_id, display_name) или (None, '') при ошибке."""
    m = re.match(r"^@(\w{5,32})$", text)
    if m:
        try:
            chat = await message.bot.get_chat(f"@{m.group(1)}")
            return chat.id, f"@{m.group(1)} (ID: {chat.id})"
        except Exception:
            await message.answer(
                f"❌ Не удалось найти <code>@{m.group(1)}</code>.\n"
                "Убедитесь, что пользователь уже писал боту, или введите числовой ID.",
                parse_mode="HTML",
            )
            return None, ""

    if text.lstrip("-").isdigit():
        tg_id = int(text)
        try:
            await message.bot.get_chat(tg_id)
            return tg_id, f"ID {tg_id}"
        except Exception:
            await message.answer(
                f"❌ Пользователь <code>{tg_id}</code> не найден.",
                parse_mode="HTML",
            )
            return None, ""

    await message.answer(
        "❌ Неверный формат. Введите <code>@username</code> или числовой ID.",
        parse_mode="HTML",
    )
    return None, ""
